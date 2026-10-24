export function observedCount(value: unknown): number | null {
  return typeof value === "number" &&
    Number.isFinite(value) &&
    Number.isInteger(value) &&
    value >= 0
    ? value
    : null;
}
export function observedRatio(
  value: any,
): { numerator: number; denominator: number; percent: number } | null {
  const numerator = observedCount(value?.numerator),
    denominator = observedCount(value?.denominator);
  if (
    numerator === null ||
    denominator === null ||
    denominator === 0 ||
    numerator > denominator ||
    value?.suppressed === true
  )
    return null;
  return {
    numerator,
    denominator,
    percent: Math.round((numerator * 100) / denominator),
  };
}
export function dashboardRole(role: string) {
  return role === "MANAGER"
    ? {
        title: "Моя команда",
        caption: "Сотрудники и задачи, доступные вам как руководителю.",
      }
    : role === "HEAD_TALENT"
      ? {
          title: "Общая картина адаптации",
          caption:
            "Сводка по доступным вам площадкам. Личные ответы сотрудников здесь не показываются.",
        }
      : {
          title: "Работа HR",
          caption:
            "Текущая нагрузка и участие в анкетах в вашей области ответственности.",
        };
}
export function displayAverage(row: any): string {
  if (row?.suppressed) return "Скрыто: меньше 5 участников";
  return typeof row?.average === "number" && Number.isFinite(row.average)
    ? row.average.toFixed(2)
    : "Пока нет ответов";
}

/** Only already-authorized, human-reviewed topic aggregates may be shown. */
export function frequentProblemTopics(data: any) {
  const minimum = Math.max(5, observedCount(data?.minimum_group_size) ?? 5);
  const rows = Array.isArray(data?.items) ? data.items : [];
  let hidden = (observedCount(data?.suppressed_count) ?? 0) > 0;
  const visible: { topic: string; employeeCount: number }[] = [];
  for (const row of rows) {
    const count = observedCount(row?.employee_count);
    if (row?.suppressed === true || count === null || count < minimum) {
      hidden = true;
      continue;
    }
    if (typeof row?.topic !== "string" || !row.topic.trim()) continue;
    visible.push({ topic: row.topic.trim(), employeeCount: count });
  }
  visible.sort(
    (a, b) =>
      b.employeeCount - a.employeeCount || a.topic.localeCompare(b.topic, "ru"),
  );
  return {
    topics: visible.slice(0, 5),
    visibleTopics: visible.length,
    hidden,
    minimum,
    pending: observedCount(data?.pending_analysis_count),
  };
}
