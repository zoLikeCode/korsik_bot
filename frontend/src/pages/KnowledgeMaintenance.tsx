import { useEffect, useState } from "react";
import { Archive, History, RefreshCcw } from "lucide-react";
import { useActor } from "../App";
import { api, ApiError, errorText, items, request } from "../api";
import {
  ErrorBox,
  Field,
  Loading,
  Notice,
  Status,
  Success,
  useResource,
  date,
} from "../ui";

export function ImpactReview({
  id,
  onDone,
}: {
  id: string;
  onDone: () => void;
}) {
  const state = useResource(`/knowledge/impact-reviews/${id}`),
    documents = useResource("/knowledge/documents");
  const actor = useActor(),
    [reason, setReason] = useState(""),
    [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const dependents = state.data?.dependents || [];
  const names = new Map(items(documents.data).map((d) => [d.id, d.title]));
  if (state.loading) return <Loading />;
  if (!dependents.length && !state.error) return null;
  return (
    <section className="panel inset">
      <div className="panel-head">
        <h3>Влияние на инструкции HR</h3>
        <Status value={state.data?.status} />
      </div>
      <ErrorBox error={error || state.error} />
      <p>
        На прежнюю версию источника опираются материалы ниже. При публикации
        замены они будут сняты с выдачи. Их можно вернуть после пересборки и
        проверки по новому источнику.
      </p>
      {dependents.map((d: any) => (
        <div className="list-row" key={d.revision_id}>
          <strong>
            {names.get(d.document_id) || "Инструкция в вашей области"}
          </strong>
          <Status value={d.state} />
        </div>
      ))}
      {state.data?.status !== "PASSED" && actor.role === "HR_ADMIN" && (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            try {
              await api.post(
                `/knowledge/impact-reviews/${id}/review`,
                {
                  decisions: dependents.map((d: any) => ({
                    revision_id: d.revision_id,
                    action: "WITHDRAW",
                    reason,
                  })),
                },
                state.etag || state.data?.etag,
              );
              state.reload();
              onDone();
            } catch (e) {
              setError(errorText(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          <Field label="Почему прежние инструкции снимаются с выдачи">
            <textarea
              required
              minLength={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <label className="check-field">
            <input
              required
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            Проверены все зависимые материалы. Подтверждаю их снятие при
            публикации нового источника.
          </label>
          <button
            className="button secondary"
            disabled={busy || !confirmed || reason.trim().length < 3}
          >
            Сохранить решение по зависимым материалам
          </button>
        </form>
      )}
    </section>
  );
}

export function KnowledgeMaintenance({
  documentId,
  revisions,
  onChange,
  onRevision,
  onPurgeStarted,
}: {
  documentId: string;
  revisions: any[];
  onChange: () => void;
  onRevision: (id: string) => void;
  onPurgeStarted: () => void;
}) {
  const actor = useActor(),
    doc = useResource(`/knowledge/documents/${documentId}`),
    snapshot = useResource("/knowledge/snapshots/current");
  const [target, setTarget] = useState(""),
    [reason, setReason] = useState(""),
    [confirmed, setConfirmed] = useState(false),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  const published = revisions.filter(
    (r) => r.status === "PUBLISHED" && r.id !== doc.data?.active_revision_id,
  );
  async function action(rollback: boolean) {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const result = await api.post(
        `/knowledge/documents/${documentId}/${rollback ? "rollback" : "archive"}`,
        rollback
          ? {
              target_revision_id: target,
              expected_snapshot_id: snapshot.data?.id,
              reason,
            }
          : { reason },
        doc.etag || doc.data?.etag,
      );
      doc.reload();
      snapshot.reload();
      onChange();
      if (rollback) {
        onRevision(result.data.revision_id);
        setSuccess(
          "Создана новая ревизия прежнего материала и запущена повторная проверка. Старая активная версия сохраняется до успешной публикации.",
        );
      } else
        setSuccess(
          "Документ архивирован и исключён из новых ответов. История сохранена.",
        );
    } catch (e) {
      if (
        e instanceof ApiError &&
        e.detail &&
        typeof e.detail === "object" &&
        "revision_id" in e.detail
      ) {
        onRevision(String((e.detail as any).revision_id));
        onChange();
        setSuccess(
          "Подготовлена ревизия для отката. Сначала оцените влияние на зависимые инструкции, затем выполните QA и публикацию.",
        );
      } else setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  if (actor.role !== "HR_ADMIN") return null;
  return (
    <details className="advanced">
      <summary>Управление актуальностью документа</summary>
      <ErrorBox error={error || doc.error || snapshot.error} />
      {success && <Success>{success}</Success>}
      <Notice>
        Архивирование сохраняет историю. Откат создаёт новую ревизию и повторяет
        проверки на текущем индексе; прежние QA не переиспользуются.
      </Notice>
      <Field label="Основание изменения актуальности">
        <textarea
          minLength={3}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </Field>
      <label className="check-field">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(e) => setConfirmed(e.target.checked)}
        />
        Я проверил последствия изменения доступных сотрудникам материалов.
      </label>
      <div className="button-row wrap">
        <button
          className="button secondary"
          disabled={
            busy ||
            doc.loading ||
            !confirmed ||
            reason.trim().length < 3 ||
            doc.data?.status === "ARCHIVED"
          }
          onClick={() => action(false)}
        >
          <Archive size={16} />
          Архивировать документ
        </button>
        <button
          className="text-button"
          onClick={() => {
            doc.reload();
            snapshot.reload();
          }}
        >
          <RefreshCcw size={15} />
          Обновить состояние
        </button>
      </div>
      {published.length > 0 && (
        <>
          <Field label="Ранее опубликованная версия для восстановления">
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">Выберите версию</option>
              {published.map((r, i) => (
                <option key={r.id} value={r.id}>
                  {r.filename || `Версия ${r.revision_no || i + 1}`} ·{" "}
                  {r.published_at || r.created_at}
                </option>
              ))}
            </select>
          </Field>
          <button
            className="button secondary"
            disabled={
              busy ||
              doc.loading ||
              snapshot.loading ||
              !target ||
              !confirmed ||
              reason.trim().length < 3
            }
            onClick={() => action(true)}
          >
            <History size={16} />
            Подготовить и проверить откат
          </button>
        </>
      )}
      <PurgeDocument
        documentId={documentId}
        documentState={doc}
        snapshot={snapshot}
        onChange={() => {
          onChange();
          onPurgeStarted();
        }}
      />
    </details>
  );
}

export function RollbackOperation({
  id,
  onDone,
}: {
  id: string;
  onDone: () => void;
}) {
  const state = useResource(`/operations/${id}`);
  const pending = ["PENDING", "SCHEDULED", "RUNNING"].includes(
    state.data?.status,
  );
  useEffect(() => {
    if (pending) {
      const timer = setInterval(state.reload, 3000);
      return () => clearInterval(timer);
    }
  }, [pending, id]);
  useEffect(() => {
    if (state.data?.status === "SUCCEEDED") onDone();
  }, [state.data?.status]);
  return (
    <Notice>
      <Status value={state.data?.status || "PENDING"} /> Откат к прежнему
      материалу:{" "}
      {state.data?.status === "WAITING_REVIEW"
        ? "Оцените контрольные ответы и согласуйте новую ревизию ниже."
        : state.data?.status === "SUCCEEDED"
          ? "Проверенная ревизия опубликована."
          : state.data?.status === "FAILED"
            ? "Откат не опубликован; действующая версия сохранена."
            : "Идёт повторная проверка. Статус сохранён на сервере."}
      <ErrorBox error={state.error || state.data?.error?.code} />
    </Notice>
  );
}

function PurgeDocument({
  documentId,
  documentState,
  snapshot,
  onChange,
}: {
  documentId: string;
  documentState: any;
  snapshot: any;
  onChange: () => void;
}) {
  const policy = useResource("/retention/knowledge-policy");
  const [confirmed, setConfirmed] = useState(false),
    [reason, setReason] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [operationId, setOperationId] = useState("");
  const allowed =
    policy.data?.enabled === true &&
    policy.data?.status === "ACTIVE" &&
    policy.data?.hold !== true;
  return (
    <details className="advanced">
      <summary>Удалить документ и производные данные</summary>
      <ErrorBox error={error || policy.error} />
      <Notice>
        После запуска доступ отзывается сразу, затем рабочие файлы, векторы и
        кэши удаляются фоновым заданием. Зависимые инструкции снимаются с
        выдачи. Сообщения, уже доставленные во внешний мессенджер, и резервные
        копии требуют отдельной работы оператора. Автоматического удаления копий
        нет.
      </Notice>
      {!allowed ? (
        <p className="muted">
          Удаление недоступно: нужна действующая утверждённая политика без
          запрета обязательного хранения. Настройте её в разделе «Настройки».
        </p>
      ) : (
        <>
          <p>
            Регламент: {policy.data.approval_reference}. Срок хранения резервных
            копий: {policy.data.backup_retention_days} дней.
          </p>
          <Field label="Основание безвозвратного удаления">
            <textarea
              required
              minLength={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <label className="check-field">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            Подтверждаю безвозвратное удаление рабочих данных документа и
            понимаю влияние на связанные материалы.
          </label>
          <button
            className="button danger"
            disabled={
              busy ||
              !!operationId ||
              !confirmed ||
              reason.trim().length < 3 ||
              documentState.loading ||
              snapshot.loading
            }
            onClick={async () => {
              setBusy(true);
              setError("");
              try {
                const result = await request(
                  `/knowledge/documents/${documentId}`,
                  {
                    method: "DELETE",
                    body: JSON.stringify({
                      expected_snapshot_id: snapshot.data?.id,
                      retention_policy_id: policy.data.id,
                      reason,
                    }),
                  },
                  documentState.etag || documentState.data?.etag,
                );
                setOperationId(result.data.operation_id || result.data.id);
                onChange();
              } catch (e) {
                setError(errorText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            Безвозвратно удалить рабочие данные
          </button>
        </>
      )}
      {operationId && (
        <Notice>
          Удаление поставлено в очередь. Статус останется доступен в журнале
          операций базы знаний после перезагрузки.
        </Notice>
      )}
    </details>
  );
}

export function KnowledgeMaintenanceOperations({
  onRevision,
  refreshToken,
}: {
  onRevision: (id: string) => void;
  refreshToken: unknown;
}) {
  const actor = useActor(),
    state = useResource(
      actor.role === "HR_ADMIN" ? "/knowledge/maintenance-operations" : null,
      [refreshToken],
    );
  const rows = items(state.data),
    pending = rows.some((r) =>
      ["PENDING", "RUNNING", "SCHEDULED", "WAITING_REVIEW"].includes(r.status),
    );
  useEffect(() => {
    if (pending) {
      const timer = setInterval(state.reload, 3000);
      return () => clearInterval(timer);
    }
  }, [pending]);
  if (actor.role !== "HR_ADMIN" || (!rows.length && !state.error)) return null;
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Операции базы знаний</h2>
        <button className="text-button" onClick={state.reload}>
          Обновить
        </button>
      </div>
      <ErrorBox error={state.error} />
      {rows.map((r) => (
        <div className="list-row" key={r.id}>
          <div>
            <strong>
              {r.type === "KNOWLEDGE_PURGE"
                ? "Удаление рабочих данных"
                : "Восстановление прежнего материала"}
            </strong>
            <p>
              {date(r.created_at)}
              {r.type === "KNOWLEDGE_PURGE" && r.status === "SUCCEEDED"
                ? " · Рабочие данные удалены; копии контролирует оператор."
                : ""}
              {r.backup_expiry_at
                ? ` · Срок резервных копий: ${date(r.backup_expiry_at)}`
                : ""}
            </p>
            <ErrorBox error={r.error?.code} />
          </div>
          <Status value={r.status} />
          {r.type === "KNOWLEDGE_ROLLBACK" && r.revision_id && (
            <button
              className="text-button"
              onClick={() => onRevision(r.revision_id)}
            >
              Открыть проверку
            </button>
          )}
        </div>
      ))}
    </section>
  );
}
