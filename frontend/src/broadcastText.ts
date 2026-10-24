export type MentionSpan = {
  employee_id: string;
  start: number;
  end: number;
  label: string;
};
export type BroadcastText = { text: string; mentions: MentionSpan[] };
export function editBroadcastText(
  value: BroadcastText,
  text: string,
): BroadcastText {
  let start = 0;
  while (
    start < value.text.length &&
    start < text.length &&
    value.text[start] === text[start]
  )
    start++;
  let oldEnd = value.text.length,
    newEnd = text.length;
  while (
    oldEnd > start &&
    newEnd > start &&
    value.text[oldEnd - 1] === text[newEnd - 1]
  ) {
    oldEnd--;
    newEnd--;
  }
  const delta = newEnd - oldEnd;
  return {
    text,
    mentions: value.mentions.flatMap((m) =>
      m.end <= start
        ? [m]
        : m.start >= oldEnd
          ? [{ ...m, start: m.start + delta, end: m.end + delta }]
          : [],
    ),
  };
}
export function insertBroadcastMention(
  value: BroadcastText,
  person: {
    id: string;
    name: string;
    preferred_name?: string | null;
    mention_name?: string | null;
  },
  start: number,
  end = start,
): BroadcastText {
  const label = person.preferred_name || person.name;
  const delta = label.length - (end - start);
  const retained = value.mentions.flatMap((mention) => {
    if (mention.end <= start) return [mention];
    if (mention.start >= end)
      return [
        { ...mention, start: mention.start + delta, end: mention.end + delta },
      ];
    return [];
  });
  return {
    text: value.text.slice(0, start) + label + value.text.slice(end),
    mentions: [
      ...retained,
      { employee_id: person.id, start, end: start + label.length, label },
    ].sort((a, b) => a.start - b.start),
  };
}
export function encodeBroadcastText(value: BroadcastText): string {
  return value.text;
}
export function broadcastMentionPeople(
  employees: any[],
  unit: string,
  search: string,
) {
  return employees.filter(
    (p) =>
      p.mention_available !== false &&
      (!unit || (p.org_unit_ancestor_ids || []).includes(unit)) &&
      `${p.name} ${p.preferred_name || ""} ${p.department || ""}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
}

export function broadcastAudience(
  employees: any[],
  selected: string[],
  units: string[],
) {
  const explicit = new Set(selected),
    group = new Set(units);
  return employees.filter(
    (e) =>
      explicit.has(e.id) ||
      (e.org_unit_ancestor_ids || []).some((id: string) => group.has(id)),
  );
}
export function broadcastStatus(item: any) {
  if (item.status === "COMPLETED")
    return { label: "Исполнено", detail: "", tone: "green" };
  if (item.counts?.unknown)
    return {
      label: "В ожидании",
      detail: "Результат доставки не подтверждён",
      tone: "amber",
    };
  if (item.counts?.failed)
    return {
      label: "В ожидании",
      detail: "Есть ошибки доставки",
      tone: "amber",
    };
  return {
    label: "В ожидании",
    detail: item.sending ? "Отправляется" : "",
    tone: "neutral",
  };
}

export function reconcileBroadcastSelection(
  employees: any[],
  units: any[],
  selected: string[],
  groups: string[],
) {
  const available = new Set(
    employees.filter((person) => person.available).map((person) => person.id),
  );
  const visibleUnits = new Set(units.map((unit) => unit.id));
  const recipients = selected.filter((id) => available.has(id)),
    selectedUnits = groups.filter((id) => visibleUnits.has(id));
  return {
    recipients,
    units: selectedUnits,
    removed:
      selected.length -
      recipients.length +
      groups.length -
      selectedUnits.length,
  };
}
