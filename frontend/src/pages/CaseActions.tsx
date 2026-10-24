import { useState } from "react";
import { useActor } from "../App";
import { api, errorText, items } from "../api";
import { ErrorBox, Field, Modal, Notice, Success, useResource } from "../ui";
export function CreateSupportItem({
  task,
  onClose,
  onCreated,
}: {
  task: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const employees = useResource("/employees"),
    directory = useResource("/directory");
  const [employeeId, setEmployeeId] = useState(""),
    [managerId, setManagerId] = useState(""),
    [kind, setKind] = useState("GENERAL"),
    [title, setTitle] = useState(""),
    [text, setText] = useState(""),
    [due, setDue] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  return (
    <Modal
      title={task ? "Новое поручение руководителю" : "Новое обращение HR"}
      onClose={onClose}
    >
      <ErrorBox error={error || employees.error || directory.error} />
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          try {
            await api.post(
              task ? "/manager-tasks" : "/cases",
              task
                ? {
                    employee_id: employeeId,
                    assigned_manager_id: managerId,
                    summary: text,
                    reason_code: "HR_REQUEST",
                    due_at: new Date(due).toISOString(),
                  }
                : {
                    employee_id: employeeId,
                    reason: title,
                    description: text,
                    kind,
                    request_id: crypto.randomUUID(),
                  },
            );
            onCreated();
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <Field label="Сотрудник">
          <select
            required
            value={employeeId}
            onChange={(e) => {
              setEmployeeId(e.target.value);
              setManagerId(
                items(employees.data).find((v) => v.id === e.target.value)
                  ?.manager_id || "",
              );
            }}
          >
            <option value="">Выберите сотрудника</option>
            {items(employees.data)
              .filter((e) => e.employment_stage !== "ENDED")
              .map((e) => (
                <option key={e.id} value={e.id}>
                  {e.name}
                </option>
              ))}
          </select>
        </Field>
        {task ? (
          <>
            <Field label="Ответственный руководитель">
              <select
                required
                value={managerId}
                onChange={(e) => setManagerId(e.target.value)}
              >
                <option value="">Выберите руководителя</option>
                {(directory.data?.users || [])
                  .filter(
                    (u: any) => u.role === "MANAGER" && u.enabled !== false,
                  )
                  .map((u: any) => (
                    <option key={u.id} value={u.id}>
                      {u.name}
                    </option>
                  ))}
              </select>
            </Field>
            <Notice>
              Для замещения можно выбрать другого руководителя. Он получит
              доступ к этому поручению; карточка сотрудника и его анкеты
              остаются в текущей области полномочий.
            </Notice>
            <Field label="Срок по времени вашего браузера">
              <input
                required
                type="datetime-local"
                value={due}
                onChange={(e) => setDue(e.target.value)}
              />
            </Field>
          </>
        ) : (
          <>
            <Field label="Тема обращения">
              <input
                required
                minLength={3}
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
            </Field>
            <Field label="Вид обращения">
              <select value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="GENERAL">Общий вопрос</option>
                <option value="BLOCKER">
                  Препятствие адаптации с подтверждением результата
                </option>
              </select>
            </Field>
          </>
        )}
        <Field label={task ? "Что нужно сделать" : "Описание вопроса"}>
          <textarea
            required
            minLength={3}
            rows={6}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </Field>
        <Notice>
          {task
            ? "Поручение попадёт в кабинет выбранного руководителя. Доставка в MAX зависит от его подтверждённой привязки."
            : "Решение препятствия завершится только после ответа сотрудника или отдельного административного закрытия."}
        </Notice>
        <button
          className="button primary"
          disabled={busy || !employeeId || (task && (!managerId || !due))}
        >
          {busy ? "Создаём…" : "Создать"}
        </button>
      </form>
    </Modal>
  );
}
export function CaseExtraActions({
  item,
  onDone,
}: {
  item: any;
  onDone: () => void;
}) {
  const actor = useActor(),
    [note, setNote] = useState(""),
    [reason, setReason] = useState(""),
    [confirmed, setConfirmed] = useState(false),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  async function act(path: string, body: any) {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await api.post(`/cases/${item.id}/${path}`, body);
      setSuccess("Действие сохранено в истории обращения.");
      setNote("");
      onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  if (!["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(actor.role))
    return null;
  return (
    <details className="advanced">
      <summary>Заметки и дополнительные действия</summary>
      <ErrorBox error={error} />
      {success && <Success>{success}</Success>}
      <Field label="Рабочая заметка HR">
        <textarea
          minLength={3}
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </Field>
      <button
        className="button secondary"
        disabled={busy || note.trim().length < 3}
        onClick={() => act("notes", { text: note })}
      >
        Сохранить заметку
      </button>
      <Field label="Основание повторного открытия / административного закрытия">
        <textarea
          minLength={3}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </Field>
      {[
        "RESOLVED",
        "CLOSED",
        "CONFIRMED",
        "CLOSED_WITHOUT_CONFIRMATION",
      ].includes(item.status) && (
        <button
          className="button secondary"
          disabled={busy || reason.trim().length < 3}
          onClick={() => act("reopen", { reason })}
        >
          Открыть повторно
        </button>
      )}
      {actor.role === "HR_ADMIN" &&
        !["CANCELLED", "CLOSED_WITHOUT_CONFIRMATION", "CONFIRMED"].includes(
          item.status,
        ) && (
          <>
            <label className="check-field">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              Закрыть по административному основанию, без подмены подтверждения
              сотрудником
            </label>
            <button
              className="button secondary"
              disabled={busy || !confirmed || reason.trim().length < 3}
              onClick={() => act("administrative-close", { reason })}
            >
              Закрыть без подтверждения решения
            </button>
          </>
        )}
    </details>
  );
}
