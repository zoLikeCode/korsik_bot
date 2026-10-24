import { useEffect, useState } from "react";
import { MessageCircle } from "lucide-react";
import { api, errorText, request } from "../api";
import {
  ErrorBox,
  Field,
  Loading,
  Modal,
  Notice,
  Status,
  Success,
  useResource,
} from "../ui";

export function HrContactEditor({
  user,
  onClose,
}: {
  user: any;
  onClose: () => void;
}) {
  const state = useResource(`/users/${user.id}/hr-contact`);
  const [url, setUrl] = useState("");
  const [verified, setVerified] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (state.data) {
      setUrl(state.data.url || "");
      setVerified(state.data.status === "VERIFIED");
    }
  }, [state.data]);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await api.put(
        `/users/${user.id}/hr-contact`,
        { url, verified, reason },
        state.etag || state.data?.etag,
      );
      state.reload();
      setSuccess(
        "Проверенный контакт сохранён. Он будет доступен сотрудникам, которым назначен этот HR.",
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function revoke() {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await request(
        `/users/${user.id}/hr-contact`,
        { method: "DELETE" },
        state.etag || state.data?.etag,
      );
      state.reload();
      setSuccess(
        "Прямой контакт отозван. Сотрудники по-прежнему могут оставить обращение HR через бота.",
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Проверенный контакт HR в MAX" onClose={onClose}>
      <h3>{user.name}</h3>
      <ErrorBox error={error || state.error} />
      {success && <Success>{success}</Success>}
      {state.loading ? (
        <Loading />
      ) : (
        <form onSubmit={save}>
          <Status value={state.data?.status || "NOT_CONFIGURED"} />
          <Field label="Ссылка на профиль HR в MAX">
            <input
              required
              type="url"
              pattern="https://max\.ru/.+"
              maxLength={2048}
              value={url}
              placeholder="https://max.ru/..."
              onChange={(e) => {
                setUrl(e.target.value);
                setVerified(false);
              }}
            />
          </Field>
          <label className="check-field">
            <input
              required
              type="checkbox"
              checked={verified}
              onChange={(e) => setVerified(e.target.checked)}
            />
            Проверено по известному каналу: ссылка принадлежит этому
            HR-специалисту
          </label>
          <Field label="Основание проверки">
            <textarea
              required
              minLength={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <Notice>
            Контакт привязан к назначенному HR. При смене ссылки
            неподтверждённые старые доставки не будут использовать прежний
            адрес.
          </Notice>
          <div className="modal-actions">
            {state.data?.status === "VERIFIED" && (
              <button
                className="button secondary"
                type="button"
                disabled={busy}
                onClick={revoke}
              >
                Отозвать контакт
              </button>
            )}
            <button
              className="button primary"
              disabled={busy || !verified || reason.trim().length < 3}
            >
              {busy ? "Сохраняем…" : "Подтвердить и сохранить"}
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}

export function EmployeeHrContact({ employee }: { employee: any }) {
  const state = useResource(
    employee.employment_stage !== "ENDED"
      ? `/employees/${employee.id}/hr-contact`
      : null,
  );
  if (employee.employment_stage === "ENDED") return null;
  const contact: any = state.data;
  return (
    <div className="employee-contact">
      <ErrorBox error={state.error} />
      {contact?.available &&
      typeof contact.url === "string" &&
      contact.url.startsWith("https://max.ru/") ? (
        <a
          className="button secondary"
          href={contact.url}
          target="_blank"
          rel="noopener noreferrer"
        >
          <MessageCircle size={16} />
          {contact.label || "Написать назначенному HR в MAX"}
        </a>
      ) : (
        !state.loading && (
          <small className="muted">
            Прямой контакт назначенного HR пока не подтверждён. Обращение можно
            оставить в диалоге с Корсиком.
          </small>
        )
      )}
    </div>
  );
}
