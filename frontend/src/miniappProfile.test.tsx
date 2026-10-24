import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

const state = vi.hoisted(() => ({
  role: "EMPLOYEE",
  paths: [] as (string | null)[],
  error: "",
  loading: false,
}));
vi.mock("./App", () => ({
  useActor: () => ({
    role: state.role,
    scope: { site_ids: ["private-site-id"] },
  }),
}));
vi.mock("./ui", async (original) => ({
  ...(await original<typeof import("./ui")>()),
  useResource: (path: string | null) => {
    state.paths.push(path);
    return {
      data: path === "/me/profile" ? null : { items: [] },
      error: state.error,
      loading: state.loading,
      reload: vi.fn(),
    };
  },
}));
import Profile, {
  ProfileContent,
  profileDate,
  type PersonalProfile,
} from "./pages/Profile";
import HrAccounts, { InvitationStatus, scopeCaption } from "./pages/HrAccounts";

const data: PersonalProfile = {
  user: {
    id: "u",
    name: "Иван Соколов",
    email: "ivan@example.test",
    role: "EMPLOYEE",
  },
  employee: {
    id: "e",
    name: "Иван Соколов",
    email: "ivan@example.test",
    department: "Команда поддержки",
    profile_photo_url: "/api/v1/profiles/e/photo",
    start_date: "2026-10-25",
    probation_end_date: "2027-01-25",
  },
  manager: {
    id: "m",
    name: "Анна Иванова",
    position: "Руководитель команды",
    profile_photo_url: "/api/v1/profiles/m/photo",
  },
  direct_reports: [],
};
beforeEach(() => {
  state.role = "EMPLOYEE";
  state.paths = [];
  state.error = "";
  state.loading = false;
});
describe("personal and team Mini App profiles", () => {
  it("shows real self identity, dates and the actual manager photo", () => {
    const html = renderToStaticMarkup(
      <ProfileContent data={data} managerView={false} />,
    );
    for (const text of [
      "Иван Соколов",
      "ivan@example.test",
      "Команда поддержки",
      "25 октября 2026",
      "25 января 2027",
      "Анна Иванова",
      "/api/v1/profiles/e/photo",
      "/api/v1/profiles/m/photo",
    ])
      expect(html).toContain(text);
    expect(html).not.toContain("Моя команда");
  });
  it("marks only a current adapting team member and never turns employment dates into a badge", () => {
    const html = renderToStaticMarkup(
      <ProfileContent
        managerView
        data={{
          ...data,
          direct_reports: [
            {
              id: "a",
              name: "Новичок",
              profile_photo_url: "/api/v1/profiles/a/photo",
              is_adapting: true,
            },
            {
              id: "b",
              name: "Коллега",
              profile_photo_url: "/api/v1/profiles/b/photo",
              probation_end_date: "2027-12-31",
              is_adapting: false,
            },
          ],
        }}
      />,
    );
    expect(html).toContain("Моя команда");
    expect(html).toContain("Новичок");
    expect(html).toContain("Коллега");
    expect(html.match(/Проходит адаптацию/g)).toHaveLength(1);
    expect(html).not.toContain("Окончание испытательного срока");
  });
  it("uses the real user identity for an unlinked manager and an honest empty team", () => {
    const html = renderToStaticMarkup(
      <ProfileContent managerView data={{ ...data, employee: null }} />,
    );
    expect(html).toContain("ivan@example.test");
    expect(html).toContain("Кадровая карточка пока не связана");
    expect(html).toContain("Подчинённые пока не назначены");
    expect(html).not.toContain("Команда поддержки");
  });
  it("does not invent a manager, dates or an image when data is absent", () => {
    const html = renderToStaticMarkup(
      <ProfileContent
        managerView={false}
        data={{
          ...data,
          employee: {
            name: "Иван",
            profile_photo_url: "https://untrusted.invalid/photo",
          },
          manager: null,
        }}
      />,
    );
    expect(html).toContain("Руководитель пока не назначен");
    expect(html).not.toContain("untrusted.invalid");
    expect(html).toContain("Не указана");
    expect(profileDate("2026-02-30")).toBe("Не указана");
    expect(profileDate("invalid")).toBe("Не указана");
  });
  it("never requests the personal endpoint for a different role", () => {
    state.role = "HR_SPECIALIST";
    expect(renderToStaticMarkup(<Profile />)).toBe("");
    expect(state.paths).toEqual([null]);
  });
});
describe("HR account invitation workspace", () => {
  it("does not request administrative accounts for an ordinary HR, manager or employee", () => {
    for (const role of [
      "HR_SPECIALIST",
      "MANAGER",
      "EMPLOYEE",
      "PLATFORM_ADMIN",
    ]) {
      state.role = role;
      state.paths = [];
      expect(renderToStaticMarkup(<HrAccounts />)).toBe("");
      expect(state.paths).toEqual([null, null]);
    }
  });
  it("gives the talent head account invitations without displaying scope UUIDs or password fields", () => {
    state.role = "HEAD_TALENT";
    const html = renderToStaticMarkup(<HrAccounts />);
    expect(state.paths).toEqual(["/hr-accounts", "/hr-accounts/invitations"]);
    expect(html).toContain("Создать HR-менеджера");
    expect(html).not.toContain('type="password"');
    expect(html).not.toContain("private-site-id");
    expect(scopeCaption({ all: true })).toBe("Вся область организации");
    expect(scopeCaption({ site_ids: ["s"] })).toBe(
      "В пределах вашей области ответственности",
    );
  });
  it("keeps revoked and expired invitations distinct from connected accounts", () => {
    expect(
      renderToStaticMarkup(<InvitationStatus value="REVOKED" />),
    ).toContain("Приглашение отозвано");
    expect(
      renderToStaticMarkup(<InvitationStatus value="EXPIRED" />),
    ).toContain("Срок истёк");
    expect(
      renderToStaticMarkup(<InvitationStatus value="CONSUMED" />),
    ).toContain("Подключён");
  });
});
