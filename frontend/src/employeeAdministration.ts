export const EMPLOYEE_PAGE_SIZE = 10;
export type EmployeeCreateFields = {
  name: string;
  email: string;
  manager_id: string;
  start_date: string;
  probation_end_date: string;
  timezone: string;
};
export function employeeCreatePayload(form: EmployeeCreateFields) {
  return {
    name: form.name.trim(),
    ...(form.email.trim() ? { email: form.email.trim() } : {}),
    ...(form.manager_id ? { manager_id: form.manager_id } : {}),
    start_date: form.start_date,
    ...(form.probation_end_date
      ? { probation_end_date: form.probation_end_date }
      : {}),
    timezone: form.timezone,
  };
}
export function employeeListPath(page: number, search: string, stage: string) {
  const query = new URLSearchParams({
    page: String(Math.max(1, Math.trunc(page))),
    page_size: String(EMPLOYEE_PAGE_SIZE),
  });
  if (search.trim()) query.set("search", search.trim());
  if (stage) query.set("stage", stage);
  return `/employees?${query}`;
}
export function employeeDateChanges(
  start: string,
  probation: string,
  baseline: { start_date?: string; probation_end_date?: string },
) {
  return {
    ...(start && start !== baseline.start_date ? { start_date: start } : {}),
    ...(probation && probation !== baseline.probation_end_date
      ? { probation_end_date: probation }
      : {}),
  };
}
export function employeeInviteUrl(invite: any): string {
  const value =
    invite?.invite_url ||
    invite?.url ||
    invite?.invitation_url ||
    invite?.deep_link;
  return typeof value === "string" && /^https:\/\/max\.ru\/[^\s]+$/.test(value)
    ? value
    : "";
}
export function employeeAccessLabel(enabled: boolean | undefined) {
  return enabled === false ? "Выключен" : "Активен";
}
export function employeeMaxLabel(status: string) {
  return status === "VERIFIED"
    ? "Подключён"
    : ["PENDING_VERIFICATION", "PENDING"].includes(status)
      ? "Подключается"
      : "Не подключён";
}

export function employeeDatesDirty(
  start: string,
  probation: string,
  baseline: { start_date?: string; probation_end_date?: string },
) {
  return (
    start !== (baseline.start_date || "") ||
    probation !== (baseline.probation_end_date || "")
  );
}
export function employeeEffectiveEnabled(employee: {
  enabled?: boolean;
  employment_stage?: string;
}) {
  return employee.enabled !== false && employee.employment_stage !== "ENDED";
}
export function moscowTodayISO(clock = new Date()) {
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: "Europe/Moscow",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(clock);
  const get = (type: string) => parts.find((part) => part.type === type)!.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}
