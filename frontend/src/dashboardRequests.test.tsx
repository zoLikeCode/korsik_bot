import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
const state = vi.hoisted(() => ({
  role: "HR_ADMIN",
  paths: [] as (string | null)[],
}));
vi.mock("./App", () => ({
  useActor: () => ({ role: state.role, scope: { all: true } }),
}));
vi.mock("./ui", async () => {
  const actual = await vi.importActual<typeof import("./ui")>("./ui");
  return {
    ...actual,
    useResource: (path: string | null) => {
      state.paths.push(path);
      const data = path?.startsWith("/reports/risk-topics")
        ? {
            items: [
              { topic: "Пропуска", employee_count: 5, suppressed: false },
            ],
            minimum_group_size: 5,
          }
        : path === "/sites"
          ? { items: [] }
          : {
              summary: { employees: 8, adapting: 5, completed: 3 },
              problems: {
                items: [
                  { topic: "Пропуска", employee_count: 5, suppressed: false },
                ],
                minimum_group_size: 5,
              },
              employees: [{ name: "Do not render detailed roster" }],
            };
      return { data, loading: false, error: "", reload: () => {}, etag: null };
    },
  };
});
function render(role: string) {
  state.role = role;
  state.paths = [];
  return renderToStaticMarkup(
    <MemoryRouter>
      <Dashboard analytics />
    </MemoryRouter>,
  );
}
describe("compact dashboard request scope", () => {
  it("removes details and their modules while retaining overview and CSV", () => {
    const html = render("HR_ADMIN");
    for (const text of [
      "Подробные данные",
      "<details",
      "<table",
      "Do not render detailed roster",
    ])
      expect(html).not.toContain(text);
    expect(html).toContain("Работа HR");
    expect(html).toContain("Скачать CSV");
    expect(html).toContain(
      "/api/v1/analytics/insights/export?timezone=Europe%2FMoscow",
    );
    expect(state.paths.filter(Boolean)).toEqual([
      "/sites",
      expect.stringMatching(
        /^\/analytics\/insights\?timezone=Europe%2FMoscow&period_from=\d{4}-\d{2}-\d{2}$/,
      ),
    ]);
  });
  it("loads only visible scoped risk topics for HEAD_TALENT with the same filters", () => {
    const html = render("HEAD_TALENT");
    expect(html).toContain("Частые проблемы");
    expect(html).toContain("Пропуска");
    expect(state.paths.filter(Boolean)).toEqual([
      "/sites",
      expect.stringMatching(
        /^\/analytics\/insights\?timezone=Europe%2FMoscow&period_from=\d{4}-\d{2}-\d{2}$/,
      ),
    ]);
    expect(
      state.paths.some(
        (path) =>
          path?.startsWith("/reports/exports") || path?.includes("feedback"),
      ),
    ).toBe(false);
  });
  it("does not request risk topics or detail reports for a manager", () => {
    const html = render("MANAGER");
    expect(html).toContain("Моя команда");
    expect(html).not.toContain("Частые проблемы");
    expect(html).not.toContain("Подробные данные");
    expect(state.paths.filter(Boolean)).toEqual([
      "/sites",
      expect.stringMatching(
        /^\/analytics\/insights\?timezone=Europe%2FMoscow&period_from=\d{4}-\d{2}-\d{2}$/,
      ),
    ]);
  });
});
