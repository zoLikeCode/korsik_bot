import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  moveSurveyQuestion,
  surveyActionReason,
  surveyDraftBody,
} from "./surveyEditing";
import { Editor, SurveyQuestionOrder } from "./pages/Surveys";
vi.mock("./App", () => ({
  useActor: () => ({ role: "HR_ADMIN", scope: { all: true } }),
}));

describe("compact survey editor", () => {
  it("retains hidden wording from an old version while changing visible questions", () => {
    const old = Object.freeze({
      description: "Старое пояснение сотрудникам",
      thank_you: "Индивидуальное завершение",
    });
    const questions = [{ question_key: "q2", prompt: "Новый вопрос" }];
    const body = surveyDraftBody(
      { title: "Редакция", purpose: "ADAPTATION", questions },
      old,
    );
    expect(body.description).toBe(old.description);
    expect(body.thank_you).toBe(old.thank_you);
    expect(body.questions).toBe(questions);
    expect(body.reason.length).toBeGreaterThanOrEqual(3);
    expect(old).toEqual({
      description: "Старое пояснение сотрудникам",
      thank_you: "Индивидуальное завершение",
    });
  });
  it("uses explicit safe defaults for a new survey without asking HR for hidden text", () => {
    const body = surveyDraftBody({
      title: "Опрос",
      purpose: "CUSTOM",
      questions: [],
    });
    expect(body.description).toBe("");
    expect(body.thank_you).toBe("Спасибо! Ваши ответы сохранены.");
  });
  it("renders Save and both arrow buttons without the three removed fields", () => {
    const html = renderToStaticMarkup(
      <Editor id="new" onClose={() => {}} onDone={() => {}} />,
    );
    for (const text of [
      "Пояснение сотруднику",
      "Сообщение после завершения",
      "Основание действия",
      "Сохранить черновик",
    ])
      expect(html).not.toContain(text);
    expect(html).toContain("Сохранить</button>");
    expect(html).toContain('aria-label="Поднять вопрос 1"');
    expect(html).toContain('aria-label="Опустить вопрос 1"');
    expect(html).toContain("Любой вопрос можно пропустить");
  });
  it("moves a question down and back up without changing its identity or source list", () => {
    const first = Object.freeze({ question_key: "a", options: ["Yes", "No"] });
    const second = Object.freeze({ question_key: "b", options: [] });
    const source = [first, second];
    const down = moveSurveyQuestion(source, 0, 1);
    expect(down).toEqual([second, first]);
    expect(source).toEqual([first, second]);
    expect(moveSurveyQuestion(down, 1, -1)).toEqual(source);
    expect(down[1]).toBe(first);
  });
  it("keeps boundary moves inert and disables boundary arrows", () => {
    const source = [1, 2];
    expect(moveSurveyQuestion(source, 0, -1)).toBe(source);
    expect(moveSurveyQuestion(source, 1, 1)).toBe(source);
    const first = renderToStaticMarkup(
      <SurveyQuestionOrder index={0} count={2} onMove={() => {}} />,
    );
    expect(first).toMatch(/disabled=""[^>]*aria-label="Поднять вопрос 1"/);
    expect(first).not.toMatch(/disabled=""[^>]*aria-label="Опустить вопрос 1"/);
    const last = renderToStaticMarkup(
      <SurveyQuestionOrder index={1} count={2} onMove={() => {}} />,
    );
    expect(last).toMatch(/disabled=""[^>]*aria-label="Опустить вопрос 2"/);
  });
  it("records each actual UI lifecycle action without pretending a save is an approval", () => {
    for (const action of [
      "save",
      "submit-review",
      "APPROVE",
      "REJECT",
      "publish",
      "version",
    ])
      expect(surveyActionReason(action).length).toBeGreaterThanOrEqual(3);
    expect(surveyActionReason("save")).not.toContain("Согласование");
    expect(surveyActionReason("APPROVE")).toContain("Согласование");
    expect(surveyActionReason("REJECT")).toContain("доработку");
  });
});
