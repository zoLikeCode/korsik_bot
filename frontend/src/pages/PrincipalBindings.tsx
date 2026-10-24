import { useEffect, useState } from "react";
import { Copy, Link2, RefreshCw } from "lucide-react";
import { api, items, errorText } from "../api";
import { useResource, ErrorBox, Success, Notice, Status } from "../ui";

export default function PrincipalBindings({
  showPreferences = false,
}: {
  showPreferences?: boolean;
}) {
  const binding = useResource("/me/notification-bindings"),
    prefs = useResource(showPreferences ? "/me/dashboard-notifications" : null);
  const [preview, setPreview] = useState<any>(null);
  const [identityChecked, setIdentityChecked] = useState(false);
  const [created, setCreated] = useState<any>(null),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    if (
      items(binding.data).some((b) =>
        ["ISSUED", "PENDING_WEB_CONFIRM"].includes(b.status),
      )
    ) {
      const timer = setInterval(binding.reload, 3000);
      return () => clearInterval(timer);
    }
  }, [binding.data]);
  async function run(fn: () => Promise<any>, message: string) {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await fn();
      binding.reload();
      prefs.reload();
      setSuccess(message);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Notice>
        Создайте персональную ссылку, откройте её в своём аккаунте MAX, затем
        вернитесь сюда и подтвердите найденный аккаунт. Подключение относится
        только к вашей учётной записи.
      </Notice>
      <div className="principal-binding-actions">
        <button
          className="button secondary"
          disabled={busy}
          onClick={binding.reload}
        >
          <RefreshCw size={16} />
          Обновить состояние
        </button>
        <button
          className="button secondary"
          disabled={
            busy ||
            binding.loading ||
            items(binding.data).some((b) => b.status === "ACTIVE")
          }
          onClick={() =>
            run(async () => {
              setPreview(null);
              setIdentityChecked(false);
              setCreated(
                (await api.post("/me/notification-bindings", {})).data,
              );
            }, "Ссылка для привязки создана")
          }
        >
          <Link2 size={16} />
          Подключить MAX
        </button>
      </div>
      <ErrorBox error={error || binding.error} />
      {success && <Success>{success}</Success>}
      {items(binding.data).map((b) => (
        <div className="list-row" key={b.id}>
          <div>
            <strong>
              MAX · {b.display_name || b.max_user_id || "Личная привязка"}
            </strong>
            <p>
              <Status value={b.status} />
            </p>
          </div>
          {b.status === "PENDING_WEB_CONFIRM" && (
            <button
              className="button secondary compact"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  const r = await api.get(`/me/notification-bindings/${b.id}`);
                  setPreview({ ...r.data, etag: r.etag });
                  setIdentityChecked(false);
                }, "Проверьте найденный аккаунт MAX")
              }
            >
              Проверить аккаунт
            </button>
          )}
          {!["REVOKED", "CANCELLED"].includes(b.status) && (
            <button
              className="text-button"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  const r = await api.get(`/me/notification-bindings/${b.id}`);
                  await api.post(
                    `/me/notification-bindings/${b.id}/revoke`,
                    {},
                    r.etag,
                  );
                  if (preview?.id === b.id) {
                    setPreview(null);
                    setIdentityChecked(false);
                  }
                  if (created?.id === b.id) setCreated(null);
                }, "Привязка отключена")
              }
            >
              Отключить
            </button>
          )}
        </div>
      ))}
      {preview && (
        <section className="panel principal-binding-preview">
          <h3>Найденный аккаунт MAX</h3>
          <p>
            <strong>{preview.display_name || "Пользователь MAX"}</strong>
          </p>
          <Status value={preview.status} />
          <label className="check-field spaced">
            <input
              type="checkbox"
              checked={identityChecked}
              onChange={(e) => setIdentityChecked(e.target.checked)}
            />
            Это мой аккаунт MAX
          </label>
          <div className="button-row">
            <button
              className="button primary"
              disabled={
                busy ||
                !identityChecked ||
                preview.status !== "PENDING_WEB_CONFIRM" ||
                !preview.etag
              }
              onClick={() =>
                run(async () => {
                  const response = await api.post(
                    `/me/notification-bindings/${preview.id}/confirm`,
                    { confirm: true },
                    preview.etag,
                  );
                  if (
                    response.data.status !== "ACTIVE" ||
                    response.data.id !== preview.id
                  )
                    throw new Error(
                      "Подключение не подтверждено. Обновите состояние.",
                    );
                  setPreview(null);
                  setIdentityChecked(false);
                  setCreated(null);
                }, "Аккаунт MAX подключён")
              }
            >
              Подтвердить подключение
            </button>
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => {
                setPreview(null);
                setIdentityChecked(false);
              }}
            >
              Отменить проверку
            </button>
          </div>
        </section>
      )}
      {created && (
        <div className="invite-box">
          <input
            aria-label="Ссылка для MAX"
            readOnly
            value={
              created.invite_url ||
              created.url ||
              created.deep_link ||
              created.invitation_url ||
              created.token ||
              ""
            }
          />
          <button
            className="icon-button"
            aria-label="Скопировать"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(
                  created.invite_url ||
                    created.url ||
                    created.deep_link ||
                    created.invitation_url ||
                    created.token ||
                    "",
                );
              } catch {
                setError(
                  "Не удалось скопировать автоматически. Выделите ссылку и скопируйте её вручную.",
                );
              }
            }}
          >
            <Copy size={16} />
          </button>
        </div>
      )}
      {showPreferences && (
        <label className="check-field spaced">
          <input
            type="checkbox"
            checked={prefs.data?.enabled ?? false}
            disabled={busy || prefs.loading}
            onChange={(e) =>
              run(
                () =>
                  api.put(
                    "/me/dashboard-notifications",
                    { enabled: e.target.checked },
                    prefs.etag,
                  ),
                "Настройки уведомлений сохранены",
              )
            }
          />
          Уведомлять об обновлениях дашборда
        </label>
      )}
    </>
  );
}
