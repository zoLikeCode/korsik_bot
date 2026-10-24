import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ResultView } from "./pages/Scenarios";

describe("server scenario simulation result", () => {
  it("renders validation and every actual timeline row, including typed trigger objects, dates and waiting states", () => {
    const response = {
      valid: true,
      errors: [],
      warnings: [{ path: "steps[1]", message: "Проверьте время ожидания" }],
      employee: { timezone: "Asia/Yekaterinburg" },
      simulation_only: true,
      outbound_messages_created: 0,
      timeline: [
        {
          step_key: "welcome",
          kind: "MESSAGE",
          due_at: "2026-10-24T07:00:00+00:00",
          status: "READY",
          reason: {
            type: "ANCHOR_TIME",
            anchor: "START_DATE",
            offset_days: 0,
            day_basis: "CALENDAR",
            local_time: "12:00",
          },
          missing_assets: [],
        },
        {
          step_key: "after_welcome",
          kind: "QUESTION",
          due_at: "2026-10-24T07:01:00+00:00",
          status: "READY",
          reason: {
            type: "AFTER_STEP",
            after_step_key: "welcome",
            after_event: "STEP_COMPLETED",
            offset_minutes: 1,
          },
          missing_assets: [],
        },
        {
          step_key: "wait_for_answer",
          kind: "SURVEY",
          due_at: null,
          status: "NOT_SELECTED_OR_WAITING_ANSWER",
          reason: {
            type: "AFTER_STEP",
            after_step_key: "after_welcome",
            offset_minutes: 0,
          },
          questions: [
            { question_key: "support_comment", comment_requested: true },
          ],
          missing_assets: ["source-picture"],
        },
      ],
    };
    const html = renderToStaticMarkup(<ResultView data={response} />);
    expect(html).toContain("Программа прошла серверную проверку");
    expect(html).toContain("welcome");
    expect(html).toContain("after_welcome");
    expect(html).toContain("wait_for_answer");
    expect(html).toContain("Блоков в схеме: 3");
    expect(html).toContain("После шага welcome · 1 мин.");
    expect(html).toContain('dateTime="2026-10-24T07:01:00+00:00"');
    expect(html).toContain("12:01");
    expect(html).toContain("Asia/Yekaterinburg");
    expect(html).toContain("Проверьте время ожидания");
    expect(html).toContain("Зависит от ответа сотрудника");
    expect(html).toContain("Неприкреплённых материалов: 1");
    expect(html).toContain("support_comment · запрошен комментарий");
    expect(html).not.toContain("[object Object]");
  });
  it("shows server errors and the empty simulation state without replacing them with raw JSON", () => {
    const html = renderToStaticMarkup(
      <ResultView
        data={{
          valid: false,
          errors: [{ path: "welcome", message: "Не задан календарь" }],
          warnings: [],
          timeline: [],
        }}
      />,
    );
    expect(html).toContain("В программе есть ошибки");
    expect(html).toContain("welcome: Не задан календарь");
    expect(html).toContain("Маршрут не построен");
    expect(html).not.toContain('class="json-view"');
  });
});
