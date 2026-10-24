/** Resolve a wall-clock input in an explicit IANA zone without guessing at DST folds. */
export function stageTimeCandidates(local: string, timeZone: string): string[] {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(local);
  if (!match) return [];
  const [, y, mo, d, h, mi] = match.map(Number);
  if (y < 1970 || mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59)
    return [];
  const wall = Date.UTC(y, mo - 1, d, h, mi);
  const checked = new Date(wall);
  if (
    checked.getUTCFullYear() !== y ||
    checked.getUTCMonth() !== mo - 1 ||
    checked.getUTCDate() !== d
  )
    return [];
  try {
    const formatter = new Intl.DateTimeFormat("en-CA", {
      timeZone,
      calendar: "gregory",
      numberingSystem: "latn",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });
    const fields = (instant: number) =>
      Object.fromEntries(
        formatter
          .formatToParts(instant)
          .filter((part) => part.type !== "literal")
          .map((part) => [part.type, Number(part.value)]),
      );
    const key = (parts: Record<string, number>) =>
      `${parts.year}-${parts.month}-${parts.day}-${parts.hour}-${parts.minute}`;
    const expected = `${y}-${mo}-${d}-${h}-${mi}`;
    const offsets = new Set<number>();
    for (let hours = -36; hours <= 36; hours += 6) {
      const instant = wall + hours * 3_600_000;
      const parts = fields(instant);
      offsets.add(
        Date.UTC(
          parts.year,
          parts.month - 1,
          parts.day,
          parts.hour,
          parts.minute,
          parts.second,
        ) - instant,
      );
    }
    return [...offsets]
      .map((offset) => wall - offset)
      .filter((instant) => key(fields(instant)) === expected)
      .sort((a, b) => a - b)
      .map((instant) => new Date(instant).toISOString());
  } catch {
    return [];
  }
}
export function stageTimeLabel(instant: string, timeZone: string): string {
  try {
    return new Intl.DateTimeFormat("ru-RU", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "shortOffset",
      hourCycle: "h23",
    }).format(new Date(instant));
  } catch {
    return "Не удалось отобразить дату в этом часовом поясе";
  }
}
