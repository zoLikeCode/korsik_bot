import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  organizationTree,
  organizationDescendants,
  organizationUnitPath,
  unassignedEmployees,
  filterOrganizationTree,
  organizationManagerCandidates,
  organizationAssignmentPayload,
  organizationUnitPatchPayload,
  organizationUnitCompatible,
  organizationEmployeeUrl,
  organizationSiteRequired,
  organizationWritableSites,
  type OrganizationUnit,
  type OrganizationEmployee,
} from "./organization";
import {
  OrganizationPerson,
  DirectoryRelationships,
} from "./pages/Organization";
const units: OrganizationUnit[] = [
  {
    id: "direction",
    title: "Производство",
    type: "DIRECTION",
    legal_entity_id: "legal",
    head_employee_id: "boss",
  },
  {
    id: "department",
    title: "Контроль качества",
    type: "DEPARTMENT",
    parent_id: "direction",
    legal_entity_id: "legal",
  },
  {
    id: "team",
    title: "Лаборатория",
    type: "TEAM",
    parent_id: "department",
    legal_entity_id: "legal",
    site_id: "plant",
  },
];
const employees: OrganizationEmployee[] = [
  {
    id: "boss",
    name: "Анна Директор",
    org_unit_id: "direction",
    legal_entity_id: "legal",
    position: "Директор",
  },
  {
    id: "manager",
    name: "Борис Руководитель",
    org_unit_id: "department",
    reports_to_employee_id: "boss",
    manager_employee_id: "boss",
    legal_entity_id: "legal",
  },
  {
    id: "worker",
    name: "Вера Лаборант",
    org_unit_id: "team",
    reports_to_employee_id: "manager",
    manager_employee_id: "manager",
    legal_entity_id: "legal",
    site_id: "plant",
    position: "Инженер",
  },
  { id: "legacy", name: "Глеб Без отдела", department: "Архивное название" },
];
describe("organization directory relationships", () => {
  it("builds nested direction/department/team membership without mutating source records", () => {
    const before = JSON.stringify({ units, employees });
    const roots = organizationTree(units, employees);
    expect(roots).toHaveLength(1);
    expect(roots[0].members.map((e) => e.id)).toEqual(["boss"]);
    expect(roots[0].children[0].children[0].members.map((e) => e.id)).toEqual([
      "worker",
    ]);
    expect(organizationUnitPath(units, "team").map((u) => u.id)).toEqual([
      "direction",
      "department",
      "team",
    ]);
    expect(JSON.stringify({ units, employees })).toBe(before);
  });
  it("keeps legacy and missing-unit employees visible as unassigned", () => {
    const rows = [
      ...employees,
      { id: "orphan", name: "Олег", org_unit_id: "missing" },
    ];
    expect(unassignedEmployees(units, rows).map((e) => e.id)).toEqual([
      "legacy",
      "orphan",
    ]);
  });
  it("keeps a matching employee's ancestor path during search", () => {
    const roots = filterOrganizationTree(
      organizationTree(units, employees),
      "вЕрА",
    );
    expect(roots[0].unit.id).toBe("direction");
    expect(roots[0].members).toEqual([]);
    expect(roots[0].children[0].children[0].members[0].id).toBe("worker");
    expect(
      filterOrganizationTree(organizationTree(units, employees), "неизвестный"),
    ).toEqual([]);
    expect(
      filterOrganizationTree(
        organizationTree(units, employees),
        "Лаборатория",
      )[0].children[0].children[0].members,
    ).toHaveLength(1);
  });
  it("excludes descendants as new parents and the employee's own reports as managers", () => {
    expect([...organizationDescendants(units, "department")]).toEqual([
      "department",
      "team",
    ]);
    expect(
      organizationManagerCandidates(employees, "boss").map((e) => e.id),
    ).toEqual(["legacy"]);
    expect(
      organizationManagerCandidates(employees, "worker").map((e) => e.id),
    ).toContain("manager");
    expect(
      organizationManagerCandidates(employees, "worker").map((e) => e.id),
    ).not.toContain("worker");
  });
  it("terminates on malformed historical cycles while still exposing each unit exactly once", () => {
    const cyclic: OrganizationUnit[] = [
      { id: "a", title: "А", type: "TEAM", parent_id: "b" },
      { id: "b", title: "Б", type: "TEAM", parent_id: "a" },
    ];
    const roots = organizationTree(cyclic, []);
    const ids: string[] = [];
    const collect = (nodes: ReturnType<typeof organizationTree>) => {
      for (const node of nodes) {
        ids.push(node.unit.id);
        collect(node.children);
      }
    };
    collect(roots);
    expect(ids.sort()).toEqual(["a", "b"]);
    expect(organizationUnitPath(cyclic, "a")).toHaveLength(2);
  });
  it("restricts assignment options by legal entity and site and never writes the authorization manager_id", () => {
    expect(organizationUnitCompatible(units[2], employees[2])).toBe(true);
    expect(
      organizationUnitCompatible(
        { ...units[2], site_id: "other" },
        employees[2],
      ),
    ).toBe(false);
    expect(
      organizationUnitCompatible(
        { ...units[1], legal_entity_id: "other" },
        employees[2],
      ),
    ).toBe(false);
    const payload = organizationAssignmentPayload("team", "manager", {});
    expect(payload).toEqual({
      org_unit_id: "team",
      reports_to_employee_id: "manager",
    });
    expect(payload).not.toHaveProperty("manager_id");
    expect(
      organizationAssignmentPayload("", "", {
        org_unit_id: "old-unit",
        reports_to_employee_id: "old-manager",
      }),
    ).toEqual({
      org_unit_id: null,
      reports_to_employee_id: null,
    });
    expect(organizationEmployeeUrl("a&b")).toBe("/organization?employee=a%26b");
  });
  it("renders safe employee identity and clickable hierarchy entries without exposing unrelated employee fields", () => {
    const extra = {
      ...employees[2],
      email: "private@example.test",
      max_user_id: "private-max",
      mood: "private-mood",
    };
    const html = renderToStaticMarkup(
      <OrganizationPerson employee={extra} onOpen={() => {}} />,
    );
    expect(html).toContain("Вера Лаборант");
    expect(html).toContain("Инженер");
    for (const value of [extra.email, extra.max_user_id, extra.mood])
      expect(html).not.toContain(value);
    const hierarchy = renderToStaticMarkup(
      <DirectoryRelationships
        managerChain={[employees[1], employees[0]]}
        directReports={[employees[2]]}
        onOpen={() => {}}
      />,
    );
    expect(hierarchy.indexOf("Борис Руководитель")).toBeLessThan(
      hierarchy.indexOf("Анна Директор"),
    );
    expect(hierarchy).toContain("Непосредственный руководитель");
    expect(hierarchy).toContain("Прямые подчинённые");
    expect(hierarchy.match(/<button/g)).toHaveLength(3);
  });
  it("requires site-scoped HR to select an allowed site while allowing enterprise-wide HR units", () => {
    const sites = [
      { id: "plant", legal_entity_id: "legal" },
      { id: "other", legal_entity_id: "legal" },
      { id: "foreign", legal_entity_id: "other-legal" },
    ];
    const scope = { site_ids: ["plant"] };
    expect(organizationSiteRequired(scope)).toBe(true);
    expect(
      organizationWritableSites(sites, "legal", scope).map((site) => site.id),
    ).toEqual(["plant"]);
    expect(organizationSiteRequired({ all: true })).toBe(false);
    expect(organizationSiteRequired({ all: true }, "plant")).toBe(true);
    expect(
      organizationWritableSites(sites, "legal", { all: true }, "plant").map(
        (site) => site.id,
      ),
    ).toEqual(["plant"]);
    expect(organizationWritableSites(sites, "other-legal", scope)).toEqual([]);
  });
  it("preserves a manager redacted from a scoped projection when only the unit changes", () => {
    const safeProjection: OrganizationEmployee = {
      id: "worker",
      name: "Сотрудник",
      org_unit_id: "old-unit",
      reports_to_employee_id: null,
    };
    const changes = organizationAssignmentPayload(
      "new-unit",
      "",
      safeProjection,
    );
    const actualStored = {
      org_unit_id: "old-unit",
      reports_to_employee_id: "hidden-hq-manager",
      manager_id: "authorization-user",
    };
    expect({ ...actualStored, ...changes }).toEqual({
      org_unit_id: "new-unit",
      reports_to_employee_id: "hidden-hq-manager",
      manager_id: "authorization-user",
    });
    expect(changes).not.toHaveProperty("reports_to_employee_id");
    expect(
      organizationAssignmentPayload("old-unit", "", safeProjection),
    ).toEqual({});
  });
  it("preserves a redacted head and parent during a unit rename but supports explicitly clearing visible links", () => {
    const safeProjection: OrganizationUnit = {
      id: "unit",
      title: "Старое название",
      type: "TEAM",
      legal_entity_id: "legal",
      site_id: "plant",
      head_employee_id: null,
      parent_id: null,
    };
    const form = {
      title: "Новое название",
      type: "TEAM" as const,
      legal_entity_id: "legal",
      site_id: "plant",
      head_employee_id: null,
      parent_id: null,
    };
    const changes = organizationUnitPatchPayload(form, safeProjection);
    const actualStored = {
      ...safeProjection,
      head_employee_id: "hidden-hq-head",
      parent_id: "hidden-hq-unit",
    };
    const saved = { ...actualStored, ...changes };
    expect(saved.title).toBe("Новое название");
    expect(saved.head_employee_id).toBe("hidden-hq-head");
    expect(saved.parent_id).toBe("hidden-hq-unit");
    expect(changes).toEqual({ title: "Новое название" });
    expect(
      organizationUnitPatchPayload(
        { ...form, title: safeProjection.title },
        safeProjection,
      ),
    ).toEqual({});
    expect(
      organizationUnitPatchPayload(
        { ...form, title: safeProjection.title },
        {
          ...safeProjection,
          head_employee_id: "visible-head",
          parent_id: "visible-parent",
        },
      ),
    ).toEqual({ head_employee_id: null, parent_id: null });
  });
});
