import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import AnalyticsDashboard, {
  Donut,
  Results,
  Trend,
} from "./components/AnalyticsDashboard";
import { analyticsDateRange } from "./analyticsDates";

describe("HR analytics dashboards", () => {
  it("renders observed metrics without employee names or raw responses", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <AnalyticsDashboard
          role="HR_ADMIN"
          onDepartment={() => {}}
          data={{
            summary: {
              employees: 12,
              adapting: 8,
              completed: 4,
              open_questions: 2,
              problem_conversations: 1,
              surveys_completed: 6,
              surveys_delivered: 10,
              completion_rate: 0.6,
              automatic: 8,
              handoff: 2,
              clarify: 0,
              pending: 0,
            },
            adaptation: { in_progress: 8, completed: 4 },
            employees: [{ name: "PRIVATE_PERSON" }],
            raw_answers: ["PRIVATE_ANSWER"],
          }}
        />
      </MemoryRouter>,
    );
    expect(html).toContain("60%");
    expect(html).toContain("80%");
    expect(html).toContain("Общая картина");
    expect(html).toContain("Опросы и обратная связь");
    expect(html).toContain("Поддержка и подразделения");
    expect(html).not.toContain("PRIVATE_PERSON");
    expect(html).not.toContain("PRIVATE_ANSWER");
    expect(html).toContain('href="/chat"');
  });
  it("shows honest empty charts instead of invented percentages", () => {
    expect(
      renderToStaticMarkup(<Donut label="сотрудников" rows={[]} />),
    ).toContain("Пока нет данных");
    const html = renderToStaticMarkup(
      <Trend data={[{ date: "2026-10-25", questions: 0 }]} />,
    );
    expect(html).toContain("ещё не задавали");
    expect(html).not.toContain("polyline");
  });
  it("does not display a suppressed or invalid small-group result even in a stale payload", () => {
    for (const extra of [
      { suppressed: true, respondents: 8 },
      { suppressed: false, respondents: 4 },
      { suppressed: false },
    ]) {
      const html = renderToStaticMarkup(
        <Results
          rows={[
            {
              key: "q",
              question: "Шкала",
              type: "NUMERIC",
              average: 4.91,
              maximum: 5,
              ...extra,
            },
          ]}
        />,
      );
      expect(html).toContain("минимум пяти");
      expect(html).not.toContain("4,91");
    }
  });
  it("keeps source scales and categories without clinical interpretation", () => {
    const html = renderToStaticMarkup(
      <Results
        rows={[
          {
            key: "q",
            survey: "Поддержка",
            question: "Поддержка наставника",
            type: "NUMERIC",
            minimum: 1,
            maximum: 10,
            average: 7.6,
            respondents: 9,
          },
        ]}
      />,
    );
    expect(html).toContain("7,6");
    expect(html).toContain("из 10");
    expect(html).not.toContain("диагноз");
  });
  it("uses local dates across month boundaries without the UTC day shift", () => {
    expect(analyticsDateRange(7, new Date("2026-10-24T22:30:00Z"))).toEqual({
      from: "2026-10-19",
      to: "2026-10-25",
    });
    expect(analyticsDateRange(30, new Date("2026-03-02T09:00:00Z"))).toEqual({
      from: "2026-02-01",
      to: "2026-03-02",
    });
  });
  it("renders finite trend geometry and a tooltip for each real observation", () => {
    const html = renderToStaticMarkup(
      <Trend
        data={[{ date: "2026-10-25", questions: 3, automatic: 2, handoff: 1 }]}
      />,
    );
    expect(html).toContain("2026-10-25");
    expect(html).toContain("Передано HR");
    expect(html).not.toMatch(/NaN|Infinity/);
  });
});
