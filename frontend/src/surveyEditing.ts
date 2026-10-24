/** Visible editing actions get honest audit text; hidden legacy content is retained. */
export function surveyActionReason(action: string): string {
  const reasons: Record<string, string> = {
    save: "Сохранение опроса через конструктор",
    "submit-review": "Отправка версии опроса на проверку через интерфейс",
    APPROVE: "Согласование версии опроса ответственным HR через интерфейс",
    REJECT: "Возврат версии опроса на доработку через интерфейс",
    publish:
      "Проверка текущих вопросов и явная публикация версии ответственным HR через интерфейс",
    version: "Создание новой редакции опроса через интерфейс",
  };
  return reasons[action] || "Изменение опроса через конструктор";
}
export function moveSurveyQuestion<T>(
  questions: T[],
  index: number,
  direction: -1 | 1,
): T[] {
  const target = index + direction;
  if (
    index < 0 ||
    index >= questions.length ||
    target < 0 ||
    target >= questions.length
  )
    return questions;
  const result = [...questions];
  [result[index], result[target]] = [result[target], result[index]];
  return result;
}
export function surveyDraftBody(
  form: { title: string; purpose: string; questions: unknown[] },
  legacy?: { description?: string; thank_you?: string } | null,
) {
  return {
    ...form,
    description: legacy?.description ?? "",
    thank_you: legacy?.thank_you ?? "Спасибо! Ваши ответы сохранены.",
    reason: surveyActionReason("save"),
  };
}
