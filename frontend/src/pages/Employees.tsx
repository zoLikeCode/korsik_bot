import { useEffect, useRef, useState } from "react";
import { Plus, Copy, ChevronLeft, ChevronRight, Trash2 } from "lucide-react";
import { useActor } from "../App";
import EmployeeAvatar from "../EmployeeAvatar";
import RussiaTimeZoneField, {
  russiaTimeZoneLabel,
} from "../RussiaTimeZoneField";
import { api, ApiError, errorText, items } from "../api";
import {
  PageTitle,
  useResource,
  Loading,
  ErrorBox,
  Empty,
  Modal,
  Field,
  SearchInput,
  Success,
  date,
} from "../ui";
import { EmployeeOrganizationLink } from "./Organization";
import EmployeeImport from "./EmployeeImport";
import {
  employeeCreatePayload,
  employeeListPath,
  employeeDateChanges,
  employeeDatesDirty,
  employeeEffectiveEnabled,
  moscowTodayISO,
  employeeInviteUrl,
  employeeAccessLabel,
  employeeMaxLabel,
  type EmployeeCreateFields,
} from "../employeeAdministration";
import "./Employees.css";
const today = () => moscowTodayISO();

export default function Employees() {
  const actor = useActor(),
    hr = ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(actor.role);
  const [page, setPage] = useState(1),
    [query, setQuery] = useState(""),
    [stage, setStage] = useState(""),
    [create, setCreate] = useState(false),
    [importOpen, setImportOpen] = useState(false),
    [selected, setSelected] = useState<any>(null);
  const resource = useResource<any>(employeeListPath(page, query, stage));
  const total = resource.data?.total ?? 0,
    pages = Math.max(1, resource.data?.pages || 1),
    current = resource.data?.page || page;
  useEffect(() => {
    if (resource.data && current !== page) setPage(current);
  }, [resource.data]);
  return (
    <div className="employees-page">
      <PageTitle
        title="Сотрудники"
        action={
          hr ? (
            <div className="button-row">
              <button
                className="button secondary"
                onClick={() => setImportOpen(true)}
              >
                Импорт CSV
              </button>
              <button
                className="button primary"
                onClick={() => setCreate(true)}
              >
                <Plus size={18} />
                Добавить сотрудника
              </button>
            </div>
          ) : undefined
        }
      />
      <div className="toolbar employees-toolbar">
        <SearchInput
          value={query}
          onChange={(value) => {
            setQuery(value);
            setPage(1);
          }}
          placeholder="Имя или рабочая почта"
        />
        <Field label="Статус адаптации">
          <select
            value={stage}
            onChange={(event) => {
              setStage(event.target.value);
              setPage(1);
            }}
          >
            <option value="">Все сотрудники</option>
            <option value="PREBOARDING">До выхода</option>
            <option value="ACTIVE">Работают</option>
            <option value="ENDED">Найм завершён</option>
          </select>
        </Field>
        <span className="muted">Найдено: {total}</span>
      </div>
      <ErrorBox error={resource.error} />
      <section className="panel flush">
        {resource.loading ? (
          <Loading />
        ) : items(resource.data).length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Сотрудник</th>
                  <th>Дата выхода</th>
                  <th>Конец испытательного срока</th>
                  <th>Доступ</th>
                </tr>
              </thead>
              <tbody>
                {items(resource.data).map((employee) => (
                  <tr key={employee.id}>
                    <td>
                      <button
                        type="button"
                        className="employee-row-open"
                        onClick={() => setSelected(employee)}
                      >
                        <EmployeeAvatar
                          name={employee.name || ""}
                          src={employee.profile_photo_url}
                          small
                        />
                        <span>
                          <strong>{employee.name}</strong>
                          <small>
                            {employee.email || employee.position || ""}
                          </small>
                        </span>
                      </button>
                    </td>
                    <td>{date(employee.start_date)}</td>
                    <td>{date(employee.probation_end_date)}</td>
                    <td>
                      <span
                        className={`employee-access-badge ${employeeEffectiveEnabled(employee) ? "enabled" : "disabled"}`}
                      >
                        {employeeAccessLabel(
                          employeeEffectiveEnabled(employee),
                        )}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title={
              query || stage ? "Сотрудники не найдены" : "Сотрудников пока нет"
            }
          />
        )}
      </section>
      <EmployeePagination
        page={current}
        pages={pages}
        total={total}
        busy={resource.loading}
        onChange={setPage}
      />
      {create && (
        <CreateEmployee
          onClose={() => setCreate(false)}
          onCreated={(employee) => {
            setCreate(false);
            setPage(1);
            resource.reload();
            setSelected(employee);
          }}
        />
      )}
      {importOpen && (
        <EmployeeImportDialog
          onClose={() => setImportOpen(false)}
          onDone={() => {
            setPage(1);
            resource.reload();
          }}
        />
      )}
      {selected && (
        <EmployeeDetail
          employee={selected}
          hr={hr}
          onClose={() => setSelected(null)}
          onChange={resource.reload}
        />
      )}
    </div>
  );
}
function EmployeeImportDialog({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: () => void;
}) {
  const sites = useResource("/sites");
  return (
    <EmployeeImport
      sites={items(sites.data)}
      onClose={onClose}
      onDone={onDone}
    />
  );
}
export function EmployeePagination({
  page,
  pages,
  total,
  busy,
  onChange,
}: {
  page: number;
  pages: number;
  total: number;
  busy: boolean;
  onChange: (page: number) => void;
}) {
  return (
    <nav className="employee-pagination" aria-label="Пагинация сотрудников">
      <span>
        Страница {page} из {pages} · Всего {total}
      </span>
      <div>
        <button
          type="button"
          className="button secondary"
          disabled={busy || page <= 1}
          onClick={() => onChange(page - 1)}
        >
          <ChevronLeft size={17} />
          Назад
        </button>
        <button
          type="button"
          className="button secondary"
          disabled={busy || page >= pages}
          onClick={() => onChange(page + 1)}
        >
          Далее
          <ChevronRight size={17} />
        </button>
      </div>
    </nav>
  );
}
function CreateEmployee({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (employee: any) => void;
}) {
  const directory = useResource("/directory");
  const [form, setForm] = useState<EmployeeCreateFields>({
      name: "",
      email: "",
      manager_id: "",
      start_date: today(),
      probation_end_date: "",
      timezone: "Europe/Moscow",
    }),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <Modal title="Новый сотрудник" onClose={onClose}>
      <form
        className="employee-create-form"
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError("");
          try {
            const response = await api.post(
              "/employees",
              employeeCreatePayload(form),
            );
            onCreated(response.data);
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <fieldset disabled={busy}>
          <EmployeeCreateFields
            form={form}
            managers={(directory.data?.users || []).filter(
              (user: any) => user.role === "MANAGER",
            )}
            onChange={(key, value) =>
              setForm((before) => ({ ...before, [key]: value }))
            }
          />
          <ErrorBox error={error || directory.error} />
          <div className="modal-actions">
            <button
              type="button"
              className="button secondary"
              onClick={onClose}
            >
              Отмена
            </button>
            <button
              className="button primary"
              disabled={busy || !form.name.trim()}
            >
              Создать карточку
            </button>
          </div>
        </fieldset>
      </form>
    </Modal>
  );
}
export function EmployeeCreateFields({
  form,
  managers,
  onChange,
}: {
  form: EmployeeCreateFields;
  managers: any[];
  onChange: (key: keyof EmployeeCreateFields, value: string) => void;
}) {
  return (
    <>
      <Field label="Имя и фамилия">
        <input
          required
          maxLength={160}
          value={form.name}
          onChange={(event) => onChange("name", event.target.value)}
        />
      </Field>
      <Field label="Рабочая почта">
        <input
          type="email"
          value={form.email}
          onChange={(event) => onChange("email", event.target.value)}
        />
      </Field>
      <Field label="Руководитель">
        <select
          value={form.manager_id}
          onChange={(event) => onChange("manager_id", event.target.value)}
        >
          <option value="">Не назначен</option>
          {managers.map((manager) => (
            <option key={manager.id} value={manager.id}>
              {manager.name}
            </option>
          ))}
        </select>
      </Field>
      <div className="form-grid">
        <Field label="Дата выхода">
          <input
            required
            type="date"
            value={form.start_date}
            onChange={(event) => onChange("start_date", event.target.value)}
          />
        </Field>
        <Field label="Конец испытательного срока">
          <input
            type="date"
            min={form.start_date}
            value={form.probation_end_date}
            onChange={(event) =>
              onChange("probation_end_date", event.target.value)
            }
          />
        </Field>
      </div>
      <RussiaTimeZoneField
        value={form.timezone}
        onChange={(value) => onChange("timezone", value)}
      />
    </>
  );
}
function EmployeeDetail({
  employee,
  hr,
  onClose,
  onChange,
}: {
  employee: any;
  hr: boolean;
  onClose: () => void;
  onChange: () => void;
}) {
  const info = useResource<any>(`/employees/${employee.id}`);
  const [record, setRecord] = useState(employee),
    [etag, setEtag] = useState<string | null>(employee.etag || null),
    [invite, setInvite] = useState<any>(null),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false),
    [conflict, setConflict] = useState(false),
    [deletion, setDeletion] = useState<EmployeeDeletionSnapshot | null>(null),
    [deleteError, setDeleteError] = useState(""),
    [deleteConflict, setDeleteConflict] = useState(false);
  const [dates, setDates] = useState({
      start_date: employee.start_date || "",
      probation_end_date: employee.probation_end_date || "",
    }),
    [baseline, setBaseline] = useState(dates),
    [datesEtag, setDatesEtag] = useState<string | null>(employee.etag || null);
  const changes = employeeDateChanges(
      dates.start_date,
      dates.probation_end_date,
      baseline,
    ),
    dirty = employeeDatesDirty(
      dates.start_date,
      dates.probation_end_date,
      baseline,
    );
  const dirtyRef = useRef(dirty);
  const discardAfterLoad = useRef(false);
  dirtyRef.current = dirty;
  useEffect(() => {
    if (!info.data) return;
    setRecord(info.data);
    setEtag(info.etag);
    if (!dirtyRef.current || discardAfterLoad.current) {
      discardAfterLoad.current = false;
      const next = {
        start_date: info.data.start_date || "",
        probation_end_date: info.data.probation_end_date || "",
      };
      setDates(next);
      setBaseline(next);
      setDatesEtag(info.etag);
    }
  }, [info.data, info.etag]);
  useEffect(() => {
    if (
      record.max_binding_status === "VERIFIED" ||
      (!invite &&
        !["PENDING_VERIFICATION", "PENDING"].includes(
          record.max_binding_status,
        ))
    )
      return;
    const timer = setInterval(info.reload, 3000);
    return () => clearInterval(timer);
  }, [invite, record.max_binding_status]);
  const refresh = () => {
    setError("");
    setConflict(false);
    discardAfterLoad.current = true;
    info.reload();
  };
  const access = async (enabled: boolean) => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await api.patch(
        `/employees/${record.id}/administration`,
        { enabled },
        etag,
      );
      setRecord(response.data);
      setEtag(response.etag);
      info.reload();
      onChange();
      setMessage(enabled ? "Доступ включён" : "Доступ выключен");
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 412) setConflict(true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={record.name || "Сотрудник"}
      onClose={() => {
        if (!busy) onClose();
      }}
      wide
    >
      <div className="employee-detail-compact">
        {info.loading && !info.data ? (
          <Loading />
        ) : (
          <>
            <EmployeeDetailHeader
              employee={record}
              editable={hr}
              busy={busy || info.loading || !!deletion}
              onEnabledChange={access}
              onDelete={() => {
                if (!etag) {
                  setError("Дождитесь загрузки карточки сотрудника");
                  return;
                }
                setDeletion({
                  id: record.id,
                  name: record.name || "Сотрудник",
                  etag,
                });
                setDeleteError("");
                setDeleteConflict(false);
              }}
            />
            {deletion && (
              <EmployeeDeleteConfirmation
                employee={deletion}
                busy={busy}
                error={deleteError}
                conflict={deleteConflict}
                onCancel={() => setDeletion(null)}
                onReload={async () => {
                  setBusy(true);
                  try {
                    const latest = await api.get(`/employees/${deletion.id}`);
                    if (!latest.etag)
                      throw new Error(
                        "Не удалось загрузить актуальную версию карточки",
                      );
                    setRecord(latest.data);
                    setEtag(latest.etag);
                    setDeletion({
                      id: latest.data.id,
                      name: latest.data.name || "Сотрудник",
                      etag: latest.etag,
                    });
                    setDeleteConflict(false);
                    setDeleteError("");
                    info.reload();
                  } catch (failure) {
                    setDeleteError(errorText(failure));
                  } finally {
                    setBusy(false);
                  }
                }}
                onConfirm={async () => {
                  setBusy(true);
                  setDeleteError("");
                  try {
                    await deleteEmployeeRecord(deletion);
                    onChange();
                    onClose();
                  } catch (failure) {
                    setDeleteError(errorText(failure));
                    if (failure instanceof ApiError && failure.status === 412)
                      setDeleteConflict(true);
                  } finally {
                    setBusy(false);
                  }
                }}
              />
            )}

            <ErrorBox error={error || info.error} />
            {message && <Success>{message}</Success>}
            {conflict && (
              <button
                type="button"
                className="button secondary"
                disabled={busy}
                onClick={refresh}
              >
                Загрузить актуальные данные
              </button>
            )}
            <form
              className="employee-dates"
              onSubmit={async (event) => {
                event.preventDefault();
                if (!dirty) return;
                setBusy(true);
                setError("");
                setMessage("");
                try {
                  const response = await api.patch(
                    `/employees/${record.id}/administration`,
                    changes,
                    datesEtag,
                  );
                  setRecord(response.data);
                  setEtag(response.etag);
                  const next = {
                    start_date: response.data.start_date || "",
                    probation_end_date: response.data.probation_end_date || "",
                  };
                  setDates(next);
                  setBaseline(next);
                  setDatesEtag(response.etag);
                  dirtyRef.current = false;
                  info.reload();
                  onChange();
                  setMessage("Даты сохранены");
                } catch (e) {
                  setError(errorText(e));
                  if (e instanceof ApiError && e.status === 412)
                    setConflict(true);
                } finally {
                  setBusy(false);
                }
              }}
            >
              <fieldset
                disabled={
                  !hr ||
                  busy ||
                  !!deletion ||
                  record.employment_stage === "ENDED"
                }
              >
                <div className="form-grid">
                  <Field label="Дата выхода">
                    <input
                      required
                      type="date"
                      value={dates.start_date}
                      onChange={(event) =>
                        setDates((before) => ({
                          ...before,
                          start_date: event.target.value,
                        }))
                      }
                    />
                  </Field>
                  <Field label="Конец испытательного срока">
                    <input
                      required
                      type="date"
                      min={dates.start_date}
                      value={dates.probation_end_date}
                      onChange={(event) =>
                        setDates((before) => ({
                          ...before,
                          probation_end_date: event.target.value,
                        }))
                      }
                    />
                  </Field>
                </div>
                {hr && record.employment_stage !== "ENDED" && (
                  <button
                    className="button secondary"
                    disabled={
                      busy ||
                      conflict ||
                      !dirty ||
                      !dates.start_date ||
                      !dates.probation_end_date
                    }
                  >
                    Сохранить даты
                  </button>
                )}
              </fieldset>
              <span className="employee-zone-label">
                {russiaTimeZoneLabel(record.timezone)}
              </span>
            </form>
            {record.employment_stage === "ENDED" ? (
              <section className="panel inset">
                <h3>Оргструктура</h3>
                <p>Найм завершён</p>
              </section>
            ) : (
              <EmployeeOrganizationLink id={record.id} />
            )}
            <section className="panel inset employee-max-card">
              <h3>Подключение к MAX</h3>
              <p>
                <span
                  className={`employee-access-badge ${record.max_binding_status === "VERIFIED" ? "enabled" : "disabled"}`}
                >
                  {employeeMaxLabel(record.max_binding_status)}
                </span>
              </p>
              {hr && record.max_binding_status !== "VERIFIED" && !invite && (
                <button
                  type="button"
                  className="button secondary"
                  disabled={
                    busy ||
                    !!deletion ||
                    record.enabled === false ||
                    record.employment_stage === "ENDED"
                  }
                  onClick={async () => {
                    setBusy(true);
                    setError("");
                    try {
                      const response = await api.post(
                        `/employees/${record.id}/binding-invitations`,
                        {},
                      );
                      setInvite(response.data);
                      info.reload();
                      onChange();
                      setMessage(
                        "Ссылка готова. Подключение выполнится автоматически после перехода сотрудника.",
                      );
                    } catch (e) {
                      setError(errorText(e));
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  Создать приглашение
                </button>
              )}
              {invite && record.max_binding_status !== "VERIFIED" && (
                <EmployeeInvitation
                  invite={invite}
                  onCopied={() => setMessage("Ссылка скопирована")}
                  onError={setError}
                />
              )}
            </section>
          </>
        )}
      </div>
    </Modal>
  );
}
export function EmployeeDetailHeader({
  employee,
  editable,
  busy,
  onEnabledChange,
  onDelete,
}: {
  employee: any;
  editable: boolean;
  busy: boolean;
  onEnabledChange: (enabled: boolean) => void;
  onDelete?: () => void;
}) {
  return (
    <div className="employee-detail-heading">
      <div className="employee-detail-person">
        <EmployeeAvatar
          name={employee.name || ""}
          src={employee.profile_photo_url}
        />
        <div>
          <h3>{employee.name}</h3>
          {employee.email && <p>{employee.email}</p>}
        </div>
      </div>
      <div className="employee-detail-controls">
        <Field label="Доступ">
          <select
            className={`employee-access-select ${employeeEffectiveEnabled(employee) ? "enabled" : "disabled"}`}
            value={employeeEffectiveEnabled(employee) ? "true" : "false"}
            disabled={
              !editable || busy || employee.employment_stage === "ENDED"
            }
            onChange={(event) => onEnabledChange(event.target.value === "true")}
          >
            <option value="true">Активен</option>
            <option value="false">Выключен</option>
          </select>
        </Field>
        {editable && onDelete && (
          <button
            type="button"
            className="icon-button danger employee-delete-button"
            aria-label="Удалить сотрудника"
            title="Удалить сотрудника"
            disabled={busy}
            onClick={onDelete}
          >
            <Trash2 size={19} aria-hidden="true" />
          </button>
        )}
      </div>
    </div>
  );
}
export type EmployeeDeletionSnapshot = {
  id: string;
  name: string;
  etag: string;
};
export async function deleteEmployeeRecord(snapshot: EmployeeDeletionSnapshot) {
  if (!snapshot.etag) throw new Error("Дождитесь загрузки карточки сотрудника");
  return api.delete(
    `/employees/${encodeURIComponent(snapshot.id)}`,
    snapshot.etag,
  );
}
export function EmployeeDeleteConfirmation({
  employee,
  busy,
  error,
  conflict,
  onConfirm,
  onCancel,
  onReload,
}: {
  employee: EmployeeDeletionSnapshot;
  busy: boolean;
  error: string;
  conflict: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  onReload: () => void;
}) {
  return (
    <section
      className="employee-delete-confirmation"
      role="alertdialog"
      aria-labelledby="employee-delete-title"
      aria-describedby="employee-delete-description"
    >
      <h3 id="employee-delete-title">
        Удалить «{employee.name}» из списка сотрудников?
      </h3>
      <p id="employee-delete-description">
        Сотрудник исчезнет из списка, доступ к кабинету и боту будет выключен.
        История адаптации и ответов сохранится.
      </p>
      <ErrorBox error={error} />
      {conflict && (
        <button
          type="button"
          className="button secondary"
          disabled={busy}
          onClick={onReload}
        >
          Загрузить актуальные данные
        </button>
      )}
      <div className="modal-actions">
        <button
          type="button"
          className="button secondary"
          disabled={busy}
          onClick={onCancel}
        >
          Отмена
        </button>
        <button
          type="button"
          className="button danger"
          disabled={busy || conflict}
          onClick={onConfirm}
        >
          {busy ? "Удаляем…" : "Удалить сотрудника"}
        </button>
      </div>
    </section>
  );
}
export function EmployeeInvitation({
  invite,
  onCopied,
  onError,
}: {
  invite: any;
  onCopied: () => void;
  onError: (error: string) => void;
}) {
  const url = employeeInviteUrl(invite);
  return url ? (
    <div className="employee-invitation">
      <Field label="Ссылка для сотрудника">
        <input
          readOnly
          value={url}
          onFocus={(event) => event.target.select()}
        />
      </Field>
      <button
        type="button"
        className="button secondary"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(url);
            onCopied();
          } catch (e) {
            onError(errorText(e));
          }
        }}
      >
        <Copy size={17} />
        Скопировать ссылку
      </button>
    </div>
  ) : (
    <ErrorBox error="Сервер не вернул ссылку приглашения" />
  );
}
