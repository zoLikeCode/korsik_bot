import { useState } from "react";
import { Copy, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useActor } from "../App";
import { api, ApiError, errorText, items } from "../api";
import {
  ErrorBox,
  Loading,
  Modal,
  PageTitle,
  Success,
  useResource,
} from "../ui";
import "./HrAccounts.css";

export function scopeCaption(scope: any): string {
  if (scope?.all === true) return "Вся область организации";
  const sites = Array.isArray(scope?.site_ids) ? scope.site_ids.length : 0;
  const legal = Array.isArray(scope?.legal_entity_ids)
    ? scope.legal_entity_ids.length
    : 0;
  return sites || legal
    ? "В пределах вашей области ответственности"
    : "Назначенные сотрудники";
}
export function InvitationStatus({ value }: { value: string }) {
  const labels: Record<string, string> = {
    ISSUED: "Ожидает подключения",
    CONSUMED: "Подключён",
    REDEEMED: "Подключён",
    ACTIVE: "Подключён",
    REVOKED: "Приглашение отозвано",
    EXPIRED: "Срок истёк",
  };
  return <span className="status">{labels[value] || "Недоступно"}</span>;
}
export default function HrAccounts() {
  const actor = useActor();
  const allowed = ["HEAD_TALENT", "HR_ADMIN"].includes(actor.role);
  const accounts = useResource(allowed ? "/hr-accounts" : null);
  const invitations = useResource(allowed ? "/hr-accounts/invitations" : null);
  const [open, setOpen] = useState(false),
    [created, setCreated] = useState<any>(null);
  const [deleting, setDeleting] = useState<any>(null),
    [deleteConflict, setDeleteConflict] = useState(false);
  const [name, setName] = useState(""),
    [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [success, setSuccess] = useState("");
  const reload = () => {
    accounts.reload();
    invitations.reload();
  };
  if (!allowed) return null;
  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const result = await api.post("/hr-accounts/invitations", {
        name: name.trim(),
        email: email.trim(),
      });
      setCreated(result.data);
      reload();
      setSuccess("Приглашение создано");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <PageTitle
        title="HR-менеджеры"
        action={
          <>
            <button className="button secondary" onClick={reload}>
              <RefreshCw size={16} />
              Обновить
            </button>
            <button
              className="button primary"
              onClick={() => {
                setName("");
                setEmail("");
                setCreated(null);
                setError("");
                setSuccess("");
                setOpen(true);
              }}
            >
              <Plus size={17} />
              Создать HR-менеджера
            </button>
          </>
        }
      />
      <ErrorBox error={error || accounts.error || invitations.error} />
      {!open && success && <Success>{success}</Success>}
      <section className="panel hr-account-section">
        <h2>HR-менеджеры в вашей области</h2>
        {accounts.loading ? (
          <Loading />
        ) : (
          !accounts.error &&
          (items(accounts.data).length ? (
            <ul className="hr-account-list">
              {items(accounts.data).map((person) => (
                <li key={person.id}>
                  <div>
                    <strong>{person.name}</strong>
                    <p>{person.email}</p>
                    <small className="muted">
                      {scopeCaption(person.scope)}
                    </small>
                  </div>
                  <span className="status">
                    {person.enabled ? "Активен" : "Выключен"}
                  </span>
                  {actor.role === "HEAD_TALENT" &&
                    person.can_delete === true && (
                      <button
                        className="button secondary compact"
                        disabled={busy}
                        aria-label={`Удалить HR-менеджера: ${person.name}`}
                        onClick={() => {
                          setDeleting(person);
                          setDeleteConflict(false);
                          setError("");
                        }}
                      >
                        <Trash2 size={16} />
                        Удалить
                      </button>
                    )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">HR-менеджеры пока не подключены.</p>
          ))
        )}
      </section>
      <section className="panel hr-account-section">
        <h2>Приглашения</h2>
        {invitations.loading ? (
          <Loading />
        ) : (
          !invitations.error &&
          (items(invitations.data).length ? (
            <ul className="hr-account-list">
              {items(invitations.data).map((invite) => (
                <li key={invite.id}>
                  <div>
                    <strong>{invite.name}</strong>
                    <p>{invite.email}</p>
                    <InvitationStatus value={invite.status} />
                    <small className="muted hr-invite-expiry">
                      Действует до{" "}
                      {new Date(invite.expires_at).toLocaleString("ru-RU")}
                    </small>
                  </div>
                  {invite.status === "ISSUED" && (
                    <button
                      className="button secondary compact"
                      disabled={busy}
                      onClick={async () => {
                        setBusy(true);
                        setError("");
                        try {
                          await api.post(
                            `/hr-accounts/invitations/${invite.id}/revoke`,
                            {},
                            invite.etag,
                          );
                          reload();
                        } catch (e) {
                          setError(errorText(e));
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      Отозвать
                    </button>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">Приглашений пока нет.</p>
          ))
        )}
      </section>
      {deleting && (
        <Modal
          title="Удалить HR-менеджера"
          onClose={() => {
            if (!busy) setDeleting(null);
          }}
        >
          <p>
            Удалить доступ {deleting.name}? Его сессии, привязки MAX и
            приглашения будут отозваны. История работы сохранится.
          </p>
          <ErrorBox error={error} />
          {deleteConflict ? (
            <button
              className="button secondary"
              onClick={() => {
                reload();
                setDeleting(null);
                setError("");
              }}
            >
              Обновить список
            </button>
          ) : (
            <div className="button-row hr-create-actions">
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => setDeleting(null)}
              >
                Отмена
              </button>
              <button
                className="button primary"
                disabled={busy || !deleting.etag}
                onClick={async () => {
                  setBusy(true);
                  setError("");
                  try {
                    await api.delete(
                      `/hr-accounts/${deleting.id}`,
                      deleting.etag,
                    );
                    setDeleting(null);
                    reload();
                    setSuccess(
                      "Доступ HR-менеджера удалён. История сохранена.",
                    );
                  } catch (e) {
                    setError(errorText(e));
                    if (e instanceof ApiError && e.status === 412)
                      setDeleteConflict(true);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {busy ? "Удаляем…" : "Удалить доступ"}
              </button>
            </div>
          )}
        </Modal>
      )}
      {open && (
        <Modal
          title="Новый HR-менеджер"
          onClose={() => {
            if (!busy) setOpen(false);
          }}
        >
          <ErrorBox error={error} />
          {success && <Success>{success}</Success>}
          {created ? (
            <div className="hr-created-invitation">
              <p>
                Передайте персональную ссылку новому HR-менеджеру. После
                открытия в MAX он сможет войти в кабинет.
              </p>
              <label className="field">
                <span>Ссылка для подключения</span>
                <input
                  value={created.invite_url}
                  readOnly
                  onFocus={(event) => event.target.select()}
                />
              </label>
              <button
                className="button secondary"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(created.invite_url);
                    setSuccess("Ссылка скопирована");
                  } catch {
                    setError("Выделите ссылку и скопируйте её вручную.");
                  }
                }}
              >
                <Copy size={16} />
                Скопировать ссылку
              </button>
              <p className="muted">
                Ссылка одноразовая. Действует до{" "}
                {new Date(created.expires_at).toLocaleString("ru-RU")}.
              </p>
              <button className="button primary" onClick={() => setOpen(false)}>
                Готово
              </button>
            </div>
          ) : (
            <form onSubmit={create}>
              <label className="field">
                <span>Имя и фамилия</span>
                <input
                  required
                  maxLength={255}
                  autoComplete="name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <label className="field">
                <span>Рабочая почта</span>
                <input
                  type="email"
                  required
                  maxLength={255}
                  autoComplete="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                />
              </label>
              <p className="muted">
                Доступ: {scopeCaption(actor.scope).toLocaleLowerCase("ru-RU")}.
                Новый аккаунт получает роль HR-менеджера.
              </p>
              <div className="button-row hr-create-actions">
                <button
                  type="button"
                  className="button secondary"
                  disabled={busy}
                  onClick={() => setOpen(false)}
                >
                  Отмена
                </button>
                <button
                  className="button primary"
                  disabled={busy || !name.trim() || !email.trim()}
                >
                  {busy ? "Создаём…" : "Создать приглашение"}
                </button>
              </div>
            </form>
          )}
        </Modal>
      )}
    </>
  );
}
