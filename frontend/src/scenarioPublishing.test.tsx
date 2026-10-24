import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { api } from "./api";
import {
  publishScenario,
  ScenarioActions,
  persistScenarioDraft,
  type ScenarioDraftState,
} from "./pages/Scenarios";
import type { Graph } from "./scenario";

afterEach(() => vi.restoreAllMocks());

function successfulApi(validation: any = { valid: true, source_changes: [] }) {
  const calls: { path: string; body?: any; etag?: string | null }[] = [];
  vi.spyOn(api, "get").mockImplementation(async (path) => {
    calls.push({ path });
    return { data: { status: "DRAFT" }, etag: '"after-validation"' } as any;
  });
  vi.spyOn(api, "post").mockImplementation(async (path, body, etag) => {
    calls.push({ path, body, etag });
    if (path.endsWith("/validate"))
      return { data: validation, etag: null } as any;
    return {
      data: { status: path.endsWith("/review") ? "APPROVED" : "PUBLISHED" },
      etag: path.endsWith("/review") ? '"after-review"' : '"published"',
    } as any;
  });
  return calls;
}

describe("single scenario publication action", () => {
  it("validates, acquires a fresh ETag, reviews, and publishes for new enrollments only", async () => {
    const calls = successfulApi();
    const result = await publishScenario("draft");
    expect(result.kind).toBe("published");
    expect(calls.map((call) => call.path)).toEqual([
      "/scenario-versions/draft/validate",
      "/scenario-versions/draft",
      "/scenario-versions/draft/review",
      "/scenario-versions/draft/publish",
    ]);
    expect(calls[2].etag).toBe('"after-validation"');
    expect(calls[3].etag).toBe('"after-review"');
    expect(calls[3].body).toEqual({
      migration_policy: "NEW_ENROLLMENTS_ONLY",
      reason: "Публикация схемы адаптации из редактора",
    });
  });

  it("stops before approval when server validation fails", async () => {
    const validation = {
      valid: false,
      errors: [{ message: "Нет конечного шага" }],
    };
    const calls = successfulApi(validation);
    expect(await publishScenario("draft")).toEqual({
      kind: "invalid",
      validation,
    });
    expect(calls).toHaveLength(1);
  });

  it("does not silently approve changed mandatory steps or stale acknowledgements", async () => {
    const validation = {
      valid: true,
      source_changes: [
        { step_key: "welcome", change: "CHANGED_MANDATORY_STEP" },
      ],
      source_change_fingerprint: "current-source-fingerprint",
    };
    const calls = successfulApi(validation);
    expect((await publishScenario("draft")).kind).toBe("source_review");
    expect(
      (
        await publishScenario("draft", {
          fingerprint: "old",
          reason: "Проверено",
        })
      ).kind,
    ).toBe("source_review");
    expect(
      (
        await publishScenario("draft", {
          fingerprint: "current-source-fingerprint",
          reason: "  ",
        })
      ).kind,
    ).toBe("source_review");
    expect(calls).toHaveLength(3);
    expect(calls.every((call) => call.path.endsWith("/validate"))).toBe(true);
  });

  it("pins explicit confirmation to the exact current fingerprint and retains the human reason", async () => {
    const calls = successfulApi({
      valid: true,
      source_changes: [
        { step_key: "welcome", change: "CHANGED_MANDATORY_STEP" },
      ],
      source_change_fingerprint: "current",
    });
    expect(
      (
        await publishScenario("draft", {
          fingerprint: "current",
          reason: "  Уточнили время приветствия  ",
        })
      ).kind,
    ).toBe("published");
    expect(calls[2].body).toEqual({
      action: "APPROVE",
      source_change_acknowledged: true,
      source_change_fingerprint: "current",
      source_change_reason: "Уточнили время приветствия",
    });
    expect(calls[3].body.reason).toBe("Уточнили время приветствия");
  });

  it("never invokes publication after review rejects the version", async () => {
    const calls = successfulApi();
    const post = vi.mocked(api.post);
    post
      .mockImplementationOnce(
        async () =>
          ({ data: { valid: true, source_changes: [] }, etag: null }) as any,
      )
      .mockImplementationOnce(async () => {
        throw new Error("Исходная программа изменилась");
      });
    await expect(publishScenario("draft")).rejects.toThrow(
      "Исходная программа изменилась",
    );
    expect(post).toHaveBeenCalledTimes(2);
    expect(calls.some((call) => call.path.endsWith("/publish"))).toBe(false);
  });
});

describe("minimal scenario toolbar", () => {
  it("renders only Simulation and Publish and no publication metadata fields", () => {
    const html = renderToStaticMarkup(
      <ScenarioActions
        busy={false}
        canPublish
        onSimulation={() => {}}
        onPublish={() => {}}
      />,
    );
    expect(html.match(/<button/g) || []).toHaveLength(2);
    expect(html).toContain("Симуляция");
    expect(html).toContain("Опубликовать");
    for (const removed of [
      "Сохранить",
      "Согласовать",
      "Проверить",
      "Основание публикации",
      "<input",
      "<textarea",
    ])
      expect(html).not.toContain(removed);
  });

  it("keeps simulation available for read-only viewers and blocks duplicate busy actions", () => {
    const readOnly = renderToStaticMarkup(
      <ScenarioActions
        busy={false}
        canPublish={false}
        onSimulation={() => {}}
        onPublish={() => {}}
      />,
    );
    expect(readOnly.match(/disabled=""/g) || []).toHaveLength(1);
    const busy = renderToStaticMarkup(
      <ScenarioActions
        busy
        canPublish
        onSimulation={() => {}}
        onPublish={() => {}}
      />,
    );
    expect(busy.match(/disabled=""/g) || []).toHaveLength(2);
  });
});

describe("scenario autosave", () => {
  const graph = (text: string) =>
    ({
      schema_version: "korsik-scenario-v2",
      algorithm_version: "calendar-v1",
      start_step_key: "welcome",
      steps: [{ step_key: "welcome", content: { text_ty: text } }],
    }) as Graph;

  it("serializes rapid edits and saves the newest graph with the preceding response ETag", async () => {
    const first = graph("Первый текст");
    const second = graph("Уточнённый текст");
    const state: ScenarioDraftState = {
      graph: first,
      dirty: true,
      etag: '\"v1\"',
      pending: null,
    };
    let release: (value: any) => void = () => {};
    const delayed = new Promise<any>((resolve) => {
      release = resolve;
    });
    const put = vi
      .spyOn(api, "put")
      .mockImplementationOnce(() => delayed)
      .mockResolvedValueOnce({ data: {}, etag: '\"v3\"' } as any);
    const saved = vi.fn();
    const firstSave = persistScenarioDraft("draft", state, api, saved);
    state.graph = second;
    state.dirty = true;
    const secondSave = persistScenarioDraft("draft", state, api, saved);
    expect(put).toHaveBeenCalledTimes(1);
    release({ data: {}, etag: '\"v2\"' });
    await Promise.all([firstSave, secondSave]);
    expect(put).toHaveBeenCalledTimes(2);
    expect(put.mock.calls.map((call) => [call[1], call[2]])).toEqual([
      [{ graph: first }, '\"v1\"'],
      [{ graph: second }, '\"v2\"'],
    ]);
    expect(state.graph).toBe(second);
    expect(state.etag).toBe('\"v3\"');
    expect(state.dirty).toBe(false);
    expect(state.pending).toBeNull();
    expect(saved).toHaveBeenCalledTimes(1);
  });

  it("retains edits and rejects dependent actions on save conflict instead of clearing them", async () => {
    const latest = graph("Несохранённый текст");
    const state: ScenarioDraftState = {
      graph: latest,
      dirty: true,
      etag: '\"stale\"',
      pending: null,
    };
    vi.spyOn(api, "put").mockRejectedValueOnce(
      new Error("412 version conflict"),
    );
    const saved = vi.fn();
    await expect(
      persistScenarioDraft("draft", state, api, saved),
    ).rejects.toThrow("412 version conflict");
    expect(state.graph).toBe(latest);
    expect(state.dirty).toBe(true);
    expect(state.etag).toBe('\"stale\"');
    expect(state.pending).toBeNull();
    expect(saved).not.toHaveBeenCalled();
  });
});
