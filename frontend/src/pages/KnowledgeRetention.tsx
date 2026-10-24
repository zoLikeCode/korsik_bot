import { useEffect, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { api, errorText } from "../api";
import {
  ErrorBox,
  Field,
  Loading,
  Notice,
  Status,
  Success,
  useResource,
} from "../ui";

export default function KnowledgeRetention() {
  const state = useResource("/retention/knowledge-policy");
  const [enabled, setEnabled] = useState(false),
    [days, setDays] = useState(""),
    [hold, setHold] = useState(false),
    [reference, setReference] = useState(""),
    [confirmed, setConfirmed] = useState(false),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    if (state.data) {
      setEnabled(state.data.enabled === true);
      setDays(
        state.data.backup_retention_days == null
          ? ""
          : String(state.data.backup_retention_days),
      );
      setHold(state.data.hold === true);
      setReference(state.data.approval_reference || "");
      setConfirmed(false);
    }
  }, [state.data]);
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>
          <ShieldCheck size={20} />
          Политика удаления базы знаний
        </h2>
        <Status value={state.data?.status || "UNCONFIGURED"} />
      </div>
      <ErrorBox error={error || state.error} />
      {success && <Success>{success}</Success>}
      {state.loading ? (
        <Loading />
      ) : (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            setSuccess("");
            try {
              await api.put(
                "/retention/knowledge-policy",
                {
                  enabled,
                  backup_retention_days: days === "" ? null : Number(days),
                  hold,
                  approval_reference: reference,
                  approval_confirmed: confirmed,
                  reason,
                },
                state.etag || state.data?.etag,
              );
              state.reload();
              setSuccess(
                "Политика сохранена. Удаление каждого документа выполняется только отдельным явным действием.",
              );
            } catch (e) {
              setError(errorText(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          <Notice>
            Включайте удаление только по утверждённому регламенту компании.
            Документы не удаляются автоматически. Очисткой резервных копий в
            указанный срок управляет оператор инфраструктуры; приложение не
            выдаёт её за выполненную.
          </Notice>
          <label className="check-field">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => {
                setEnabled(e.target.checked);
                setConfirmed(false);
              }}
            />
            Разрешить ручное удаление документов по утверждённой политике
          </label>
          <div className="form-grid">
            <Field label="Срок хранения резервных копий, дней">
              <input
                type="number"
                min={0}
                max={36500}
                step={1}
                required={enabled}
                value={days}
                onChange={(e) => {
                  setDays(e.target.value);
                  setConfirmed(false);
                }}
              />
            </Field>
            <Field label="Ссылка или номер утверждённого регламента">
              <input
                required={enabled}
                minLength={enabled ? 3 : undefined}
                value={reference}
                onChange={(e) => {
                  setReference(e.target.value);
                  setConfirmed(false);
                }}
              />
            </Field>
          </div>
          <label className="check-field">
            <input
              type="checkbox"
              checked={hold}
              onChange={(e) => {
                setHold(e.target.checked);
                setConfirmed(false);
              }}
            />
            Запретить удаление до снятия обязательного хранения
          </label>
          <label className="check-field">
            <input
              type="checkbox"
              required={enabled}
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            Подтверждаю наличие полномочий и утверждённого регламента с
            указанными параметрами
          </label>
          <Field label="Причина настройки политики">
            <textarea
              required
              minLength={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <button
            className="button secondary"
            disabled={
              busy ||
              reason.trim().length < 3 ||
              (enabled &&
                (!confirmed || days === "" || reference.trim().length < 3))
            }
          >
            Сохранить политику
          </button>
        </form>
      )}
    </section>
  );
}
