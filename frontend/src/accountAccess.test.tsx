import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

const state = vi.hoisted(() => ({
  role: "HEAD_TALENT",
  canDelete: true,
  canChange: true,
  accessRole: "EMPLOYEE",
}));
vi.mock("./App", () => ({
  useActor: () => ({ id: "head", role: state.role, scope: {} }),
}));
vi.mock("./ui", async (original) => ({
  ...(await original<typeof import("./ui")>()),
  useResource: (path: string | null) => ({
    data:
      path === "/hr-accounts"
        ? {
            items: [
              {
                id: "staff",
                name: "Мария",
                email: "hr@example.test",
                enabled: true,
                etag: '"user:1"',
                can_delete: state.canDelete,
              },
            ],
          }
        : path?.startsWith("/organization/employees/")
          ? {
              id: "employee",
              name: "Иван",
              version: 1,
              etag: '"employee:1"',
              can_edit: true,
              access_role: state.accessRole,
              can_change_access_role: state.canChange,
            }
          : { items: [] },
    loading: false,
    error: "",
    etag: '"employee:1"',
    reload: vi.fn(),
  }),
}));
import HrAccounts from "./pages/HrAccounts";
import { DirectoryEmployee } from "./pages/Organization";

beforeEach(() => {
  state.role = "HEAD_TALENT";
  state.canDelete = true;
  state.canChange = true;
  state.accessRole = "EMPLOYEE";
});

describe("explicit account administration", () => {
  it("offers HR deletion only to HEAD when the server grants the action", () => {
    expect(renderToStaticMarkup(<HrAccounts />)).toContain(
      "Удалить HR-менеджера: Мария",
    );
    state.role = "HR_ADMIN";
    expect(renderToStaticMarkup(<HrAccounts />)).not.toContain(
      "Удалить HR-менеджера",
    );
  });
  it("hides a protected account deletion even for HEAD", () => {
    state.canDelete = false;
    expect(renderToStaticMarkup(<HrAccounts />)).not.toContain(
      "Удалить HR-менеджера",
    );
  });
  it("offers the real Mini App manager role separately from reporting assignments", () => {
    const html = renderToStaticMarkup(
      <DirectoryEmployee
        id="employee"
        units={[]}
        employees={[]}
        canEdit
        onClose={() => {}}
        onOpen={() => {}}
        onSaved={() => {}}
      />,
    );
    expect(html).toContain("Сделать руководителем");
    expect(html).toContain("Изменить подразделение и руководителя");
  });
  it("allows explicit demotion of a current manager", () => {
    state.accessRole = "MANAGER";
    const html = renderToStaticMarkup(
      <DirectoryEmployee
        id="employee"
        units={[]}
        employees={[]}
        canEdit
        onClose={() => {}}
        onOpen={() => {}}
        onSaved={() => {}}
      />,
    );
    expect(html).toContain("Убрать роль руководителя");
    expect(html).not.toContain("Сделать руководителем");
  });
  it("never offers role mutation to read-only viewers", () => {
    const html = renderToStaticMarkup(
      <DirectoryEmployee
        id="employee"
        units={[]}
        employees={[]}
        canEdit={false}
        onClose={() => {}}
        onOpen={() => {}}
        onSaved={() => {}}
      />,
    );
    expect(html).not.toContain("Сделать руководителем");
  });
  it("honors the server refusal for protected HR roles", () => {
    state.canChange = false;
    state.accessRole = "HR_ADMIN";
    const html = renderToStaticMarkup(
      <DirectoryEmployee
        id="employee"
        units={[]}
        employees={[]}
        canEdit
        onClose={() => {}}
        onOpen={() => {}}
        onSaved={() => {}}
      />,
    );
    expect(html).not.toContain("Сделать руководителем");
    expect(html).not.toContain("Убрать роль руководителя");
  });
});
