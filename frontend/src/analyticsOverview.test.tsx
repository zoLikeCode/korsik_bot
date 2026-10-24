import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import AnalyticsOverview, { SurveyRate } from "./components/AnalyticsOverview";
import {
  displayAverage,
  observedCount,
  observedRatio,
} from "./analyticsOverview";
const data = {
  metrics: {
    employees: 12,
    active_employees: 8,
    preboarding: 2,
    open_cases: 3,
    open_blockers: 1,
    tasks_overdue: 0,
    surveys_declined: 1,
    surveys_missing: 2,
    completion: { numerator: 4, denominator: 8 },
    participation: { numerator: 6, denominator: 8 },
  },
  sites: [{ site_id: "a", name: "Площадка А", employees: 12 }],
  employees: [{ name: "Секретное личное имя" }],
};
function render(role: string, value: any = data) {
  return renderToStaticMarkup(
    <MemoryRouter>
      <AnalyticsOverview data={value} role={role} />
    </MemoryRouter>,
  );
}
describe("role-specific scoped dashboard presentation", () => {
  it("uses real counts and exact delivered-survey denominators", () => {
    const html = render("HR_SPECIALIST");
    expect(html).toContain("Работа HR");
    expect(html).toContain("50%");
    expect(html).toContain("75%");
    expect(html).toContain("4 из 8");
    expect(html).toContain("доставленным первым вопросом");
    expect(html).toContain("Текущее состояние, независимо от периода анкет");
    expect(html).toContain('href="/chat"');
  });
  it("gives head HR an aggregate site view without personal names or chat access", () => {
    const html = render("HEAD_TALENT");
    expect(html).toContain("Общая картина адаптации");
    expect(html).toContain("Площадка А");
    expect(html).not.toContain("Секретное личное имя");
    expect(html).not.toContain('href="/chat"');
    expect(html).toContain("не рейтинг площадок");
  });
  it("gives managers their team view and personal-task caption without HR-only links", () => {
    const html = render("MANAGER");
    expect(html).toContain("Моя команда");
    expect(html).toContain("Ваши назначенные задачи");
    expect(html).not.toContain('href="/chat"');
    expect(html).not.toContain("Сотрудники по площадкам");
  });
  it("does not manufacture zeroes, percentages, or an alarming status when data is absent", () => {
    const html = render("HR_ADMIN", {});
    expect(html).toContain("Нет данных");
    expect(html).not.toContain("0%");
    expect(html).not.toContain("Нужно внимание");
    expect(html).toContain("Пока нет доставленных анкет");
    expect(observedCount(undefined)).toBeNull();
    expect(observedCount("4")).toBeNull();
    expect(observedCount(-1)).toBeNull();
    expect(observedCount(0)).toBe(0);
  });
  it("does not turn a zero denominator, malformed ratio, or suppressed result into a bar", () => {
    for (const ratio of [
      { numerator: 0, denominator: 0 },
      { numerator: 6, denominator: 5 },
      { numerator: 1, denominator: 3, suppressed: true },
      { numerator: 1, denominator: NaN },
    ]) {
      expect(observedRatio(ratio)).toBeNull();
      expect(
        renderToStaticMarkup(
          <SurveyRate title="Завершены" value={ratio} caption="Анкеты" />,
        ),
      ).not.toContain('role="progressbar"');
    }
    expect(observedRatio({ numerator: 0, denominator: 4 })?.percent).toBe(0);
  });
  it("never displays a suppressed mean even if a stale response also contains its value", () => {
    expect(displayAverage({ suppressed: true, average: 4.777 })).toBe(
      "Скрыто: меньше 5 участников",
    );
    expect(displayAverage({ average: null })).toBe("Пока нет ответов");
    expect(displayAverage({ average: 4.25 })).toBe("4.25");
  });
  it("does not expose dashboards to the employee page", () => {
    expect(render("EMPLOYEE")).toBe("");
  });
});

describe("frequent problem topic privacy", () => {
  it("sorts real aggregates without exposing suppressed labels or counts", async () => {
    const { default: FrequentProblems } = await import(
      "./components/FrequentProblems"
    );
    const { frequentProblemTopics } = await import("./analyticsOverview");
    const payload = {
      minimum_group_size: 5,
      pending_analysis_count: 2,
      items: [
        { topic: "Пропуск", employee_count: 5, suppressed: false },
        { topic: "Рабочее место", employee_count: 9, suppressed: false },
        { topic: "Секретная малая тема", employee_count: 4, suppressed: true },
        { topic: "Скрытое старое имя", employee_count: 99, suppressed: true },
      ],
    };
    const original = JSON.stringify(payload);
    const html = renderToStaticMarkup(<FrequentProblems data={payload} />);
    expect(html.indexOf("Рабочее место")).toBeLessThan(html.indexOf("Пропуск"));
    expect(html).toContain("9 сотрудников");
    expect(html).not.toContain("Секретная малая тема");
    expect(html).not.toContain("Скрытое старое имя");
    expect(html).not.toContain("99 сотрудников");
    expect(html).toContain("Ожидают оценки HR: 2");
    expect(html).toContain("Один сотрудник может входить в несколько тем");
    expect(frequentProblemTopics(payload).topics).toHaveLength(2);
    expect(JSON.stringify(payload)).toBe(original);
  });
  it("fails closed for malformed counts and a stale response below the minimum", async () => {
    const { frequentProblemTopics } = await import("./analyticsOverview");
    const result = frequentProblemTopics({
      minimum_group_size: 1,
      items: [
        { topic: "Hidden A", employee_count: 4, suppressed: false },
        { topic: "Hidden B", employee_count: "8", suppressed: false },
        { topic: "Hidden C", employee_count: NaN },
      ],
    });
    expect(result.topics).toEqual([]);
    expect(result.minimum).toBe(5);
    expect(result.hidden).toBe(true);
  });
  it("shows honest empty/error/loading states and never stale scoped topics", async () => {
    const { default: FrequentProblems } = await import(
      "./components/FrequentProblems"
    );
    expect(
      renderToStaticMarkup(
        <FrequentProblems data={{ items: [], pending_analysis_count: 0 }} />,
      ),
    ).toContain("Пока нет подтверждённых тем");
    const data = { items: [{ topic: "Old scope secret", employee_count: 7 }] };
    for (const props of [{ error: "Доступ изменился" }, { loading: true }])
      expect(
        renderToStaticMarkup(<FrequentProblems data={data} {...props} />),
      ).not.toContain("Old scope secret");
    const suppressed = renderToStaticMarkup(
      <FrequentProblems
        data={{
          items: [{ topic: null, employee_count: null, suppressed: true }],
        }}
      />,
    );
    expect(suppressed).toContain("Недостаточно данных для показа тем");
    expect(suppressed).not.toContain("Пока нет подтверждённых тем");
  });
  it("limits the view to five visible topics without summing overlapping employees", async () => {
    const { frequentProblemTopics } = await import("./analyticsOverview");
    const result = frequentProblemTopics({
      items: Array.from({ length: 7 }, (_, i) => ({
        topic: `Topic${i}`,
        employee_count: 5 + i,
      })),
    });
    expect(result.topics).toHaveLength(5);
    expect(result.topics[0]).toEqual({ topic: "Topic6", employeeCount: 11 });
    expect(result.visibleTopics).toBe(7);
  });
});
