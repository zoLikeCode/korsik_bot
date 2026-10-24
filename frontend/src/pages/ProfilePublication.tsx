import { useEffect, useState } from "react";
import { Users, Image, CheckCheck } from "lucide-react";
import { api, errorText, items } from "../api";
import {
  Empty,
  ErrorBox,
  Field,
  Loading,
  Notice,
  Status,
  Success,
  useResource,
} from "../ui";

export default function ProfilePublication({ employee }: { employee: any }) {
  const state = useResource(`/employees/${employee.id}/profile-publication`),
    directory = useResource("/directory");
  const [selected, setSelected] = useState<string[]>([]),
    [preview, setPreview] = useState<any>(null),
    [reason, setReason] = useState(
      "Проверка представления сотрудника командой HR",
    ),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [success, setSuccess] = useState("");
  const data: any = state.data || {},
    rows = data.publications || [];
  const users = (directory.data?.users || []).filter((u: any) =>
    ["MANAGER", "HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(u.role),
  );
  useEffect(() => {
    if (
      rows.some((p: any) =>
        p.deliveries?.some((d: any) =>
          ["PENDING", "QUEUED", "PROCESSING"].includes(d.status),
        ),
      )
    ) {
      const timer = setInterval(state.reload, 5000);
      return () => clearInterval(timer);
    }
  }, [state.data]);
  async function run(action: string) {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const response = await api.post(
        `/employees/${employee.id}/profile-publication/${action}`,
        action === "preview"
          ? { recipient_user_ids: selected, reason }
          : action === "approve"
            ? {
                preview_id: preview.preview_id,
                preview_hash: preview.preview_hash,
                reason,
              }
            : { reason },
      );
      if (action === "preview") setPreview(response.data);
      else {
        setPreview(null);
        state.reload();
        setSuccess(
          action === "approve"
            ? "Представление согласовано. Статус каждой доставки показан ниже."
            : "Публикация отозвана, неотправленные сообщения остановлены.",
        );
      }
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel inset">
      <div className="panel-head">
        <h3>
          <Users size={18} /> Знакомство с командой
        </h3>
        {data.status && <Status value={data.status} />}
      </div>
      <ErrorBox error={state.error || directory.error || error} />
      {success && <Success>{success}</Success>}
      {state.loading ? (
        <Loading />
      ) : (
        <>
          {data.profile_bio || data.profile_photo_asset_id ? (
            <div className="profile-preview">
              {data.profile_photo_asset_id && (
                <img
                  src={`/api/v1/assets/${encodeURIComponent(data.profile_photo_asset_id)}/content`}
                  alt="Фото для представления команде"
                />
              )}
              <p>{data.profile_bio || "Текст о сотруднике пока не заполнен"}</p>
            </div>
          ) : (
            <Empty title="Профиль ещё не заполнен">
              Сотрудник может отправить рассказ о себе и фото в своём диалоге.
              Перед рассылкой HR проверяет содержание и выбирает получателей.
            </Empty>
          )}
          {!!(data.profile_bio || data.profile_photo_asset_id) && (
            <>
              <Field label="Кому отправить представление">
                <div className="recipient-list">
                  {users.map((u: any) => (
                    <label className="check-field" key={u.id}>
                      <input
                        type="checkbox"
                        checked={selected.includes(u.id)}
                        onChange={(e) => {
                          setSelected(
                            e.target.checked
                              ? [...selected, u.id]
                              : selected.filter((id) => id !== u.id),
                          );
                          setPreview(null);
                        }}
                      />
                      {u.name}
                      {u.id === employee.manager_id
                        ? " · руководитель сотрудника"
                        : u.id === employee.assigned_hr_id
                          ? " · назначенный HR"
                          : ""}
                    </label>
                  ))}
                </div>
              </Field>
              <Field label="Комментарий проверки">
                <input
                  minLength={3}
                  value={reason}
                  onChange={(e) => {
                    setReason(e.target.value);
                    setPreview(null);
                  }}
                />
              </Field>
              <div className="button-row wrap">
                <button
                  className="button secondary"
                  disabled={busy || !selected.length || reason.length < 3}
                  onClick={() => run("preview")}
                >
                  Предпросмотр рассылки
                </button>
                <button
                  className="button secondary"
                  disabled={busy || reason.length < 3}
                  onClick={() => run("reject")}
                >
                  Отклонить / отозвать
                </button>
              </div>
            </>
          )}
          {preview && (
            <div className="resolution">
              <h4>Перед отправкой</h4>
              <p>{preview.text}</p>
              {preview.photo_asset_id && (
                <p>
                  <Image size={15} /> Будет приложено проверенное фото
                </p>
              )}
              <ul>
                {preview.recipients?.map((u: any) => (
                  <li key={u.id}>
                    {u.name} ·{" "}
                    {u.max_connected
                      ? "MAX подключён"
                      : "MAX не подключён — сообщение не будет отправлено"}
                  </li>
                ))}
              </ul>
              <Notice>
                Профиль отправится только выбранным получателям. Предпросмотр
                фиксирует текущий текст, фото и эпизод трудоустройства; при
                изменении профиля его нужно повторить.
              </Notice>
              <button
                className="button primary"
                disabled={busy}
                onClick={() => run("approve")}
              >
                <CheckCheck size={16} />
                Согласовать и отправить выбранным
              </button>
            </div>
          )}
          {!!rows.length && (
            <>
              <h4>История представлений</h4>
              <div className="stack">
                {rows.map((p: any) => (
                  <article className="extraction-block" key={p.id}>
                    <div className="panel-head">
                      <Status value={p.status} />
                      <small className="muted">
                        Принято MAX: {p.accepted_count ?? 0}
                      </small>
                    </div>
                    {p.deliveries?.map((d: any) => (
                      <p key={d.outbox_id || d.recipient_user_id}>
                        {users.find((u: any) => u.id === d.recipient_user_id)
                          ?.name || "Получатель"}{" "}
                        · <Status value={d.status} />
                        {d.suppression_reason && (
                          <small className="muted">
                            {" "}
                            · {d.suppression_reason}
                          </small>
                        )}
                      </p>
                    ))}
                  </article>
                ))}
              </div>
            </>
          )}
          <Notice>
            Согласование представления отделено от сохранения личного профиля.
            Отзыв останавливает будущую доставку, уже принятые мессенджером
            сообщения могут остаться у получателя.
          </Notice>
        </>
      )}
    </section>
  );
}
