import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ScenarioCard,
  ScenarioCardMenu,
  ScenarioVersionSettings,
  ScenarioSettings,
  deleteScenarioResource,
} from "./pages/Scenarios";
import {
  scenarioHasPublishedVersion,
  scenarioDraftPayload,
  type Graph,
} from "./scenario";
const definition = (): Graph => ({
  schema_version: "korsik-scenario-v2",
  algorithm_version: "calendar-v1",
  editor_mode: "SIMPLE",
  start_step_key: "hello",
  steps: [],
  source_mapping: [],
  start_policy: { condition: "ON_BOT_START", timezone: "Europe/Moscow" },
  resume_policy: { reminder_hours: 24, max_reminders: 3 },
});
function buttonDepth(html: string) {
  let depth = 0,
    maximum = 0;
  for (const token of html.match(/<\/?button\b/g) || []) {
    depth += token.startsWith("</") ? -1 : 1;
    maximum = Math.max(maximum, depth);
  }
  return maximum;
}

describe("scenario cards and version management", () => {
  it.each([
    { latest_version_status: "DRAFT", has_published_versions: true },
    {
      latest_version_status: "DRAFT",
      current_published_version_id: "published",
    },
    { latest_version_status: "DRAFT", published_version_id: "published" },
    {
      latest_version_status: "ARCHIVED",
      has_published_versions: true,
      current_published_version_id: null,
    },
  ])(
    "keeps a published scenario published while another version is a draft: %j",
    (state) => {
      expect(scenarioHasPublishedVersion(state)).toBe(true);
      const html = renderToStaticMarkup(
        <ScenarioCard
          scenario={{ id: "scenario", name: "Адаптация", ...state }}
          onOpen={() => {}}
        />,
      );
      expect(html).toContain(">Опубликован<");
      expect(html).not.toContain("Черновик");
      expect(html).not.toContain("Не опубликован");
    },
  );
  it("labels an unpublished scenario honestly and keeps native card/menu buttons separate", () => {
    const html = renderToStaticMarkup(
      <ScenarioCard
        scenario={{
          id: "new",
          name: "Первый день",
          has_published_versions: false,
          latest_version_status: "DRAFT",
        }}
        onOpen={() => {}}
        onRename={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(html).toContain("Не опубликован");
    expect(html).toContain("Действия со сценарием: Первый день");
    expect(buttonDepth(html)).toBe(1);
    expect(html.match(/<button\b/g)).toHaveLength(2);
    const menu = renderToStaticMarkup(
      <ScenarioCardMenu
        onRename={() => {}}
        onDelete={() => {}}
        onClose={() => {}}
      />,
    );
    expect(menu).toContain("Переименовать");
    expect(menu).toContain("Удалить");
    expect(buttonDepth(menu)).toBe(1);
    const readonly = renderToStaticMarkup(
      <ScenarioCard
        scenario={{ name: "Общая", can_edit: false }}
        onOpen={() => {}}
        onRename={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(readonly).not.toContain("Действия со сценарием");
  });
  it.each(["PUBLISHED", "ARCHIVED", "DELETED"])(
    "does not offer version rename or deletion for immutable %s",
    (status) => {
      const html = renderToStaticMarkup(
        <ScenarioVersionSettings
          name="Зафиксированная версия"
          status={status}
          busy={false}
          canEdit
          onRename={() => {}}
          onDelete={() => {}}
        />,
      );
      expect(html).toMatch(/<input[^>]*disabled=""/);
      expect(html).not.toContain("Удалить черновик");
      expect(html).not.toContain("Сохранить изменения");
    },
  );
  it.each(["DRAFT", "IN_REVIEW", "APPROVED"])(
    "allows renaming editable %s while deleting only a draft",
    (status) => {
      const html = renderToStaticMarkup(
        <ScenarioVersionSettings
          name="Рабочая версия"
          status={status}
          busy={false}
          canEdit
          onRename={() => {}}
          onDelete={() => {}}
        />,
      );
      expect(html).toContain("Название версии");
      expect(html).toContain("Сохранить изменения");
      expect(html).not.toMatch(/<input[^>]*disabled=""/);
      expect(html.includes(">Удалить<")).toBe(status === "DRAFT");
    },
  );
  it("sends a fenced DELETE for the exact selected draft without carrying a graph rewrite", async () => {
    const transport = vi.fn().mockResolvedValue({ data: {}, etag: null });
    await deleteScenarioResource(
      "/scenario-versions/selected-draft",
      '"selected:4"',
      transport as any,
    );
    expect(transport).toHaveBeenCalledWith(
      "/scenario-versions/selected-draft",
      { method: "DELETE" },
      '"selected:4"',
    );
  });
});

describe("draft cloning preserves the existing execution language", () => {
  it("clones a legacy route with no conversion flag and requests SIMPLE for a simple or new scenario", () => {
    const legacy = {
      id: "legacy-published",
      graph: { editor_mode: undefined },
    };
    expect(scenarioDraftPayload(legacy)).toEqual({
      base_version_id: "legacy-published",
    });
    expect(
      scenarioDraftPayload({
        id: "simple-published",
        graph: { editor_mode: "SIMPLE" },
      }),
    ).toEqual({ base_version_id: "simple-published", editor_mode: "SIMPLE" });
    expect(scenarioDraftPayload()).toEqual({ editor_mode: "SIMPLE" });
    expect(legacy.graph.editor_mode).toBeUndefined();
  });
});

describe("bounded repeats in simple settings", () => {
  it.each(["ON_BOT_START", "START_DATE"] as const)(
    "shows a bounded repeat cap for %s",
    (condition) => {
      const graph = definition();
      graph.start_policy!.condition = condition;
      const html = renderToStaticMarkup(
        <ScenarioSettings graph={graph} locked={false} onChange={() => {}} />,
      );
      expect(html).toContain("Количество напоминаний");
      expect(html).toMatch(/<input[^>]*min="0"[^>]*max="100"[^>]*value="3"/);
      expect(html).toContain("Повторить незавершённый шаг через, часов");
    },
  );
  it("does not silently invent a cap for an older graph or show it for a scheduled launch", () => {
    const graph = definition();
    delete graph.resume_policy!.max_reminders;
    const html = renderToStaticMarkup(
      <ScenarioSettings graph={graph} locked={false} onChange={() => {}} />,
    );
    expect(html).toContain('placeholder="Без ограничения"');
    expect(graph.resume_policy).toEqual({ reminder_hours: 24 });
    graph.start_policy = {
      condition: "SCHEDULED",
      date: "2026-11-01",
      time: "10:00",
      timezone: "Europe/Moscow",
    };
    const scheduled = renderToStaticMarkup(
      <ScenarioSettings graph={graph} locked={false} onChange={() => {}} />,
    );
    expect(scheduled).not.toContain("Количество напоминаний");
  });
});

describe("full calendar recurrence settings", () => {
  it("keeps the full-run calendar separate from reminders and exposes an explicit local timezone", () => {
    const graph = definition();
    graph.start_policy = {condition:"RECURRING", every_days:7, time:"09:30", timezone:"Asia/Vladivostok"};
    const html=renderToStaticMarkup(<ScenarioSettings graph={graph} locked={false} onChange={()=>{}}/>);
    expect(html).toContain("Повторять весь сценарий каждые, дней");
    expect(html).toMatch(/min="1"[^>]*max="365"[^>]*value="7"/);
    expect(html).toContain('value="09:30"');
    expect(html).toContain('value="Asia/Vladivostok" selected=""');
    expect(html).toContain("Количество напоминаний");
    expect(html).toContain("Повторить незавершённый шаг");
    expect(html).not.toContain("День календаря");
  });
  it("places one combined save after settings and draft deletion before save", () => {
    const html=renderToStaticMarkup(<ScenarioVersionSettings name="Редакция" status="DRAFT" busy={false} canEdit onRename={()=>{}} onDelete={()=>{}}><p>Поля запуска</p></ScenarioVersionSettings>);
    expect(html.match(/Сохранить изменения/g)).toHaveLength(1);
    expect(html).not.toContain("Сохранить название");
    expect(html.indexOf("Поля запуска")).toBeLessThan(html.indexOf(">Удалить<"));
    expect(html.indexOf(">Удалить<")).toBeLessThan(html.indexOf("Сохранить изменения"));
  });
});
