import { Download } from "lucide-react";
import { useActor } from "../App";
import { items } from "../api";
import TimeZoneField from "../TimeZoneField";
import AnalyticsOverview from "../components/AnalyticsOverview";
import FrequentProblems from "../components/FrequentProblems";
import { useState } from "react";
import { useResource, Field, PageTitle, Loading, ErrorBox } from "../ui";

export default function Dashboard({
  analytics = false,
}: {
  analytics?: boolean;
}) {
  const actor = useActor();
  const [periodFrom, setPeriodFrom] = useState("");
  const [periodTo, setPeriodTo] = useState("");
  const [timezone, setTimezone] = useState("Europe/Moscow");
  const [siteId, setSiteId] = useState("");
  const sites = useResource(actor.role === "EMPLOYEE" ? null : "/sites");
  const reportingParams = new URLSearchParams({ timezone });
  if (periodFrom) reportingParams.set("period_from", periodFrom);
  if (periodTo) reportingParams.set("period_to", periodTo);
  if (siteId) reportingParams.set("site_id", siteId);
  const state = useResource(
    actor.role === "EMPLOYEE" && actor.employee_id
      ? `/employees/${actor.employee_id}`
      : analytics
        ? `/analytics?${reportingParams}`
        : `/dashboards/current?${reportingParams}`,
  );
  const problems = useResource(
    actor.role === "HEAD_TALENT"
      ? `/reports/risk-topics?${reportingParams}`
      : null,
  );
  const data: any = state.data || {};
  return (
    <>
      <PageTitle
        title={analytics ? "Аналитика" : "Обзор"}
        action={
          <div className="button-row">
            {analytics && (
              <a
                className="button secondary"
                href={`/api/v1/analytics/export?${reportingParams}`}
              >
                <Download size={16} />
                Скачать CSV
              </a>
            )}
            <button
              className="button secondary"
              onClick={() => {
                state.reload();
                if (actor.role === "HEAD_TALENT") problems.reload();
              }}
            >
              Обновить данные
            </button>
          </div>
        }
      />
      <section className="panel">
        <div className="form-grid">
          <Field label="События с">
            <input
              type="date"
              value={periodFrom}
              onChange={(e) => setPeriodFrom(e.target.value)}
            />
          </Field>
          <Field label="События по (включительно)">
            <input
              type="date"
              value={periodTo}
              min={periodFrom || undefined}
              onChange={(e) => setPeriodTo(e.target.value)}
            />
          </Field>
          <TimeZoneField value={timezone} onChange={setTimezone} />
          <Field label="Площадка среза">
            <select value={siteId} onChange={(e) => setSiteId(e.target.value)}>
              <option value="">Вся моя область</option>
              {items(sites.data).map((site) => (
                <option value={site.id} key={site.id}>
                  {site.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <p className="muted">
          Период применяется к событиям анкет и подтверждённой обратной связи;
          текущая нагрузка отображается на момент среза. Даты трактуются в
          выбранном часовом поясе.
        </p>
      </section>
      <ErrorBox error={state.error} />
      {state.loading ? (
        <Loading />
      ) : state.error ? null : (
        <>
          <AnalyticsOverview data={data} role={actor.role} />
          {actor.role === "HEAD_TALENT" && (
            <FrequentProblems
              data={problems.data}
              loading={problems.loading}
              error={problems.error}
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
