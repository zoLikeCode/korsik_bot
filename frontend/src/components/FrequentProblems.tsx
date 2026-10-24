import { frequentProblemTopics } from "../analyticsOverview";
import { ErrorBox, Loading } from "../ui";
import "./AnalyticsOverview.css";

export default function FrequentProblems({
  data,
  loading = false,
  error,
}: {
  data: any;
  loading?: boolean;
  error?: unknown;
}) {
  // An error must never reveal retained results from a previous filter/scope.
  const summary = frequentProblemTopics(error || loading ? null : data);
  const max = Math.max(1, ...summary.topics.map((row) => row.employeeCount));
  return (
    <section className="panel overview-problems" aria-label="Частые проблемы">
      <h2>Частые проблемы</h2>
      <p className="muted">
        Наиболее частые темы обратной связи, подтверждённые HR за выбранный
        период в доступной вам области.
      </p>
      {loading ? (
        <Loading />
      ) : error ? (
        <ErrorBox error={error} />
      ) : !data ? (
        <p className="muted">Данные ещё не получены.</p>
      ) : (
        <>
          {summary.topics.length ? (
            <ol
              className="overview-problem-topics"
              aria-label="Подтверждённые темы"
            >
              {summary.topics.map((row) => (
                <li key={row.topic}>
                  <strong>{row.topic}</strong>
                  <span>{row.employeeCount} сотрудников</span>
                  <div className="overview-track" aria-hidden="true">
                    <span
                      style={{ width: `${(row.employeeCount * 100) / max}%` }}
                    />
                  </div>
                </li>
              ))}
            </ol>
          ) : (
            <p>
              {summary.hidden
                ? "Недостаточно данных для показа тем."
                : "Пока нет подтверждённых тем."}
            </p>
          )}
          {summary.visibleTopics > 5 && (
            <p className="muted">
              Показаны пять наиболее частых доступных тем.
            </p>
          )}
          {summary.pending !== null && summary.pending > 0 && (
            <p className="muted">Ожидают оценки HR: {summary.pending}.</p>
          )}
          <p className="muted">
            Считаются уникальные сотрудники по каждой теме. Один сотрудник может
            входить в несколько тем. Названия и численность групп менее{" "}
            {summary.minimum} сотрудников скрыты. Отсутствие данных не означает
            отсутствия затруднений.
          </p>
          {summary.hidden && (
            <p className="muted">Часть тем скрыта из-за малой группы.</p>
          )}
        </>
      )}
    </section>
  );
}
