import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  pagesForActor,
  homeForActor,
  canManagePersonalMax,
  MobileNavigationTrigger,
} from "./App";
import { conversationNeedsReply } from "./pages/Chat";

const routes = (role: string, scope: any = {}) =>
  pagesForActor({ role, scope }).map((page) => page.to);
describe("role-specific Mini App navigation", () => {
  it("keeps the employee in self-service even when the session carries broad scope", () => {
    expect(routes("EMPLOYEE", { all: true, employee_drilldown: true })).toEqual(
      ["/profile", "/chat", "/surveys", "/organization"],
    );
    expect(homeForActor({ role: "EMPLOYEE", scope: {} })).toBe("/profile");
    expect(
      pagesForActor({ role: "EMPLOYEE", scope: {} }).map((page) => page.title),
    ).toEqual(["Мой профиль", "Диалог", "Опросы", "Оргструктура"]);
  });
  it("keeps a manager in profile/team views and gives the talent head the HR workspace", () => {
    expect(routes("MANAGER")).toEqual([
      "/profile",
      "/dashboard",
      "/surveys",
      "/organization",
    ]);
    expect(routes("HEAD_TALENT")).toEqual([
      "/dashboard",
      "/employees",
      "/hr-accounts",
      "/organization",
      "/scenarios",
      "/knowledge",
      "/surveys",
      "/chat",
      "/broadcasts",
      "/analytics",
    ]);
    expect(homeForActor({ role: "MANAGER", scope: {} })).toBe("/profile");
    for (const path of routes("HR_SPECIALIST"))
      expect(routes("HEAD_TALENT")).toContain(path);
    expect(routes("HR_SPECIALIST")).not.toContain("/hr-accounts");
    expect(routes("HR_ADMIN")).toContain("/hr-accounts");
    expect(routes("MANAGER", { all: true })).not.toContain("/broadcasts");
    expect(routes("EMPLOYEE", { all: true })).not.toContain("/hr-accounts");
  });
  it("excludes operator and cases routes for every business role and grants no business view to unknown or platform roles", () => {
    for (const role of [
      "HR_ADMIN",
      "HR_SPECIALIST",
      "HEAD_TALENT",
      "MANAGER",
      "EMPLOYEE",
      "PLATFORM_ADMIN",
      "UNKNOWN",
    ]) {
      expect(routes(role, { all: true })).not.toContain("/cases");
      expect(routes(role, { all: true })).not.toContain("/settings");
    }
    expect(routes("PLATFORM_ADMIN", { all: true })).toEqual([]);
    expect(routes("UNKNOWN", { all: true })).toEqual([]);
    expect(homeForActor({ role: "PLATFORM_ADMIN", scope: { all: true } })).toBe(
      "/access",
    );
    expect(
      pagesForActor({ role: "HR_ADMIN", scope: { all: true } }).find(
        (page) => page.to === "/scenarios",
      )?.title,
    ).toBe("Конструктор сценариев");
  });
});

describe("neutral support handoff", () => {
  it("keeps an automatic unanswered case visible to HR even while free dialogue stays in BOT mode", () => {
    expect(
      conversationNeedsReply({
        mode: "BOT",
        active_handoff_case_id: "case-one",
      }),
    ).toBe(true);
    expect(conversationNeedsReply({ mode: "WAITING_HR" })).toBe(true);
    expect(
      conversationNeedsReply({ mode: "BOT", active_handoff_case_id: null }),
    ).toBe(false);
    expect(conversationNeedsReply({ mode: "HUMAN" })).toBe(false);
  });
});

it("exposes personal MAX binding only to staff roles without restoring Settings navigation", () => {
  for (const role of ["HR_ADMIN", "HR_SPECIALIST", "MANAGER", "HEAD_TALENT"])
    expect(canManagePersonalMax(role)).toBe(true);
  for (const role of ["EMPLOYEE", "PLATFORM_ADMIN", "UNKNOWN", ""])
    expect(canManagePersonalMax(role)).toBe(false);
});

it("keeps a standalone mobile navigation trigger linked to the sidebar without repeating the page title", () => {
  const closed = renderToStaticMarkup(
    <MobileNavigationTrigger open={false} onToggle={() => {}} />,
  );
  expect(closed).toContain('type="button"');
  expect(closed).toContain('aria-label="Открыть меню"');
  expect(closed).toContain('aria-controls="app-sidebar"');
  expect(closed).toContain('aria-expanded="false"');
  expect(closed).not.toContain("<header");
  const opened = renderToStaticMarkup(
    <MobileNavigationTrigger open onToggle={() => {}} />,
  );
  expect(opened).toContain('aria-label="Закрыть меню"');
  expect(opened).toContain('aria-expanded="true"');
});
