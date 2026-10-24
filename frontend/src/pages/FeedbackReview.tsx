import { useEffect, useState } from "react";
import { CheckCheck, MessageSquareText } from "lucide-react";
import { useActor } from "../App";
import { api, errorText, items } from "../api";
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  Notice,
  Status,
  Success,
  date,
  useResource,
} from "../ui";
const labels: Record<string, string> = {
  NEGATIVE: "Негативный",
  NEUTRAL: "Нейтральный",
  POSITIVE: "Позитивный",
  MIXED: "Смешанный",
  UNCLEAR: "Недостаточно ясно",
};
export default function FeedbackReview() {
  const actor = useActor();
  const state = useResource(
      ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(actor.role)
        ? "/feedback-analyses"
        : null,
    ),
    employees = useResource(
      ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(actor.role)
        ? "/employees"
        : null,
    );
  const [selected, setSelected] = useState(""),
    [showReviewed, setShowReviewed] = useState(false);
  if (!["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(actor.role))
    return null;
  const rows = items(state.data).filter(
      (r) => showReviewed || r.status !== "REVIEWED",
    ),
    names = new Map(items(employees.data).map((e) => [e.id, e.name]));
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>
          <MessageSquareText size={20} />
          Оценка обратной связи HR
        </h2>
        <button className="text-button" onClick={state.reload}>
          Обновить
        </button>
      </div>
      <p>
        В агрегаты попадают только подтверждённые человеком темы и тональность
        ответов анкет. Свободный диалог не анализируется скрытно.
      </p>
      <label className="check-field">
        <input
          type="checkbox"
          checked={showReviewed}
          onChange={(e) => setShowReviewed(e.target.checked)}
        />
        Показать также проверенные ответы
      </label>
      <ErrorBox error={state.error || employees.error} />
      {state.loading ? (
        <Loading />
      ) : rows.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Сотрудник</th>
                <th>Ответ получен</th>
                <th>Статус</th>
                <th>Подтверждённая оценка</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>
                    {names.get(r.employee_id) || "Сотрудник вашей области"}
                  </td>
                  <td>{date(r.created_at)}</td>
                  <td>
                    <Status value={r.status} />
                  </td>
                  <td>{labels[r.sentiment] || "Ожидает решения HR"}</td>
                  <td>
                    <button
                      className="text-button"
                      onClick={() => setSelected(r.id)}
                    >
                      Просмотреть и оценить
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty title="Нет ответов, ожидающих оценки">
          Развёрнутые ответы анкет появятся здесь после локальной обработки.
        </Empty>
      )}
      {selected && (
        <FeedbackDetail
          id={selected}
          onClose={() => setSelected("")}
          onDone={state.reload}
        />
      )}
    </section>
  );
}
function FeedbackDetail({
  id,
  onClose,
  onDone,
}: {
  id: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const state = useResource(`/feedback-analyses/${id}`),
    history = useResource(`/feedback-analyses/${id}/reviews`);
  const [label, setLabel] = useState(""),
    [topics, setTopics] = useState(""),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    if (state.data?.sentiment) {
      setLabel(state.data.sentiment);
      setTopics((state.data.topics || []).join("\n"));
    }
  }, [state.data]);
  const d: any = state.data || {},
    source = d.source_feedback || {},
    text = source.text || d.source_text;
  return (
    <Modal title="Проверка обратной связи" onClose={onClose} wide>
      <ErrorBox error={error || state.error} />
      {success && <Success>{success}</Success>}
      {state.loading ? (
        <Loading />
      ) : (
        <>
          <section className="panel inset">
            <p className="preserve-lines">
              {text ||
                "Исходный текст недоступен. Оценку нельзя подтвердить без прочтения ответа."}
            </p>
            {source.created_at && <small>{date(source.created_at)}</small>}
          </section>
          <Notice>
            {d.suggested_sentiment
              ? `Локальная модель предлагает: «${labels[d.suggested_sentiment] || d.suggested_sentiment}». Это подсказка, требующая вашей проверки.`
              : "Готовой машинной оценки нет. Выберите тональность по прочитанному ответу."}{" "}
            Решение используется для поддержки адаптации и не определяет
            кадровый исход.
          </Notice>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              setError("");
              setSuccess("");
              try {
                await api.post(
                  `/feedback-analyses/${id}/review`,
                  {
                    sentiment: label,
                    topics: topics
                      .split("\n")
                      .map((v) => v.trim())
                      .filter(Boolean),
                    reason,
                  },
                  state.etag || d.etag,
                );
                state.reload();
                history.reload();
                onDone();
                setReason("");
                setSuccess(
                  "Оценка сохранена с автором и основанием. Агрегаты обновятся по расписанию.",
                );
              } catch (e) {
                setError(errorText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            <Field label="Подтверждённая тональность">
              <select
                required
                value={label}
                onChange={(e) => setLabel(e.target.value)}
              >
                <option value="">Выберите по прочитанному ответу</option>
                {Object.entries(labels).map(([key, name]) => (
                  <option key={key} value={key}>
                    {name}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="Темы ответа — каждая с новой строки"
              hint="Например: рабочие доступы, взаимодействие с наставником. Не добавляйте имена и персональные обстоятельства."
            >
              <textarea
                value={topics}
                onChange={(e) => setTopics(e.target.value)}
                rows={4}
              />
            </Field>
            <Field label="Основание оценки или исправления">
              <textarea
                required
                minLength={3}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </Field>
            <button
              className="button primary"
              disabled={busy || !text || !label || reason.trim().length < 3}
            >
              <CheckCheck size={16} />
              Сохранить оценку HR
            </button>
          </form>
          <details className="advanced">
            <summary>История ручных оценок</summary>
            <ErrorBox error={history.error} />
            {items(history.data).map((r) => (
              <div className="list-row" key={r.id}>
                <div>
                  <strong>
                    {labels[r.previous_sentiment] || "Не было оценки"} →{" "}
                    {labels[r.new_sentiment] || r.new_sentiment}
                  </strong>
                  <p>{r.reason}</p>
                  <small>{date(r.reviewed_at)}</small>
                </div>
                <Badge>
                  {(r.new_topics || []).join(", ") || "Темы не указаны"}
                </Badge>
              </div>
            ))}
          </details>
        </>
      )}
    </Modal>
  );
}
