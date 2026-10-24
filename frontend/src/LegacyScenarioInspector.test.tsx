import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  LegacyStepInspector,
  legacyNewStep,
  legacyNodeKinds,
} from "./LegacyScenarioInspector";
import { type Graph, type Step } from "./scenario";

function view(step: Step, locked = false) {
  const graph: Graph = {
    schema_version: "korsik-scenario-v2",
    algorithm_version: "calendar-v1",
    start_step_key: step.step_key,
    source_mapping: [],
    steps: [step],
  };
  return renderToStaticMarkup(
    <LegacyStepInspector
      step={step}
      graph={graph}
      sites={[]}
      locked={locked}
      onChange={() => {}}
      onDelete={() => {}}
    />,
  );
}
describe("native legacy draft editing", () => {
  it("creates calendar-v1 blocks with native inline survey definitions instead of SIMPLE references", () => {
    expect(legacyNodeKinds).toContain("QUESTION");
    expect(legacyNodeKinds).not.toContain("ANSWER");
    const survey = legacyNewStep("SURVEY");
    expect(survey.survey.start_question_key).toBe(
      survey.survey.questions[0].question_key,
    );
    expect(survey.survey.questions[0].prompt.text_ty).toBeTruthy();
    expect(survey.survey.reminders.anchor).toBe("FIRST_QUESTION_ACCEPTED");
    expect(survey.survey_template_version_id).toBeUndefined();
    expect(survey.trigger.type).toBe("ANCHOR_TIME");
    expect(legacyNewStep("QUESTION").question.type).toBe("TEXT");
    expect(legacyNewStep("WAIT_SIGNAL").trigger.type).toBe("SIGNAL");
    expect(legacyNewStep("HR_TASK").task.assignee_kind).toBe("ASSIGNED_HR");
  });
  it("renders existing question and work-calendar parameters without converting or mutating their data", () => {
    const step = legacyNewStep("QUESTION");
    step.step_key = "xls-r3";
    step.question.prompt = {
      text_ty: "Как к вам обращаться?",
      text_vy: "Как к вам обращаться?",
    };
    step.trigger = {
      ...step.trigger,
      day_basis: "WORKING",
      workday_number: 3,
      local_time: "12:30",
      anchor: "PROBATION_END",
    };
    const before = structuredClone(step);
    const html = view(step);
    expect(html).toContain("Как к вам обращаться?");
    expect(html).toContain("ID: xls-r3");
    expect(html).toContain("12:30");
    expect(html).toContain("Порядковый рабочий день");
    expect(html).not.toContain("Обязательный шаг");
    expect(html).not.toContain("Кому применим шаг");
    expect(step).toEqual(before);
  });
  it("renders every inline survey question and keeps the whole inspector locked for immutable versions", () => {
    const step = legacyNewStep("SURVEY");
    step.survey.questions.push({
      ...step.survey.questions[0],
      question_key: "second",
      prompt: { text_ty: "Второй вопрос", text_vy: "Второй вопрос" },
    });
    const html = view(step, true);
    expect(html).toContain("Второй вопрос");
    expect(html).toContain('<fieldset disabled=""');
    expect(html).toContain("Условия переходов");
    expect(html).not.toContain("Выберите опубликованный опрос");
  });
});
