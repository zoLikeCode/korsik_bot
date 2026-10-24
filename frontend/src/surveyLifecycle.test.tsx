import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  publishSurvey,
  saveSurveyDraft,
  surveyPublicationLabel,
} from "./surveyLifecycle";
import Surveys, { CampaignList, Editor } from "./pages/Surveys";
import { ApiError } from "./api";

const fixtures = vi.hoisted(() => ({
  paths: [] as string[],
  campaigns: [] as any[],
}));
vi.mock("./App", () => ({
  useActor: () => ({ role: "HR_SPECIALIST", scope: { site_ids: ["GrPP"] } }),
}));
vi.mock("./ui", async () => ({
  ...(await vi.importActual<any>("./ui")),
  useResource: (path: string | null) => {
    if (path) fixtures.paths.push(path);
    return {
      data: {
        items: path?.startsWith("/survey-campaigns") ? fixtures.campaigns : [],
      },
      loading: false,
      error: "",
      reload: vi.fn(),
    };
  },
}));

const version = { id: "version-1", etag: '"v1"', status: "DRAFT" };
const body = {
  title: "Опрос",
  purpose: "ADAPTATION",
  questions: [
    { question_key: "q1", prompt: "Как проходит адаптация?", type: "TEXT" },
  ],
};
function transport() {
  return { post: vi.fn(), get: vi.fn(), patch: vi.fn() };
}

describe("single-click survey lifecycle", () => {
  it("Save creates a draft without publishing or pretending to approve it", async () => {
    const api = transport(),
      created = vi.fn();
    api.post.mockResolvedValue({ data: { id: "template-1" } });
    api.get.mockResolvedValue({ data: { versions: [version] } });
    const result = await saveSurveyDraft(
      { templateId: null, version: null, body, onCreatedTemplate: created },
      api as any,
    );
    expect(api.post.mock.calls).toEqual([["/survey-templates", body]]);
    expect(api.get).toHaveBeenCalledWith("/survey-templates/template-1");
    expect(created).toHaveBeenCalledWith("template-1");
    expect(result.version).toEqual(version);
  });
  it("new survey Publish saves then publishes the exact returned version and ETag", async () => {
    const api = transport();
    api.post
      .mockResolvedValueOnce({ data: { id: "template-1" } })
      .mockResolvedValueOnce({ data: { ...version, status: "PUBLISHED" } });
    api.get.mockResolvedValue({ data: { versions: [version] } });
    const result = await publishSurvey(
      {
        templateId: null,
        version: null,
        body,
        dirty: false,
        reason: "Явная публикация текущих вопросов",
        onCreatedTemplate: vi.fn(),
      },
      api as any,
    );
    expect(result.version.status).toBe("PUBLISHED");
    expect(api.post.mock.calls).toEqual([
      ["/survey-templates", body],
      [
        "/survey-template-versions/version-1/publish",
        { reason: "Явная публикация текущих вопросов" },
        '"v1"',
      ],
    ]);
  });
  it("publishes edited questions only after saving them with displayed-version CAS", async () => {
    const api = transport();
    api.patch.mockResolvedValue({ data: { ...version, etag: '"v2"' } });
    api.post.mockResolvedValue({ data: { ...version, status: "PUBLISHED" } });
    await publishSurvey(
      {
        templateId: "template-1",
        version,
        body,
        dirty: true,
        reason: "Публикую",
        onCreatedTemplate: vi.fn(),
      },
      api as any,
    );
    expect(api.patch).toHaveBeenCalledWith(
      "/survey-template-versions/version-1",
      body,
      '"v1"',
    );
    expect(api.post).toHaveBeenCalledWith(
      "/survey-template-versions/version-1/publish",
      { reason: "Публикую" },
      '"v2"',
    );
    expect(api.get).not.toHaveBeenCalled();
  });
  it("never refreshes an ETag and retries over another editor's changes after 412", async () => {
    const api = transport();
    api.patch.mockRejectedValue(new ApiError(412, "Version changed"));
    await expect(
      publishSurvey(
        {
          templateId: "template-1",
          version,
          body,
          dirty: true,
          reason: "Публикую",
          onCreatedTemplate: vi.fn(),
        },
        api as any,
      ),
    ).rejects.toMatchObject({ status: 412 });
    expect(api.post).not.toHaveBeenCalled();
    expect(api.get).not.toHaveBeenCalled();
    expect(api.patch).toHaveBeenCalledTimes(1);
  });
  it("remembers a successful creation before a later read fails", async () => {
    const api = transport(),
      created = vi.fn();
    api.post.mockResolvedValue({ data: { id: "template-1" } });
    api.get.mockRejectedValue(new Error("Network failed"));
    await expect(
      saveSurveyDraft(
        { templateId: null, version: null, body, onCreatedTemplate: created },
        api as any,
      ),
    ).rejects.toThrow("Network failed");
    expect(created).toHaveBeenCalledWith("template-1");
    expect(api.post).toHaveBeenCalledTimes(1);
  });
  it("retains a saved ETag in the editor when publication fails", async () => {
    const api = transport(),
      saved = vi.fn();
    api.patch.mockResolvedValue({ data: { ...version, etag: '"v2"' } });
    api.post.mockRejectedValue(new Error("Publish failed"));
    await expect(
      publishSurvey(
        {
          templateId: "template-1",
          version,
          body,
          dirty: true,
          reason: "Публикую",
          onCreatedTemplate: vi.fn(),
          onSavedVersion: saved,
        },
        api as any,
      ),
    ).rejects.toThrow("Publish failed");
    expect(saved).toHaveBeenCalledWith({ ...version, etag: '"v2"' });
  });
  it("rejects edits and repeat publication of an immutable published version", async () => {
    const api = transport(),
      published = { ...version, status: "PUBLISHED" };
    await expect(
      saveSurveyDraft(
        {
          templateId: "template-1",
          version: published,
          body,
          onCreatedTemplate: vi.fn(),
        },
        api as any,
      ),
    ).rejects.toThrow("создайте черновик");
    await expect(
      publishSurvey(
        {
          templateId: "template-1",
          version: published,
          body,
          dirty: false,
          reason: "Публикую",
          onCreatedTemplate: vi.fn(),
        },
        api as any,
      ),
    ).rejects.toThrow("уже опубликована");
    expect(api.post).not.toHaveBeenCalled();
    expect(api.patch).not.toHaveBeenCalled();
  });
  it("labels cards by the published pointer even if a new draft exists", () => {
    expect(surveyPublicationLabel({ published_version_id: "v1" })).toBe(
      "Опубликованный",
    );
    expect(surveyPublicationLabel({ published_version_id: null })).toBe(
      "Неопубликованный",
    );
  });
  it("renders two explicit editor actions for HR without intermediate review buttons", () => {
    const html = renderToStaticMarkup(
      <Editor id="new" onDone={() => {}} onClose={() => {}} />,
    );
    expect(html).toContain("Сохранить</button>");
    expect(html).toContain("Опубликовать</button>");
    for (const text of [
      "На проверку",
      "Согласовать",
      "Вернуть на доработку",
      "Рассылки и результаты",
    ])
      expect(html).not.toContain(text);
  });
  it("main HR page only requests templates, and never renders the empty campaign section", () => {
    fixtures.paths = [];
    const html = renderToStaticMarkup(<Surveys />);
    expect(fixtures.paths).toEqual(["/survey-templates"]);
    expect(html).toContain("Создать опрос");
    expect(html).not.toContain("Рассылки и результаты");
    expect(html).not.toContain("Рассылок пока нет");
  });
  it("keeps actual selected-template results reachable and hides an empty results block", () => {
    fixtures.paths = [];
    fixtures.campaigns = [];
    expect(
      renderToStaticMarkup(
        <CampaignList hr revision={0} templateId="template-1" />,
      ),
    ).toBe("");
    expect(fixtures.paths).toEqual([
      "/survey-campaigns?template_id=template-1",
    ]);
    fixtures.campaigns = [
      {
        id: "campaign-1",
        name: "Адаптация команды",
        status: "SCHEDULED",
        schedule: {
          starts_at: "2026-10-25T10:00:00Z",
          timezone: "Europe/Moscow",
        },
      },
    ];
    const html = renderToStaticMarkup(
      <CampaignList hr revision={0} templateId="template-1" />,
    );
    expect(html).toContain("Адаптация команды");
    expect(html).toContain("Открыть");
    fixtures.campaigns = [];
  });
});
