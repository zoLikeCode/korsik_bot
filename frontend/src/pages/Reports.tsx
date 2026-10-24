import { useEffect, useState } from "react";
import { Download, FileBarChart, RefreshCcw } from "lucide-react";
import TimeZoneField from "../TimeZoneField";
import { useActor } from "../App";
import { api, apiSource, errorText, items } from "../api";
import {
  Empty,
  ErrorBox,
  Field,
  Loading,
  Notice,
  Status,
  Success,
  date,
  useResource,
} from "../ui";
export default function Reports() {
  const actor = useActor(),
    canPeople =
      ["HR_ADMIN", "HR_SPECIALIST", "MANAGER"].includes(actor.role) ||
      (actor.role === "HEAD_TALENT" &&
        actor.scope?.employee_drilldown === true);
  const sites = useResource("/sites"),
    [site, setSite] = useState(""),
    [from, setFrom] = useState(""),
    [timezone, setTimezone] = useState("Europe/Moscow"),
    [to, setTo] = useState(""),
    [cohortFrom, setCohortFrom] = useState(""),
    [cohortTo, setCohortTo] = useState(""),
    [stage, setStage] = useState(""),
    [format, setFormat] = useState("CSV"),
    [dataset, setDataset] = useState(
      actor.role === "HEAD_TALENT" ? "RISK_TOPICS" : "ADAPTATION",
    ),
    [reason, setReason] = useState("Отчёт для сопровождения адаптации"),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  const params = new URLSearchParams({ timezone });
  if (site) params.set("site_id", site);
  if (from) params.set("period_from", from);
  if (to) params.set("period_to", to);
  const risk = useResource(
    actor.role !== "MANAGER" ? `/reports/risk-topics?${params}` : null,
  );
  const adaptationParams = new URLSearchParams({ timezone });
  if (site) adaptationParams.set("site_id", site);
  if (cohortFrom) adaptationParams.set("cohort_from", cohortFrom);
  if (cohortTo) adaptationParams.set("cohort_to", cohortTo);
  const adaptation = useResource(
    canPeople ? `/reports/adaptation?${adaptationParams}` : null,
  );
  const exports = useResource("/reports/exports");
  const pending = items(exports.data).some((r) =>
    ["PENDING", "QUEUED", "RUNNING", "SCHEDULED"].includes(r.status),
  );
  useEffect(() => {
    if (pending) {
      const timer = setInterval(exports.reload, 3000);
      return () => clearInterval(timer);
    }
  }, [pending]);
  const allowedDatasets: Record<string, string> = {
    ...(canPeople ? { ADAPTATION: "Статусы адаптации" } : {}),
    ...(actor.role !== "MANAGER"
      ? { RISK_TOPICS: "Подтверждённые темы обратной связи" }
      : {}),
    ...(["HR_ADMIN", "HR_SPECIALIST"].includes(actor.role)
      ? { SURVEYS: "Ответы анкет", CASES: "Обращения" }
      : {}),
    ...(actor.role === "HR_ADMIN" && actor.scope?.all
      ? { AUDIT: "Аудит действий" }
      : {}),
  };
  const rows = items(adaptation.data).filter(
    (r) => !stage || r.stage === stage,
  );
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>
            <FileBarChart size={20} />
            Отчёты по адаптации
          </h2>
        </div>
        <button
          className="text-button"
          onClick={() => {
            risk.reload();
            adaptation.reload();
            exports.reload();
          }}
        >
          <RefreshCcw size={15} />
          Обновить
        </button>
      </div>
      <ErrorBox
        error={error || risk.error || adaptation.error || exports.error}
      />
      {success && <Success>{success}</Success>}
      <div className="form-grid">
        <TimeZoneField value={timezone} onChange={setTimezone} />
        <Field label="Площадка отчёта">
          <select value={site} onChange={(e) => setSite(e.target.value)}>
            <option value="">Все площадки моей области</option>
            {items(sites.data).map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Начало периода событий">
          <input
            type="date"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
          />
        </Field>
        <Field label="Конец периода событий">
          <input
            type="date"
            value={to}
            min={from || undefined}
            onChange={(e) => setTo(e.target.value)}
          />
        </Field>
        {canPeople && (
          <>
            <Field label="Когорта: вышли с">
              <input
                type="date"
                value={cohortFrom}
                onChange={(e) => setCohortFrom(e.target.value)}
              />
            </Field>
            <Field label="Когорта: вышли по">
              <input
                type="date"
                min={cohortFrom || undefined}
                value={cohortTo}
                onChange={(e) => setCohortTo(e.target.value)}
              />
            </Field>
            <Field label="Этап сотрудника">
              <select value={stage} onChange={(e) => setStage(e.target.value)}>
                <option value="">Все этапы</option>
                <option value="PREBOARDING">До выхода</option>
                <option value="ACTIVE">Работает</option>
                <option value="ENDED">Работа завершена</option>
              </select>
            </Field>
          </>
        )}
      </div>
      {actor.role !== "MANAGER" && (
        <section className="panel inset">
          <div className="panel-head">
            <h3>Темы, подтверждённые HR</h3>
            <small>
              Ожидают оценки: {risk.data?.pending_analysis_count ?? "—"}
            </small>
          </div>
          {risk.loading ? (
            <Loading />
          ) : items(risk.data).some((r) => !r.suppressed) ? (
            <div className="topic-grid">
              {items(risk.data)
                .filter((r) => !r.suppressed)
                .map((r, i) => (
                  <div className="topic" key={i}>
                    <strong>
                      {r.suppressed ? "Малая группа: тема скрыта" : r.topic}
                    </strong>
                    <span>
                      {r.suppressed
                        ? "Число сотрудников скрыто"
                        : `${r.employee_count} сотрудников`}
                    </span>
                  </div>
                ))}
            </div>
          ) : (
            <Empty title="Пока нет подтверждённых тем" />
          )}
          {items(risk.data).some((r) => r.suppressed) && (
            <Notice>Часть тем скрыта из-за малой группы сотрудников.</Notice>
          )}
          <p className="muted">
            В подсчёте — уникальные сотрудники с ручной оценкой HR. Названия тем
            и численность групп меньше {risk.data?.minimum_group_size || 5}{" "}
            скрыты. Отсутствие данных не означает отсутствия затруднений.
          </p>
        </section>
      )}
      {canPeople && (
        <details className="advanced" open={actor.role === "MANAGER"}>
          <summary>Сотрудники выбранной когорты</summary>
          {adaptation.loading ? (
            <Loading />
          ) : rows.length ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Сотрудник</th>
                    <th>Этап</th>
                    <th>Выполнено шагов</th>
                    <th>Открытые обращения</th>
                    <th>Препятствия</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.employee_id}>
                      <td>{r.name}</td>
                      <td>
                        <Status value={r.stage} />
                      </td>
                      <td>
                        {r.completed_mandatory} из {r.total_mandatory}
                      </td>
                      <td>{r.open_case_count}</td>
                      <td>
                        {r.open_blocker_count} открытых ·{" "}
                        {r.confirmed_blocker_count} подтверждённо решены
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title="В выбранной когорте нет доступных сотрудников" />
          )}
          <small className="muted">
            Срез: {date(adaptation.data?.cutoff_at)}. Просмотр ограничен текущей
            областью доступа.
          </small>
        </details>
      )}
      <details className="advanced">
        <summary>Выгрузить отчёт</summary>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            setSuccess("");
            try {
              await api.post("/reports/exports", {
                dataset,
                format,
                filters: {
                  timezone,
                  ...(site ? { site_ids: [site] } : {}),
                  ...(from ? { period_from: from } : {}),
                  ...(to ? { period_to: to } : {}),
                  ...(cohortFrom ? { cohort_from: cohortFrom } : {}),
                  ...(cohortTo ? { cohort_to: cohortTo } : {}),
                  ...(stage ? { statuses: [stage] } : {}),
                },
                reason,
              });
              exports.reload();
              setSuccess(
                "Выгрузка поставлена в очередь. Статус и ссылка будут доступны ниже после перезагрузки.",
              );
            } catch (e) {
              setError(errorText(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          <div className="form-grid">
            <Field label="Набор данных">
              <select
                value={dataset}
                onChange={(e) => setDataset(e.target.value)}
              >
                {Object.entries(allowedDatasets).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Формат файла">
              <select
                value={format}
                onChange={(e) => setFormat(e.target.value)}
              >
                <option value="CSV">CSV</option>
                <option value="XLSX">Excel XLSX</option>
                <option value="PDF">PDF</option>
              </select>
            </Field>
          </div>
          <Field label="Назначение выгрузки">
            <input
              required
              minLength={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <button
            className="button secondary"
            disabled={busy || reason.trim().length < 3}
          >
            <Download size={16} />
            Сформировать файл
          </button>
        </form>
        <Notice>
          Выгрузка учитывает выбранную область и разрешения на момент
          скачивания. Персональные ответы доступны только уполномоченному HR,
          руководитель подбора получает агрегаты.
        </Notice>
      </details>
      {!!items(exports.data).length && (
        <details className="advanced" open={pending}>
          <summary>Мои сформированные выгрузки</summary>
          {items(exports.data).map((r) => (
            <div className="list-row" key={r.id}>
              <div>
                <strong>
                  {allowedDatasets[r.dataset] || r.dataset || "Отчёт"} ·{" "}
                  {r.format || ""}
                </strong>
                <p>{date(r.created_at)}</p>
                <ErrorBox error={r.error?.message || r.error?.code} />
              </div>
              <Status value={r.status} />
              {r.status === "SUCCEEDED" && r.links?.content && (
                <a
                  className="button secondary"
                  href={apiSource(
                    r.links.content,
                    `/api/v1/reports/exports/${r.id}/content`,
                  )}
                >
                  <Download size={15} />
                  Скачать
                </a>
              )}
              {["PENDING", "RUNNING", "SCHEDULED"].includes(r.status) && (
                <button
                  className="text-button"
                  onClick={async () => {
                    try {
                      const detail = await api.get(`/operations/${r.id}`);
                      await api.post(
                        `/operations/${r.id}/cancel`,
                        { reason: "Выгрузка остановлена пользователем" },
                        detail.etag || detail.data.etag,
                      );
                      exports.reload();
                    } catch (e) {
                      setError(errorText(e));
                    }
                  }}
                >
                  Остановить
                </button>
              )}
            </div>
          ))}
        </details>
      )}
    </section>
  );
}
