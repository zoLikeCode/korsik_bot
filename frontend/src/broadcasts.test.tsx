import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { BroadcastForm, BroadcastStatus } from "./pages/Broadcasts";
import {
  broadcastAudience,
  broadcastMentionPeople,
  broadcastStatus,
  reconcileBroadcastSelection,
  editBroadcastText,
  encodeBroadcastText,
  insertBroadcastMention,
} from "./broadcastText";

describe("broadcast composition and honest delivery state", () => {
  it("inserts the preferred address as plain text without MAX identity", () => {
    const state = insertBroadcastMention(
      { text: "Спасибо, ", mentions: [] },
      {
        id: "employee-1",
        name: "Корпоративное имя",
        preferred_name: "Обращение",
        mention_name: "MAX Имя",
      },
      9,
    );
    expect(state.text).toBe("Спасибо, Обращение");
    expect(encodeBroadcastText(state)).toBe("Спасибо, Обращение");
    expect(
      encodeBroadcastText({ text: "Обычный @MAX Имя", mentions: [] }),
    ).toBe("Обычный @MAX Имя");
  });
  it("keeps a mention when surrounding text shifts and removes native meaning if its name is edited", () => {
    const initial = insertBroadcastMention(
      { text: "Привет ", mentions: [] },
      { id: "one", name: "Иван" },
      7,
    );
    const shifted = editBroadcastText(initial, "Коллеги! " + initial.text);
    expect(encodeBroadcastText(shifted)).toBe("Коллеги! Привет Иван");
    const changed = editBroadcastText(
      shifted,
      shifted.text.replace("Иван", "Пётр"),
    );
    expect(changed.mentions).toEqual([]);
    expect(encodeBroadcastText(changed)).toBe("Коллеги! Привет Пётр");
  });
  it("replacing a selected range never transfers the old person's mention identity to a new name", () => {
    const old = insertBroadcastMention(
      { text: "Добрый день", mentions: [] },
      { id: "one", name: "Анна" },
      0,
      11,
    );
    const next = insertBroadcastMention(
      old,
      { id: "two", name: "Анна" },
      0,
      old.text.length,
    );
    expect(next.mentions).toHaveLength(1);
    expect(encodeBroadcastText(next)).toBe("Анна");
  });
  it("filters address insertion by organizational subtree without requiring MAX", () => {
    const people = [
      {
        id: "one",
        name: "Анна",
        preferred_name: "Аня",
        department: "Отдел",
        available: false,
        mention_available: true,
        org_unit_ancestor_ids: ["division", "team"],
      },
      {
        id: "two",
        name: "Иван",
        mention_available: true,
        org_unit_ancestor_ids: ["else"],
      },
      {
        id: "off",
        name: "Выключен",
        mention_available: false,
        org_unit_ancestor_ids: ["division"],
      },
    ];
    expect(
      broadcastMentionPeople(people, "division", "").map((p) => p.id),
    ).toEqual(["one"]);
    expect(broadcastMentionPeople(people, "", "Аня").map((p) => p.id)).toEqual([
      "one",
    ]);
    expect(
      encodeBroadcastText(
        insertBroadcastMention({ text: "", mentions: [] }, people[0], 0),
      ),
    ).toBe("Аня");
  });
  it("expands selected organizational ancestors and explicit people into one transparent audience", () => {
    const all = [
      {
        id: "a",
        available: true,
        org_unit_ancestor_ids: ["direction", "team"],
      },
      { id: "b", available: false, org_unit_ancestor_ids: ["direction"] },
      { id: "c", available: true, org_unit_ancestor_ids: [] },
    ];
    const selected = broadcastAudience(all, ["a", "c"], ["direction", "team"]);
    expect(selected.map((p) => p.id)).toEqual(["a", "b", "c"]);
    expect(selected.filter((p) => p.available)).toHaveLength(2);
  });
  it("offers explicit safe repair after scope refresh without discarding authored text or valid choices", () => {
    const before = { text: "Важное сообщение", mentions: [] };
    const selected = ["visible", "outside", "off"],
      groups = ["department", "hidden-unit"];
    const repaired = reconcileBroadcastSelection(
      [
        { id: "visible", available: true },
        { id: "off", available: false },
      ],
      [{ id: "department" }],
      selected,
      groups,
    );
    expect(repaired).toEqual({
      recipients: ["visible"],
      units: ["department"],
      removed: 3,
    });
    expect(selected).toEqual(["visible", "outside", "off"]);
    expect(groups).toEqual(["department", "hidden-unit"]);
    expect(encodeBroadcastText(before)).toBe("Важное сообщение");
    expect(
      reconcileBroadcastSelection(
        [{ id: "visible", available: true }],
        [{ id: "department" }],
        repaired.recipients,
        repaired.units,
      ).removed,
    ).toBe(0);
  });
  it("never presents an uncertain or rejected delivery as completed", () => {
    for (const item of [
      { status: "WAITING", sending: true, counts: { failed: 0 } },
      { status: "WAITING", counts: { failed: 1 } },
    ])
      expect(broadcastStatus(item).label).toBe("В ожидании");
    expect(
      broadcastStatus({ status: "COMPLETED", counts: { failed: 0 } }).label,
    ).toBe("Исполнено");
    const html = renderToStaticMarkup(
      <BroadcastStatus item={{ status: "WAITING", counts: { failed: 1 } }} />,
    );
    expect(html).toContain("Есть ошибки доставки");
    expect(html).not.toContain("Исполнено");
    expect(
      broadcastStatus({
        status: "WAITING",
        counts: { failed: 0, unknown: 1, pending: 1 },
      }),
    ).toEqual({
      label: "В ожидании",
      detail: "Результат доставки не подтверждён",
      tone: "amber",
    });
  });
  it("shows unavailable people, all organizational selection, and an explicit Russian timezone without hidden recipient truncation", () => {
    const html = renderToStaticMarkup(
      <BroadcastForm
        employees={[
          {
            id: "a",
            name: "Иван",
            available: false,
            unavailable_reason: "MAX не подключён",
          },
        ]}
        units={[{ id: "unit", title: "Производство", type: "DIRECTION" }]}
        onDone={() => {}}
        onRefresh={() => {}}
      />,
    );
    expect(html).toContain("Подразделения");
    expect(html).toContain("Направление");
    expect(html).toContain("MAX не подключён");
    expect(html).toContain("5000");
    expect(html).toMatch(/<option value="Europe\/Moscow" selected=""/);
    expect(html).toContain('type="date"');
    expect(html).toContain('type="time"');
    expect(html).toMatch(/type="checkbox"[^>]*disabled=""/);
  });
});
