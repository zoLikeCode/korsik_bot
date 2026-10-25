import { Download } from "lucide-react";
import { useActor } from "../App";
import { items } from "../api";
import TimeZoneField from "../TimeZoneField";
import AnalyticsOverview from "../components/AnalyticsOverview";
import AnalyticsDashboard from "../components/AnalyticsDashboard";
import FrequentProblems from "../components/FrequentProblems";
import { analyticsDateRange } from "../analyticsDates";
import { useState } from "react";
import { useResource, Field, PageTitle, Loading, ErrorBox } from "../ui";

export default function Dashboard({
  analytics = false,
}: {
  analytics?: boolean;
}) {
  const actor = useActor();
  const [periodFrom, setPeriodFrom] = useState(() =>
    analytics ? analyticsDateRange(30).from : "",
  );
  const [periodTo, setPeriodTo] = useState("");
  const [timezone, setTimezone] = useState("Europe/Moscow");
  const [siteId, setSiteId] = useState("");
  const [department, setDepartment] = useState("");
  const sites = useResource(actor.role === "EMPLOYEE" ? null : "/sites");
  const reportingParams = new URLSearchParams({ timezone });
  if (periodFrom) reportingParams.set("period_from", periodFrom);
  if (periodTo) reportingParams.set("period_to", periodTo);
  if (siteId) reportingParams.set("site_id", siteId);
  if (department) reportingParams.set("org_unit_id", department);
  const state = useResource(
    actor.role === "EMPLOYEE" && actor.employee_id
      ? `/employees/${actor.employee_id}`
      : analytics
        ? `/analytics/insights?${reportingParams}`
        : `/dashboards/current?${reportingParams}`,
  );
  const problems = useResource(
    actor.role === "HEAD_TALENT" && !analytics
      ? `/reports/risk-topics?${reportingParams}`
      : null,
  );
  const data: any = state.data || {};
  function preset(days: number) {
    const range = analyticsDateRange(days, new Date(), timezone);
    setPeriodFrom(range.from);
    setPeriodTo(range.to);
  }
  return (
    <>
      <PageTitle
        title={analytics ? "Аналитика" : "Обзор"}
        action={
          <div className="button-row">
            {analytics && (
              <a
                className="button secondary"
                href={`/api/v1/analytics/insights/export?${reportingParams}`}
              >
                <Download size={16} />
                Скачать CSV
              </a>
            )}
            <button
              className="button secondary"
              onClick={() => {
                state.reload();
                if (actor.role === "HEAD_TALENT" && !analytics)
                  problems.reload();
              }}
            >
              Обновить данные
            </button>
          </div>
        }
      />
      <section className="panel">
        {analytics && (
          <div className="analytics-presets">
            {[7, 30, 90].map((days) => (
              <button
                key={days}
                aria-pressed={
                  periodFrom ===
                    analyticsDateRange(days, new Date(), timezone).from &&
                  (!periodTo ||
                    periodTo ===
                      analyticsDateRange(days, new Date(), timezone).to)
                }
                onClick={() => preset(days)}
              >
                {days} дней
              </button>
            ))}
            <button
              onClick={() => {
                preset(30);
                setSiteId("");
                setDepartment("");
              }}
            >
              Сбросить фильтры
            </button>
          </div>
        )}
        <div className="form-grid">
          <Field label="Период с">
            <input
              type="date"
              value={periodFrom}
              onChange={(e) => setPeriodFrom(e.target.value)}
            />
          </Field>
          <Field label="По (включительно)">
            <input
              type="date"
              value={periodTo}
              min={periodFrom || undefined}
              onChange={(e) => setPeriodTo(e.target.value)}
            />
          </Field>
          <TimeZoneField value={timezone} onChange={setTimezone} />
          <Field label="Площадка">
            <select
              value={siteId}
              onChange={(e) => {
                setSiteId(e.target.value);
                setDepartment("");
              }}
            >
              <option value="">Вся моя область</option>
              {items(sites.data).map((site) => (
                <option value={site.id} key={site.id}>
                  {site.name}
                </option>
              ))}
            </select>
          </Field>
          {analytics && (
            <Field label="Подразделение">
              <select
                value={department}
                onChange={(e) => setDepartment(e.target.value)}
              >
                <option value="">Все доступные</option>
                {!state.loading &&
                  !state.error &&
                  (data.filters?.departments || []).map((unit: any) => (
                    <option key={unit.id} value={unit.id}>
                      {unit.name}
                    </option>
                  ))}
              </select>
            </Field>
          )}
        </div>
        <p className="muted">
          Опросы и динамика — за выбранный период. Этапы адаптации и вопросы без
          решения — на текущий момент.
        </p>
      </section>
      <ErrorBox error={state.error} />
      {state.loading ? (
        <Loading />
      ) : state.error ? null : (
        <>
          {analytics ? (
            <AnalyticsDashboard
              data={data}
              role={actor.role}
              onDepartment={setDepartment}
            />
          ) : (
            <AnalyticsOverview data={data} role={actor.role} />
          )}
          {actor.role === "HEAD_TALENT" && (
            <FrequentProblems
              data={analytics ? data.problems : problems.data}
              loading={!analytics && problems.loading}
              error={!analytics ? problems.error : undefined}
            />
          )}
          <div className="data-caption">
            {data.generated_at || data.reporting_window?.as_of
              ? `Данные на ${new Date(data.generated_at || data.reporting_window.as_of).toLocaleString("ru-RU", { timeZone: timezone })} · ${timezone}`
              : "Отображаются доступные вашей роли данные."}
          </div>
        </>
      )}
    </>
  );
}
