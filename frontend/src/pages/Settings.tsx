import { useEffect, useState } from "react";
import {
  Bell,
  ShieldCheck,
  CheckCheck,
  Server,
  Plus,
  CalendarClock,
  RefreshCw,
} from "lucide-react";
import { useActor } from "../App";
import SlaPolicy from "./SlaPolicy";
import PrincipalBindings from "./PrincipalBindings";
import LocalAi from "./LocalAi";
import KnowledgeRetention from "./KnowledgeRetention";
import { HrContactEditor } from "./HrContact";
import { api, items, errorText } from "../api";
import {
  PageTitle,
  useResource,
  Loading,
  ErrorBox,
  Success,
  Field,
  Notice,
  Status,
  Empty,
  JSONView,
  Modal,
} from "../ui";
export default function SettingsPage() {
  const actor = useActor(),
    admin = actor.role === "PLATFORM_ADMIN";
  return (
    <>
      <PageTitle title="Настройки" />
      <div className="settings-grid">
        <section className="panel">
          <div className="panel-head">
            <h2>
              <Bell size={20} />
              Уведомления в MAX
            </h2>
          </div>
          {actor.role !== "EMPLOYEE" && !admin ? (
            <PrincipalBindings showPreferences />
          ) : (
            <Notice>
              Уведомления сотрудника приходят в его основной диалог MAX.
            </Notice>
          )}
        </section>
        <section className="panel">
          <div className="panel-head">
            <h2>
              <ShieldCheck size={20} />
              Ваш доступ
            </h2>
          </div>
          <dl className="definition-list">
            <dt>Пользователь</dt>
            <dd>{actor.name}</dd>
            <dt>Почта</dt>
            <dd>{actor.email}</dd>
            <dt>Роль</dt>
            <dd>{actor.role}</dd>
            <dt>Авторизация</dt>
            <dd>Защищённая локальная сессия</dd>
          </dl>
          <Notice>
            Права и область данных проверяются сервером для каждого действия.
          </Notice>
        </section>
      </div>
      {["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(actor.role) && (
        <>
          <DigestSchedules />
          <SitesDirectory />
          {actor.role === "HR_ADMIN" && <WorkCalendars />}
          <SlaPolicy />
        </>
      )}
      {actor.role === "HR_ADMIN" && actor.scope?.all === true && (
        <KnowledgeRetention />
      )}
      {admin && <SystemStatus />}
      {["HR_ADMIN", "PLATFORM_ADMIN"].includes(actor.role) && <LocalAi />}
      {["HR_ADMIN", "PLATFORM_ADMIN"].includes(actor.role) && <UserAccounts />}
    </>
  );
}
function SitesDirectory() {
  const actor = useActor();
  const globalAdmin = actor.role === "HR_ADMIN" && actor.scope?.all === true;
  const canEdit =
    actor.role === "HR_ADMIN" &&
    (globalAdmin ||
      (!actor.scope?.site_ids?.length &&
        !!(
          actor.scope?.legal_entity_ids?.length || actor.scope?.legal_entity_id
        )));
  const sites = useResource("/sites"),
    legal = useResource("/legal-entities");
  const [name, setName] = useState(""),
    [code, setCode] = useState(""),
    [legalId, setLegalId] = useState(""),
    [timezone, setTimezone] = useState("Europe/Moscow"),
    [entityName, setEntityName] = useState(""),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Площадки и юридические лица</h2>
        </div>
      </div>
      <ErrorBox error={error || sites.error || legal.error} />
      {success && <Success>{success}</Success>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Площадка</th>
              <th>Юридическое лицо</th>
              <th>Часовой пояс</th>
              <th>Календарь</th>
            </tr>
          </thead>
          <tbody>
            {items(sites.data).map((s) => (
              <tr key={s.id}>
                <td>
                  <strong>{s.name}</strong>
                </td>
                <td>
                  {items(legal.data).find((l) => l.id === s.legal_entity_id)
                    ?.name || "Не указано"}
                </td>
                <td>{s.timezone || "Не указан"}</td>
                <td>{s.calendar_id ? "Настроен" : "Нужно настроить"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {canEdit && (
        <>
          <details className="advanced">
            <summary>Добавить площадку</summary>
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                setBusy(true);
                setError("");
                try {
                  await api.post("/sites", {
                    name,
                    code: code || undefined,
                    legal_entity_id: legalId,
                    timezone,
                  });
                  sites.reload();
                  setName("");
                  setCode("");
                  setSuccess(
                    "Площадка создана. Ниже создайте и активируйте её рабочий календарь, затем укажите аудиторию в сценарии и документах.",
                  );
                } catch (e) {
                  setError(errorText(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              <div className="form-grid">
                <Field label="Название площадки">
                  <input
                    required
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                  />
                </Field>
                <Field label="Короткое обозначение">
                  <input
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                    placeholder="Например, Казань"
                  />
                </Field>
                <Field label="Юридическое лицо">
                  <select
                    required
                    value={legalId}
                    onChange={(e) => setLegalId(e.target.value)}
                  >
                    <option value="">Выберите юридическое лицо</option>
                    {items(legal.data).map((l) => (
                      <option key={l.id} value={l.id}>
                        {l.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Часовой пояс">
                  <input
                    required
                    value={timezone}
                    onChange={(e) => setTimezone(e.target.value)}
                  />
                </Field>
              </div>
              <button className="button primary" disabled={busy}>
                <Plus size={16} />
                Создать площадку
              </button>
            </form>
          </details>
          {globalAdmin && (
            <details className="advanced">
              <summary>Добавить юридическое лицо</summary>
              <form
                className="inline-form"
                onSubmit={async (e) => {
                  e.preventDefault();
                  setBusy(true);
                  setError("");
                  try {
                    const r = await api.post("/legal-entities", {
                      name: entityName,
                    });
                    legal.reload();
                    setLegalId(r.data.id);
                    setEntityName("");
                    setSuccess("Юридическое лицо добавлено");
                  } catch (e) {
                    setError(errorText(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <input
                  aria-label="Название юридического лица"
                  required
                  value={entityName}
                  onChange={(e) => setEntityName(e.target.value)}
                />
                <button className="button secondary" disabled={busy}>
                  Добавить
                </button>
              </form>
            </details>
          )}
        </>
      )}
    </section>
  );
}
function UserAccounts() {
  const actor = useActor(),
    users = useResource("/users"),
    sites = useResource("/sites"),
    employees = useResource(actor.role === "HR_ADMIN" ? "/employees" : null);
  const [form, setForm] = useState({
      name: "",
      email: "",
      password: "",
      role: actor.role === "PLATFORM_ADMIN" ? "MANAGER" : "HR_SPECIALIST",
      site_id: "",
      employee_id: "",
      employee_drilldown: false,
    }),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  const [selectedUser, setSelectedUser] = useState<any>(null);
  const [contactUser, setContactUser] = useState<any>(null);
  const roleNames: Record<string, string> = {
    HR_ADMIN: "Администратор HR",
    HR_SPECIALIST: "HR-специалист",
    MANAGER: "Руководитель подразделения",
    HEAD_TALENT: "Руководитель подбора",
    EMPLOYEE: "Сотрудник",
    PLATFORM_ADMIN: "Технический администратор",
  };
  const allowed =
    actor.role === "PLATFORM_ADMIN"
      ? ["MANAGER", "PLATFORM_ADMIN"]
      : actor.scope?.all
        ? Object.keys(roleNames)
        : ["HR_SPECIALIST", "MANAGER", "HEAD_TALENT", "EMPLOYEE"];
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Учётные записи и роли</h2>
        </div>
      </div>
      <ErrorBox error={error || users.error} />
      {success && <Success>{success}</Success>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Пользователь</th>
              <th>Почта</th>
              <th>Роль</th>
              <th>Доступ</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items(users.data).map((u) => (
              <tr key={u.id}>
                <td>
                  <strong>{u.name}</strong>
                </td>
                <td>{u.email}</td>
                <td>{roleNames[u.role] || u.role}</td>
                <td>
                  <Status value={u.enabled ? "ACTIVE" : "DISABLED"} />
                </td>
                <td>
                  {u.id !== actor.id && (
                    <button
                      className="text-button"
                      onClick={() => setSelectedUser(u)}
                    >
                      Управлять доступом
                    </button>
                  )}
                  {actor.role === "HR_ADMIN" &&
                    ["HR_ADMIN", "HR_SPECIALIST"].includes(u.role) && (
                      <button
                        className="text-button"
                        onClick={() => setContactUser(u)}
                      >
                        Контакт HR в MAX
                      </button>
                    )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selectedUser && (
        <UserAccess
          user={selectedUser}
          onClose={() => setSelectedUser(null)}
          onDone={() => {
            setSelectedUser(null);
            users.reload();
          }}
        />
      )}
      {contactUser && (
        <HrContactEditor
          user={contactUser}
          onClose={() => setContactUser(null)}
        />
      )}
      <details className="advanced">
        <summary>Добавить учётную запись</summary>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            setSuccess("");
            try {
              await api.post("/users", {
                name: form.name,
                email: form.email,
                password: form.password,
                role: form.role,
                scope: {
                  ...(form.site_id
                    ? { site_ids: [form.site_id] }
                    : actor.scope?.all
                      ? { all: true }
                      : actor.scope),
                  ...(form.role === "HEAD_TALENT"
                    ? { employee_drilldown: form.employee_drilldown }
                    : {}),
                },
                employee_id:
                  form.role === "EMPLOYEE"
                    ? form.employee_id || undefined
                    : undefined,
              });
              setForm({
                ...form,
                name: "",
                email: "",
                password: "",
                employee_id: "",
                employee_drilldown: false,
              });
              users.reload();
              setSuccess(
                "Учётная запись создана. Передайте сотруднику доступ через принятый в организации защищённый канал.",
              );
            } catch (e) {
              setError(errorText(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          <div className="form-grid">
            <Field label="Имя и фамилия">
              <input
                required
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </Field>
            <Field label="Рабочая почта">
              <input
                type="email"
                autoComplete="off"
                required
                value={form.email}
                onChange={(e) => setForm({ ...form, email: e.target.value })}
              />
            </Field>
            <Field label="Роль">
              <select
                value={form.role}
                onChange={(e) => setForm({ ...form, role: e.target.value })}
              >
                {allowed.map((role) => (
                  <option key={role} value={role}>
                    {roleNames[role]}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Область доступа">
              <select
                value={form.site_id}
                onChange={(e) => setForm({ ...form, site_id: e.target.value })}
              >
                <option value="">Вся моя разрешённая область</option>
                {items(sites.data)
                  .filter(
                    (s) =>
                      !actor.scope?.site_ids?.length ||
                      actor.scope.site_ids.includes(s.id),
                  )
                  .map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
              </select>
            </Field>
          </div>
          {form.role === "HEAD_TALENT" && (
            <label className="check-field">
              <input
                type="checkbox"
                checked={form.employee_drilldown}
                disabled={
                  !actor.scope?.all && actor.scope?.employee_drilldown !== true
                }
                onChange={(e) =>
                  setForm({ ...form, employee_drilldown: e.target.checked })
                }
              />
              Разрешить просмотр карточек сотрудников в выбранной области. По
              умолчанию доступны только агрегаты.
            </label>
          )}
          {form.role === "EMPLOYEE" && (
            <Field label="Карточка сотрудника">
              <select
                required
                value={form.employee_id}
                onChange={(e) =>
                  setForm({ ...form, employee_id: e.target.value })
                }
              >
                <option value="">Выберите сотрудника</option>
                {items(employees.data).map((e) => (
                  <option value={e.id} key={e.id}>
                    {e.name}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field
            label="Начальный пароль"
            hint="Не меньше 12 символов. Пароль не сохраняется в браузере и не появляется в списке пользователей."
          >
            <input
              type="password"
              minLength={12}
              maxLength={256}
              required
              autoComplete="new-password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
            />
          </Field>
          <button className="button primary" disabled={busy}>
            <Plus size={16} />
            {busy ? "Создаём…" : "Создать учётную запись"}
          </button>
        </form>
      </details>
    </section>
  );
}
function UserAccess({
  user,
  onClose,
  onDone,
}: {
  user: any;
  onClose: () => void;
  onDone: () => void;
}) {
  const state = useResource(`/users/${user.id}`);
  const [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const current: any = state.data || user;
  return (
    <Modal title="Доступ к учётной записи" onClose={onClose}>
      <h3>{current.name}</h3>
      <p>{current.email}</p>
      <Status value={current.enabled ? "ACTIVE" : "DISABLED"} />
      <Notice>
        {current.enabled
          ? "Отключение завершит действующие сессии пользователя. История рабочих данных сохранится."
          : "Включение вернёт существующую роль и область доступа. Для повторного найма сначала оформите новый период в карточке сотрудника и проверьте назначения."}
      </Notice>
      <Field label="Причина изменения доступа">
        <textarea
          minLength={3}
          required
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </Field>
      <ErrorBox error={error || state.error} />
      <div className="modal-actions">
        <button className="button secondary" onClick={onClose}>
          Отмена
        </button>
        <button
          className="button primary"
          disabled={busy || state.loading || !!state.error || reason.length < 3}
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              await api.patch(
                `/users/${user.id}`,
                { enabled: !current.enabled, reason },
                state.etag || current.etag,
              );
              onDone();
            } catch (e) {
              setError(errorText(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          {current.enabled ? "Отключить доступ" : "Включить доступ"}
        </button>
      </div>
    </Modal>
  );
}

function WorkCalendars() {
  const state = useResource("/work-calendars"),
    sites = useResource("/sites");
  const [id, setId] = useState(""),
    [form, setForm] = useState({
      timezone: "Europe/Moscow",
      coverage_start: new Date().toISOString().slice(0, 10),
      coverage_end: `${new Date().getFullYear() + 2}-12-31`,
      working_weekdays: [0, 1, 2, 3, 4],
      start_time: "09:00",
      end_time: "18:00",
      exceptions: [] as any[],
    }),
    [newName, setNewName] = useState(""),
    [siteId, setSiteId] = useState(""),
    [versionId, setVersionId] = useState(""),
    [preview, setPreview] = useState<any>(null),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  const rows = items(state.data);
  useEffect(() => {
    if (!id && rows.length) setId(rows[0].id);
  }, [state.data]);
  useEffect(() => {
    const current = rows.find((c) => c.id === id),
      v =
        current?.versions?.find(
          (v: any) => v.id === current.active_version_id,
        ) || current?.versions?.at(-1);
    if (v)
      setForm({
        timezone: v.timezone || "Europe/Moscow",
        coverage_start: v.coverage_start || "2026-01-01",
        coverage_end: v.coverage_end || "2028-12-31",
        working_weekdays: v.working_weekdays || [0, 1, 2, 3, 4],
        start_time: v.weekly_intervals?.[0]?.intervals?.[0]?.start || "09:00",
        end_time: v.weekly_intervals?.[0]?.intervals?.[0]?.end || "18:00",
        exceptions: v.exceptions || [],
      });
    setPreview(null);
    setVersionId("");
  }, [id]);
  const change = (p: any) => {
    setForm({ ...form, ...p });
    setPreview(null);
    setVersionId("");
  };
  async function run(fn: () => Promise<any>, message: string) {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await fn();
      setSuccess(message);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>
            <CalendarClock size={20} />
            Рабочие календари
          </h2>
        </div>
      </div>
      <ErrorBox error={error || state.error} />
      {success && <Success>{success}</Success>}
      <Field label="Календарь">
        <select value={id} onChange={(e) => setId(e.target.value)}>
          <option value="">Выберите календарь</option>
          {rows.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </Field>
      {id && (
        <>
          <div className="form-grid">
            <Field label="Покрытие с">
              <input
                required
                type="date"
                value={form.coverage_start}
                onChange={(e) => change({ coverage_start: e.target.value })}
              />
            </Field>
            <Field label="Покрытие по">
              <input
                required
                type="date"
                value={form.coverage_end}
                onChange={(e) => change({ coverage_end: e.target.value })}
              />
            </Field>
            <Field label="Рабочее время с">
              <input
                required
                type="time"
                value={form.start_time}
                onChange={(e) => change({ start_time: e.target.value })}
              />
            </Field>
            <Field label="Рабочее время до">
              <input
                required
                type="time"
                value={form.end_time}
                onChange={(e) => change({ end_time: e.target.value })}
              />
            </Field>
          </div>
          <Field label="Часовой пояс">
            <input
              value={form.timezone}
              onChange={(e) => change({ timezone: e.target.value })}
            />
          </Field>
          <div className="weekday-grid">
            {["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((name, i) => (
              <label className="check-field" key={i}>
                <input
                  type="checkbox"
                  checked={form.working_weekdays.includes(i)}
                  onChange={(e) =>
                    change({
                      working_weekdays: e.target.checked
                        ? [...form.working_weekdays, i].sort()
                        : form.working_weekdays.filter((v) => v !== i),
                    })
                  }
                />
                {name}
              </label>
            ))}
          </div>
          <h4>Исключения: праздники и рабочие выходные</h4>
          {form.exceptions.map((ex: any, i: number) => (
            <div className="inline-form" key={i}>
              <input
                aria-label="Дата исключения"
                type="date"
                value={ex.date}
                onChange={(e) =>
                  change({
                    exceptions: form.exceptions.map((v, j) =>
                      i === j ? { ...v, date: e.target.value } : v,
                    ),
                  })
                }
              />
              <select
                aria-label="Тип дня"
                value={ex.is_working ? "WORKING" : "OFF"}
                onChange={(e) =>
                  change({
                    exceptions: form.exceptions.map((v, j) =>
                      i === j
                        ? { ...v, is_working: e.target.value === "WORKING" }
                        : v,
                    ),
                  })
                }
              >
                <option value="OFF">Выходной</option>
                <option value="WORKING">Рабочий</option>
              </select>
              <button
                className="text-button"
                onClick={() =>
                  change({
                    exceptions: form.exceptions.filter((_, j) => i !== j),
                  })
                }
              >
                Убрать
              </button>
            </div>
          ))}
          <button
            className="text-button"
            onClick={() =>
              change({
                exceptions: [
                  ...form.exceptions,
                  {
                    date: new Date().toISOString().slice(0, 10),
                    is_working: false,
                  },
                ],
              })
            }
          >
            <Plus size={14} />
            Добавить дату
          </button>
          <div className="modal-actions">
            <button
              className="button secondary"
              disabled={busy || form.working_weekdays.length === 0}
              onClick={() =>
                run(async () => {
                  const r = await api.post(`/work-calendars/${id}/versions`, {
                    timezone: form.timezone,
                    coverage_start: form.coverage_start,
                    coverage_end: form.coverage_end,
                    working_weekdays: form.working_weekdays,
                    weekly_intervals: form.working_weekdays.map((day) => ({
                      weekday: day + 1,
                      intervals: [
                        { start: form.start_time, end: form.end_time },
                      ],
                    })),
                    exceptions: form.exceptions,
                    dated_exceptions: form.exceptions.map((ex) => ({
                      date: ex.date,
                      is_working: ex.is_working,
                      intervals: ex.is_working
                        ? [{ start: form.start_time, end: form.end_time }]
                        : [],
                    })),
                  });
                  setVersionId(r.data.id);
                  setPreview(
                    (
                      await api.post(
                        `/work-calendars/${id}/versions/${r.data.id}/preview-impact`,
                        {},
                      )
                    ).data,
                  );
                }, "Черновик календаря создан, последствия рассчитаны")
              }
            >
              Проверить последствия
            </button>
            <button
              className="button primary"
              disabled={busy || !preview || !versionId}
              onClick={() =>
                run(async () => {
                  await api.post(
                    `/work-calendars/${id}/versions/${versionId}/activate`,
                    {
                      preview_id: preview.preview_id,
                      preview_hash: preview.preview_hash,
                    },
                  );
                  state.reload();
                  setPreview(null);
                  setVersionId("");
                }, "Календарь активирован, будущие шаги пересчитаны")
              }
            >
              Активировать календарь
            </button>
          </div>
          {preview && (
            <Notice>
              Изменение затронет {preview.impact?.length ?? 0} активных
              маршрутов. Существующие SLA обращений сохраняют свою версию
              календаря.
            </Notice>
          )}
        </>
      )}
      <details className="advanced">
        <summary>Добавить календарь для площадки</summary>
        <div className="form-grid">
          <Field label="Название">
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
            />
          </Field>
          <Field label="Площадка">
            <select value={siteId} onChange={(e) => setSiteId(e.target.value)}>
              <option value="">Без привязки</option>
              {items(sites.data).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <button
          className="button secondary"
          disabled={busy || !newName}
          onClick={() =>
            run(async () => {
              const r = await api.post("/work-calendars", {
                name: newName,
                site_id: siteId || undefined,
              });
              setId(r.data.id);
              state.reload();
              setNewName("");
            }, "Календарь создан. Задайте его рабочие дни и активируйте версию.")
          }
        >
          Создать календарь
        </button>
      </details>
    </section>
  );
}
function DigestSchedules() {
  const state = useResource("/reports/digest-schedules"),
    directory = useResource("/directory");
  const [form, setForm] = useState({
      name: "Еженедельный дайджест адаптации",
      weekday: 1,
      local_time: "10:00",
      timezone: "Europe/Moscow",
      recipient_ids: "",
    }),
    [preview, setPreview] = useState<any>(null),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  const body = () => ({
    ...form,
    recipient_ids: form.recipient_ids
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
    scope: { site_ids: [] },
    channels: ["HR_PANEL", "MAX"],
    enabled: true,
    reason: "Настройка отчёта HR",
  });
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>
            <CalendarClock size={20} />
            Расписание отчётов
          </h2>
        </div>
      </div>
      <p className="muted">
        Выберите день и время. Персональные напоминания анкет работают отдельно
        от этого расписания.
      </p>
      <ErrorBox error={error || state.error} />
      {success && <Success>{success}</Success>}
      {items(state.data).map((s) => (
        <div className="list-row" key={s.id}>
          <div>
            <strong>{s.name || "Недельный дайджест"}</strong>
            <p>
              {s.local_time} · {s.timezone}
            </p>
          </div>
          <Status value={s.status || "ACTIVE"} />
        </div>
      ))}
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          try {
            const r = await api.post("/reports/digest-schedules/preview", {
              schedule: body(),
              occurrence_count: 3,
            });
            setPreview(r.data);
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="form-grid">
          <Field label="День отправки">
            <select
              value={form.weekday}
              onChange={(e) => {
                setForm({ ...form, weekday: Number(e.target.value) });
                setPreview(null);
              }}
            >
              {[
                "Понедельник",
                "Вторник",
                "Среда",
                "Четверг",
                "Пятница",
                "Суббота",
                "Воскресенье",
              ].map((v, i) => (
                <option value={i + 1} key={i}>
                  {v}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Местное время">
            <input
              required
              type="time"
              value={form.local_time}
              onChange={(e) => {
                setForm({ ...form, local_time: e.target.value });
                setPreview(null);
              }}
            />
          </Field>
          <Field label="Часовой пояс">
            <input
              required
              value={form.timezone}
              onChange={(e) => {
                setForm({ ...form, timezone: e.target.value });
                setPreview(null);
              }}
            />
          </Field>
          <Field label="Получатель отчёта">
            <select
              required
              value={form.recipient_ids}
              onChange={(e) => {
                setForm({ ...form, recipient_ids: e.target.value });
                setPreview(null);
              }}
            >
              <option value="">Выберите получателя</option>
              {(directory.data?.users || [])
                .filter((u: any) =>
                  [
                    "HR_ADMIN",
                    "HR_SPECIALIST",
                    "HEAD_TALENT",
                    "MANAGER",
                  ].includes(u.role),
                )
                .map((u: any) => (
                  <option key={u.id} value={u.id}>
                    {u.name} · {u.email}
                  </option>
                ))}
            </select>
          </Field>
        </div>
        <div className="button-row">
          <button className="button secondary" disabled={busy}>
            Показать ближайшие запуски
          </button>
          <button
            className="button primary"
            type="button"
            disabled={busy || !preview}
            onClick={async () => {
              setBusy(true);
              setError("");
              try {
                await api.post("/reports/digest-schedules", {
                  ...body(),
                  preview_id: preview.preview_id,
                  preview_hash: preview.preview_hash,
                });
                state.reload();
                setSuccess("Расписание сохранено");
                setPreview(null);
              } catch (e) {
                setError(errorText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            <Plus size={16} />
            Сохранить расписание
          </button>
        </div>
      </form>
      {preview && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Отправка, местное время</th>
                <th>Отчётный период</th>
              </tr>
            </thead>
            <tbody>
              {(preview.occurrences || []).map((o: any, i: number) => (
                <tr key={i}>
                  <td>
                    {new Date(o.due_local || o.due_at).toLocaleString("ru-RU")}
                  </td>
                  <td>
                    {new Date(
                      o.period_from_local || o.period_from,
                    ).toLocaleDateString("ru-RU")}{" "}
                    —{" "}
                    {new Date(
                      o.period_to_local || o.period_to,
                    ).toLocaleDateString("ru-RU")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
function SystemStatus() {
  const health = useResource("/system/status"),
    jobs = useResource("/jobs"),
    audit = useResource("/audit-events");
  return (
    <>
      <section className="panel">
        <div className="panel-head">
          <h2>
            <Server size={20} />
            Состояние системы
          </h2>
          <button className="button secondary compact" onClick={health.reload}>
            <RefreshCw size={15} />
            Обновить
          </button>
        </div>
        <ErrorBox error={health.error} />
        {health.loading ? <Loading /> : <JSONView data={health.data} />}
      </section>
      <section className="panel">
        <h2>Фоновые задания</h2>
        <ErrorBox error={jobs.error} />
        {items(jobs.data).length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Тип</th>
                  <th>Статус</th>
                  <th>Попыток</th>
                  <th>Ошибка</th>
                </tr>
              </thead>
              <tbody>
                {items(jobs.data).map((j) => (
                  <tr key={j.id}>
                    <td>{j.kind}</td>
                    <td>
                      <Status value={j.status} />
                    </td>
                    <td>{j.attempts}</td>
                    <td>{j.error || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="Фоновых заданий пока нет" />
        )}
      </section>
      <section className="panel">
        <h2>Аудит действий</h2>
        <ErrorBox error={audit.error} />
        {items(audit.data).length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Время</th>
                  <th>Действие</th>
                  <th>Объект</th>
                  <th>Основание</th>
                </tr>
              </thead>
              <tbody>
                {items(audit.data).map((e) => (
                  <tr key={e.id}>
                    <td>{new Date(e.created_at).toLocaleString("ru-RU")}</td>
                    <td>{e.action}</td>
                    <td>{e.entity_id || "—"}</td>
                    <td>{e.reason || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="Событий аудита пока нет" />
        )}
      </section>
    </>
  );
}
