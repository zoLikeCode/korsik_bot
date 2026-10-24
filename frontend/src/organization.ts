export type OrganizationEmployee = {
  id: string;
  name: string;
  position?: string;
  department?: string;
  org_unit_id?: string | null;
  reports_to_employee_id?: string | null;
  manager_employee_id?: string | null;
  profile_photo_url?: string | null;
  version?: number;
  etag?: string;
  can_edit?: boolean;
  legal_entity_id?: string | null;
  site_id?: string | null;
  org_unit_title?: string | null;
  access_role?: string;
  can_change_access_role?: boolean;
};
export type OrganizationUnit = {
  id: string;
  title: string;
  type: "DIRECTION" | "DEPARTMENT" | "TEAM";
  parent_id?: string | null;
  head_employee_id?: string | null;
  legal_entity_id?: string | null;
  site_id?: string | null;
  status?: string;
  version?: number;
  etag?: string;
  can_edit?: boolean;
};
export type OrganizationNode = {
  unit: OrganizationUnit;
  members: OrganizationEmployee[];
  children: OrganizationNode[];
};
export const organizationTypes = {
  DIRECTION: "Направление",
  DEPARTMENT: "Отдел",
  TEAM: "Команда",
};
const nameOrder = (
  a: { name?: string; title?: string },
  b: { name?: string; title?: string },
) => (a.name || a.title || "").localeCompare(b.name || b.title || "", "ru");
export function organizationDescendants(
  units: OrganizationUnit[],
  id: string,
): Set<string> {
  const result = new Set<string>([id]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const unit of units)
      if (
        unit.parent_id &&
        result.has(unit.parent_id) &&
        !result.has(unit.id)
      ) {
        result.add(unit.id);
        changed = true;
      }
  }
  return result;
}
export function organizationTree(
  units: OrganizationUnit[],
  employees: OrganizationEmployee[],
): OrganizationNode[] {
  const active = units.filter((unit) => unit.status !== "ARCHIVED");
  const ids = new Set(active.map((unit) => unit.id));
  const visited = new Set<string>();
  const build = (unit: OrganizationUnit): OrganizationNode | null => {
    if (visited.has(unit.id)) return null;
    visited.add(unit.id);
    return {
      unit,
      members: employees
        .filter((employee) => employee.org_unit_id === unit.id)
        .sort(nameOrder),
      children: active
        .filter((child) => child.parent_id === unit.id)
        .sort(nameOrder)
        .map(build)
        .filter((node): node is OrganizationNode => !!node),
    };
  };
  const roots = active
    .filter((unit) => !unit.parent_id || !ids.has(unit.parent_id))
    .sort(nameOrder)
    .map(build)
    .filter((node): node is OrganizationNode => !!node);
  // Keep malformed historical cycles visible once instead of hiding the records.
  for (const unit of active.sort(nameOrder))
    if (!visited.has(unit.id)) {
      const node = build(unit);
      if (node) roots.push(node);
    }
  return roots;
}
export function unassignedEmployees(
  units: OrganizationUnit[],
  employees: OrganizationEmployee[],
) {
  const ids = new Set(
    units.filter((unit) => unit.status !== "ARCHIVED").map((unit) => unit.id),
  );
  return employees
    .filter(
      (employee) => !employee.org_unit_id || !ids.has(employee.org_unit_id),
    )
    .sort(nameOrder);
}
export function organizationEmployeeMatches(
  employee: OrganizationEmployee,
  query: string,
) {
  const term = query.trim().toLocaleLowerCase("ru");
  return (
    !term ||
    [employee.name, employee.position, employee.department].some((value) =>
      value?.toLocaleLowerCase("ru").includes(term),
    )
  );
}
export function filterOrganizationTree(
  nodes: OrganizationNode[],
  query: string,
): OrganizationNode[] {
  const term = query.trim().toLocaleLowerCase("ru");
  if (!term) return nodes;
  return nodes.flatMap((node) => {
    if (node.unit.title.toLocaleLowerCase("ru").includes(term)) return [node];
    const children = filterOrganizationTree(node.children, query),
      members = node.members.filter((employee) =>
        organizationEmployeeMatches(employee, query),
      );
    return children.length || members.length
      ? [{ ...node, children, members }]
      : [];
  });
}
export function organizationUnitPath(
  units: OrganizationUnit[],
  id?: string | null,
): OrganizationUnit[] {
  const result: OrganizationUnit[] = [],
    seen = new Set<string>();
  let unit = units.find((candidate) => candidate.id === id);
  while (unit && !seen.has(unit.id)) {
    seen.add(unit.id);
    result.unshift(unit);
    unit = units.find((candidate) => candidate.id === unit?.parent_id);
  }
  return result;
}
export function organizationManagerCandidates(
  employees: OrganizationEmployee[],
  currentId: string,
) {
  const excluded = new Set([currentId]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const employee of employees) {
      const manager =
        employee.manager_employee_id || employee.reports_to_employee_id;
      if (manager && excluded.has(manager) && !excluded.has(employee.id)) {
        excluded.add(employee.id);
        changed = true;
      }
    }
  }
  return employees
    .filter((employee) => !excluded.has(employee.id))
    .sort(nameOrder);
}
export function organizationEmployeeUrl(id: string) {
  return `/organization?employee=${encodeURIComponent(id)}`;
}

export function organizationUnitCompatible(
  unit: OrganizationUnit,
  employee: OrganizationEmployee,
) {
  return (
    unit.status !== "ARCHIVED" &&
    (!employee.legal_entity_id ||
      unit.legal_entity_id === employee.legal_entity_id) &&
    (!unit.site_id || !employee.site_id || unit.site_id === employee.site_id)
  );
}

export function organizationAssignmentPayload(
  unitId: string,
  managerEmployeeId: string,
  baseline: Pick<
    OrganizationEmployee,
    "org_unit_id" | "reports_to_employee_id"
  >,
): { org_unit_id?: string | null; reports_to_employee_id?: string | null } {
  const changes: {
    org_unit_id?: string | null;
    reports_to_employee_id?: string | null;
  } = {};
  if ((unitId || null) !== (baseline.org_unit_id || null))
    changes.org_unit_id = unitId || null;
  if ((managerEmployeeId || null) !== (baseline.reports_to_employee_id || null))
    changes.reports_to_employee_id = managerEmployeeId || null;
  return changes;
}
export type OrganizationUnitDraft = {
  title: string;
  type: OrganizationUnit["type"];
  legal_entity_id: string;
  parent_id: string | null;
  head_employee_id: string | null;
  site_id: string | null;
};
export function organizationUnitPatchPayload(
  candidate: OrganizationUnitDraft,
  baseline: OrganizationUnit,
): Partial<OrganizationUnitDraft> {
  const next: OrganizationUnitDraft = {
    title: candidate.title.trim(),
    type: candidate.type,
    legal_entity_id: candidate.legal_entity_id,
    parent_id: candidate.parent_id || null,
    head_employee_id: candidate.head_employee_id || null,
    site_id: candidate.site_id || null,
  };
  const before: OrganizationUnitDraft = {
    title: baseline.title,
    type: baseline.type,
    legal_entity_id: baseline.legal_entity_id || "",
    parent_id: baseline.parent_id || null,
    head_employee_id: baseline.head_employee_id || null,
    site_id: baseline.site_id || null,
  };
  return Object.fromEntries(
    Object.entries(next).filter(
      ([key, value]) => value !== before[key as keyof OrganizationUnitDraft],
    ),
  ) as Partial<OrganizationUnitDraft>;
}

export function organizationSiteRequired(
  scope: { all?: boolean; site_ids?: string[] },
  parentSiteId?: string | null,
) {
  return !!parentSiteId || (scope.all !== true && !!scope.site_ids?.length);
}
export function organizationWritableSites<
  T extends { id: string; legal_entity_id?: string },
>(
  sites: T[],
  legalEntityId: string,
  scope: { all?: boolean; site_ids?: string[] },
  parentSiteId?: string | null,
) {
  return sites.filter(
    (site) =>
      site.legal_entity_id === legalEntityId &&
      (!parentSiteId || site.id === parentSiteId) &&
      (scope.all === true ||
        !scope.site_ids?.length ||
        scope.site_ids.includes(site.id)),
  );
}
