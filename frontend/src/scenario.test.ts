import { describe, it, expect } from "vitest";
import {
  insertStep,
  removeStep,
  checkGraph,
  connectSteps,
  removeTransition,
  moveStep,
  type Graph,
  type Step,
  newStep,
} from "./scenario";
const step = (id: string, target?: string): Step => ({
  step_key: id,
  kind: "MESSAGE",
  mandatory: false,
  sequence_no: 0,
  eligibility: {},
  delivery_policy: {},
  transitions: target ? [{ target_step_key: target }] : [],
});

describe("executable canvas connections", () => {
  it("adds and deletes a real transition while preserving the source mapping and original graph", () => {
    const source = graph();
    source.steps.push(step("c"));
    const connected = connectSteps(source, "b", "c");
    expect(connected.steps[1].transitions).toEqual([
      { target_step_key: "c", condition: { kind: "ALWAYS" } },
    ]);
    expect(source.steps[1].transitions).toEqual([]);
    expect(connected.source_mapping).toEqual(source.source_mapping);
    const roundTrip = JSON.parse(JSON.stringify(connected));
    expect(checkGraph(roundTrip)).toEqual([]);
    const removed = removeTransition(roundTrip, "b", 0);
    expect(removed.steps[1].transitions).toEqual([]);
    expect(removed.steps[0].transitions).toEqual(source.steps[0].transitions);
    expect(connected.steps[1].transitions).toHaveLength(1);
  });
  it("places a conditional edge before the unconditional fallback used by first-match execution", () => {
    const source = graph();
    source.steps.push(step("c"));
    const condition = {
      kind: "EMPLOYEE",
      field: "employment_type",
      operator: "EQ",
      value: "OFFICE",
    };
    const connected = connectSteps(source, "a", "c", condition);
    expect(connected.steps[0].transitions).toEqual([
      { target_step_key: "c", condition },
      { target_step_key: "b" },
    ]);
    condition.value = "PRODUCTION";
    expect(connected.steps[0].transitions?.[0].condition.value).toBe("OFFICE");
    expect(() => connectSteps(source, "a", "c")).toThrow("безусловная");
  });
  it("rejects self, duplicate, unknown and cyclic edges instead of saving an unusable canvas", () => {
    const source = graph();
    expect(() => connectSteps(source, "a", "a")).toThrow("самим собой");
    expect(() => connectSteps(source, "a", "b")).toThrow("уже существует");
    expect(() => connectSteps(source, "b", "missing")).toThrow("не найден");
    expect(() => connectSteps(source, "b", "a")).toThrow("цикл");
    source.steps[1].kind = "END";
    expect(() => connectSteps(source, "b", "a")).toThrow("завершения");
    expect(() => removeTransition(source, "a", 5)).toThrow(
      "Выберите её заново",
    );
  });
  it("persists drag coordinates independently of executable transitions through a save and reload", () => {
    const source = graph();
    const moved = moveStep(source, "a", { x: -35.5, y: 222 });
    const reloaded = JSON.parse(JSON.stringify(moved));
    expect(reloaded.editor_layout.nodes).toEqual([
      { step_key: "a", x: -35.5, y: 222 },
    ]);
    expect(
      moveStep(reloaded, "a", { x: 90, y: 50 }).editor_layout.nodes,
    ).toEqual([{ step_key: "a", x: 90, y: 50 }]);
    expect(reloaded.steps).toEqual(source.steps);
    expect(reloaded.source_mapping).toEqual(source.source_mapping);
    expect(source.editor_layout).toBeUndefined();
    expect(() => moveStep(source, "a", { x: Number.NaN, y: 0 })).toThrow();
  });
  it("can remove a first block before any canvas coordinates have been persisted", () => {
    const source = graph();
    source.editor_layout = {};
    const removed = removeStep(source, "a");
    expect(removed.start_step_key).toBe("b");
    expect(removed.editor_layout.nodes).toEqual([]);
    expect(source.editor_layout).toEqual({});
  });
  it("requires updating a timed dependency before deleting the step it waits for", () => {
    const source = graph();
    source.steps[1].trigger = {
      type: "AFTER_STEP",
      after_step_key: "a",
      after_event: "STEP_COMPLETED",
      offset_minutes: 1,
    };
    expect(() => removeStep(source, "a")).toThrow("время запуска");
    expect(source.steps).toHaveLength(2);
    const broken = graph();
    broken.steps[1].transitions = [{ target_step_key: "a" }];
    expect(checkGraph(broken)).toContain(
      "Связи образуют цикл. Удалите обратную связь.",
    );
  });
});
const graph = (): Graph => ({
  schema_version: "korsik-scenario-v2",
  algorithm_version: "calendar-v1",
  start_step_key: "a",
  steps: [step("a", "b"), step("b")],
  source_mapping: [{ row_from: 3, step_keys: ["a", "b"] }],
});
describe("semantic insertion", () => {
  it("rewires a transition without mutating original identifiers and source mapping", () => {
    const source = graph(),
      g = insertStep(source, step("new"), "a");
    expect(g.steps.find((s) => s.step_key === "a")?.transitions).toEqual([
      { target_step_key: "new", condition: { kind: "ALWAYS" } },
    ]);
    expect(g.steps.find((s) => s.step_key === "new")?.transitions).toEqual([
      { target_step_key: "b" },
    ]);
    expect(source.steps[0].transitions).toEqual([{ target_step_key: "b" }]);
    expect(g.source_mapping).toEqual(source.source_mapping);
    expect(checkGraph(g)).toEqual([]);
  });
  it("inserts before entry and preserves downstream route", () => {
    const g = insertStep(graph(), step("new"), "a", true);
    expect(g.start_step_key).toBe("new");
    expect(g.steps[0].transitions).toEqual([
      { target_step_key: "a", condition: { kind: "ALWAYS" } },
    ]);
  });
  it("rejects missing anchors and detects broken references", () => {
    expect(() => insertStep(graph(), step("x"), "unknown")).toThrow();
    const g = graph();
    g.steps[0].transitions = [{ target_step_key: "missing" }];
    expect(checkGraph(g)).toHaveLength(1);
  });
  it("removes nodes regardless of their legacy mandatory flag and connects predecessor to successor", () => {
    const g = insertStep(graph(), step("new"), "a");
    expect(removeStep(g, "new").steps[0].transitions).toEqual([
      { target_step_key: "b", condition: { kind: "ALWAYS" } },
    ]);
    g.steps[1].mandatory = true;
    expect(removeStep(g, "new").steps.some((s) => s.step_key === "new")).toBe(
      false,
    );
  });
});

describe("simple message and answer branches", () => {
  const simple = (): Graph => ({
    ...graph(),
    editor_mode: "SIMPLE",
    steps: [
      step("welcome"),
      {
        ...step("yes"),
        kind: "ANSWER",
        answer: { label_ty: "Да", label_vy: "Да" },
      },
      {
        ...step("no"),
        kind: "ANSWER",
        answer: { label_ty: "Нет", label_vy: "Нет" },
      },
      { ...step("finish"), kind: "END" },
    ],
    start_step_key: "welcome",
  });
  it("creates generated, independent answer IDs without scheduled delivery", () => {
    const first = newStep("ANSWER"),
      second = newStep("ANSWER");
    expect(first.step_key).toMatch(/^hr_[0-9a-f]{8}$/);
    expect(second.step_key).not.toBe(first.step_key);
    expect(first.answer).toEqual({
      label_ty: "Новый ответ",
      label_vy: "Новый ответ",
    });
    expect(first.trigger).toBeUndefined();
    expect(newStep("MESSAGE").trigger).toEqual({ type: "AFTER_PREVIOUS" });
    expect(newStep("SURVEY").survey).toBeUndefined();
  });
  it("round-trips multiple answer buttons and independent continuations without mutating the source", () => {
    const source = simple();
    let connected = connectSteps(source, "welcome", "yes");
    connected = connectSteps(connected, "welcome", "no");
    connected = connectSteps(connected, "yes", "finish");
    connected = connectSteps(connected, "no", "finish");
    const saved = JSON.parse(JSON.stringify(connected));
    expect(checkGraph(saved)).toEqual([]);
    expect(
      saved.steps[0].transitions.map((t: any) => t.target_step_key),
    ).toEqual(["yes", "no"]);
    expect(saved.steps[1].transitions[0].target_step_key).toBe("finish");
    expect(source.steps[0].transitions).toEqual([]);
  });
  it("rejects mixing automatic delivery and answer choices or chaining answer blocks", () => {
    const source = simple();
    const choices = connectSteps(source, "welcome", "yes");
    expect(() => connectSteps(choices, "welcome", "finish")).toThrow(
      "только с блоками",
    );
    const automatic = connectSteps(source, "welcome", "finish");
    expect(() => connectSteps(automatic, "welcome", "yes")).toThrow(
      "только с блоками",
    );
    expect(() => connectSteps(source, "yes", "no")).toThrow(
      "только к сообщению",
    );
  });
  it("deletes one answer choice without turning its continuation into an automatic message edge", () => {
    let source = connectSteps(simple(), "welcome", "yes");
    source = connectSteps(source, "welcome", "no");
    source = connectSteps(source, "yes", "finish");
    const removed = removeStep(source, "yes");
    expect(removed.steps[0].transitions?.map((t) => t.target_step_key)).toEqual(
      ["no"],
    );
    expect(removed.steps.some((s) => s.step_key === "finish")).toBe(true);
    expect(source.steps[0].transitions).toHaveLength(2);
  });
});
