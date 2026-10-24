import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ScenarioSettings,
  StepScheduleFields,
  StepInspector,
  CustomFormulations,
  ScenarioCard,
  ScenarioNodeLabel,
  scenarioSimulationChoices,
} from "./pages/Scenarios";
import {
  newStep,
  answerWithLabel,
  simulationAnswerChoice,
  simulationAnswerValue,
  type Graph,
} from "./scenario";
const graph = (): Graph => ({
  schema_version: "korsik-scenario-v2",
  algorithm_version: "calendar-v1",
  editor_mode: "SIMPLE",
  start_step_key: "welcome",
  steps: [],
  source_mapping: [],
  start_policy: { condition: "ON_BOT_START", timezone: "Europe/Moscow" },
  resume_policy: { reminder_hours: 24 },
});

describe("simple scenario editing contract", () => {
  it("offers four global start conditions and hides date controls until a scheduled launch", () => {
    const initial = graph();
    const html = renderToStaticMarkup(
      <ScenarioSettings graph={initial} locked={false} onChange={() => {}} />,
    );
    expect(html.match(/<option/g)).toHaveLength(4);
    for (const label of ["При запуске", "Дата выхода", "По выбранному времени", "Повторять"])
      expect(html).toContain(label);
    expect(html).not.toContain('type="date"');
    expect(html).toContain('min="1" max="168"');
    initial.start_policy = {
      condition: "SCHEDULED",
      date: "2026-11-05",
      time: "12:30",
      timezone: "Europe/Moscow",
    };
    const scheduled = renderToStaticMarkup(
      <ScenarioSettings graph={initial} locked={false} onChange={() => {}} />,
    );
    expect(scheduled).toMatch(/<input[^>]*type="date"[^>]*value="2026-11-05"/);
    expect(scheduled).toMatch(/<input[^>]*type="time"[^>]*value="12:30"/);
    expect(initial.start_policy.timezone).toBe("Europe/Moscow");
  });
  it("disables offsets and clock when the next block follows the preceding block", () => {
    const html = renderToStaticMarkup(
      <StepScheduleFields
        trigger={{ type: "AFTER_PREVIOUS" }}
        locked={false}
        onChange={() => {}}
      />,
    );
    expect(html.match(/<input/g)).toHaveLength(2);
    expect(html.match(/disabled=""/g)).toHaveLength(2);
    for (const removed of [
      "Сдвиг, месяцев",
      "Предшествующий шаг",
      "Событие",
      "Опорная дата",
      "Считать дни",
    ])
      expect(html).not.toContain(removed);
    const offset = renderToStaticMarkup(
      <StepScheduleFields
        trigger={{ type: "ANCHOR_TIME", offset_days: -3, local_time: "09:30" }}
        locked={false}
        onChange={() => {}}
      />,
    );
    expect(offset).not.toContain('disabled=""');
    expect(offset).toContain('value="-3"');
    expect(offset).toContain('value="09:30"');
  });
  it("renders a generated block ID as text and a single message text field without obsolete inspector controls", () => {
    const step = newStep("MESSAGE");
    const html = renderToStaticMarkup(
      <StepInspector
        step={step}
        graph={graph()}
        locked={false}
        onChange={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(html).toContain(`ID: ${step.step_key}`);
    expect(html).not.toContain(`value="${step.step_key}"`);
    expect(html).toContain("Текст сообщения");
    expect(html).toMatch(/<textarea[^>]*maxLength="3900"/i);
    for (const removed of [
      "Обязательный шаг",
      "Кому применим шаг",
      "Тип сотрудника",
      "Следующие шаги",
      "Условие перехода",
      "Опубликованное меню",
      "Выберите кнопку",
      "обращение на",
    ])
      expect(html).not.toContain(removed);
  });
  it("allows a message without extra formulations and preserves explicit query text when present", () => {
    const empty = renderToStaticMarkup(
      <CustomFormulations value={[]} locked={false} onChange={() => {}} />,
    );
    expect(empty).not.toContain("<input");
    expect(empty).toContain("Добавить формулировку");
    const prompts = [
      {
        id: "lunch",
        label: "Где пообедать?",
        query_text: "Где находится столовая?",
      },
    ];
    const html = renderToStaticMarkup(
      <CustomFormulations value={prompts} locked={false} onChange={() => {}} />,
    );
    expect(html).toContain("Где пообедать?");
    expect(html).toContain("Где находится столовая?");
    expect(html).toMatch(/<input[^>]*maxLength="80"/i);
  });
  it("shows answer labels without inventing an independent schedule", () => {
    const answer = newStep("ANSWER");
    answer.answer = { label_ty: "Покажи маршрут", label_vy: "Покажи маршрут" };
    const html = renderToStaticMarkup(<ScenarioNodeLabel step={answer} />);
    expect(html).toContain("Покажи маршрут");
    expect(html).not.toContain("flow-node-schedule");
    const inspector = renderToStaticMarkup(
      <StepInspector
        step={answer}
        graph={graph()}
        locked={false}
        onChange={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(inspector).toContain("Текст ответа");
    expect(inspector).toMatch(/<input[^>]*maxLength="80"/i);
    expect(inspector).not.toContain("Сдвиг по дням");
  });
  it("persists the free-answer flag when its button label changes and renders the selected mode", () => {
    const answer = newStep("ANSWER");
    answer.answer = {
      label_ty: "Свой ответ",
      label_vy: "Свой ответ",
      free_text: true,
    };
    const renamed = answerWithLabel(answer.answer, "Расскажу своими словами");
    expect(renamed).toEqual({
      label_ty: "Расскажу своими словами",
      label_vy: "Расскажу своими словами",
      free_text: true,
    });
    expect(answer.answer.label_ty).toBe("Свой ответ");
    const inspector = renderToStaticMarkup(
      <StepInspector
        step={{ ...answer, answer: renamed }}
        graph={graph()}
        locked={false}
        onChange={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(inspector).toContain("Свободный ответ");
    expect(inspector).toMatch(/type="checkbox"[^>]*checked=""/);
  });
  it("simulation waits for an explicit free text while fixed answers remain ordinary choices", () => {
    const free = newStep("ANSWER");
    free.answer = {
      label_ty: "Свой ответ",
      label_vy: "Свой ответ",
      free_text: true,
    };
    const pending = simulationAnswerChoice(free);
    expect(pending).toEqual({ value: free.step_key, free_text: "" });
    expect(simulationAnswerValue(pending)).toBe(free.step_key);
    expect(simulationAnswerChoice(newStep("ANSWER"))).toEqual(
      expect.any(String),
    );
    expect(simulationAnswerValue({ ...pending, free_text: "Мой ответ" })).toBe(
      free.step_key,
    );
    expect(simulationAnswerChoice(undefined)).toBe("");
  });
  it("renders scenario cards with status and launch condition", () => {
    const html = renderToStaticMarkup(
      <ScenarioCard
        scenario={{
          id: "demo",
          name: "Первый день",
          latest_version_status: "DRAFT",
          start_policy: { condition: "START_DATE" },
          updated_at: "2026-10-24T10:00:00Z",
        }}
        onOpen={() => {}}
      />,
    );
    expect(html).toContain("Первый день");
    expect(html).toContain("Не опубликован");
    expect(html).not.toContain("Черновик");
    expect(html).toContain("Условие запуска: Дата выхода");
  });
  it("exposes message answer choices to simulation without treating RAG formulations as scenario branches", () => {
    const source = graph();
    const message = {
      ...newStep("MESSAGE"),
      step_key: "message",
      content: {
        text_ty: "Нужен маршрут?",
        custom_prompts: [
          { id: "lunch", label: "Обед", query_text: "Где пообедать?" },
        ],
      },
      transitions: [{ target_step_key: "yes" }, { target_step_key: "no" }],
    };
    source.steps = [
      message,
      {
        ...newStep("ANSWER"),
        step_key: "yes",
        answer: { label_ty: "Да", label_vy: "Да" },
      },
      {
        ...newStep("ANSWER"),
        step_key: "no",
        answer: { label_ty: "Нет", label_vy: "Нет" },
      },
    ];
    const choices = scenarioSimulationChoices(source);
    expect(choices).toHaveLength(1);
    expect(choices[0].message.step_key).toBe("message");
    expect(choices[0].answers.map((answer) => answer.step_key)).toEqual([
      "yes",
      "no",
    ]);
    expect(source.steps[0].content.custom_prompts).toHaveLength(1);
  });
});
