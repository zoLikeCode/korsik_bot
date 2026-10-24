import { Link } from "react-router-dom";
import {
  observedCount,
  observedRatio,
  dashboardRole,
} from "../analyticsOverview";
import "./AnalyticsOverview.css";

function Count({ value }: { value: unknown }) {
  const count = observedCount(value);
  return (
    <strong className="overview-count">
      {count === null ? "—" : count.toLocaleString("ru-RU")}
    </strong>
  );
}
export function SurveyRate({
  title,
  value,
  caption,
}: {
  title: string;
  value: any;
  caption: string;
}) {
  const ratio = observedRatio(value);
  return (
    <div className="overview-rate">
      <div>
        <strong>{title}</strong>
        <span>{ratio ? `${ratio.percent}%` : "Нет данных"}</span>
      </div>
      {ratio ? (
        <>
          <div
            className="overview-track"
            role="progressbar"
            aria-label={title}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={ratio.percent}
          >
            <span style={{ width: `${ratio.percent}%` }} />
          </div>
          <p>
            {ratio.numerator} из {ratio.denominator}. {caption}
          </p>
        </>
      ) : (
        <p>Пока нет доставленных анкет для расчёта.</p>
      )}
    </div>
  );
}
export default function AnalyticsOverview({
  data,
  role,
}: {
  data: any;
  role: string;
}) {
  if (!["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT", "MANAGER"].includes(role))
    return null;
  const stats = data.metrics || data.summary || {},
    view = dashboardRole(role),
    hr = ["HR_ADMIN", "HR_SPECIALIST"].includes(role);
  const total = observedCount(stats.employees),
    before = observedCount(stats.preboarding),
    after = observedCount(stats.active_employees);
  const sites = Array.isArray(data.sites) ? data.sites : [];
  const siteMax = Math.max(
    1,
    ...sites.map((s: any) => observedCount(s.employees) || 0),
  );
  const actions = [
    {
      label: "Открытые обращения",
      value: stats.open_cases,
      description: "Обращения, по которым работа ещё не завершена.",
      to: hr ? "/chat" : null,
      action: "Открыть диалоги",
    },
    {
      label: "Трудности в адаптации",
      value: stats.open_blockers,
      description: "Препятствия без подтверждённого решения.",
      to: null,
      action: "",
    },
    {
      label: "Просроченные задачи",
      value: stats.tasks_overdue,
      description:
        role === "MANAGER"
          ? "Ваши назначенные задачи с истёкшим сроком."
          : "Незавершённые задачи с истёкшим сроком.",
      to: null,
      action: "",
    },
  ];
  return (
    <div className="analytics-overview">
      <section className="panel overview-intro">
        <div>
          <h2>{view.title}</h2>
          <p>{view.caption}</p>
        </div>
        <div className="overview-total">
          <Count value={stats.employees} />
          <span>сотрудников в выбранной области</span>
        </div>
      </section>
      <section aria-label="Текущая нагрузка" className="overview-actions">
        {actions.map((card) => (
          <article
            className="panel overview-action"
            key={card.label}
            data-attention={
              (observedCount(card.value) || 0) > 0 ? "true" : undefined
            }
          >
            <span className="overview-state">
              {observedCount(card.value) === null
                ? "Нет данных"
                : card.value > 0
                  ? "Нужно внимание"
                  : "Нет открытых вопросов"}
            </span>
            <h3>{card.label}</h3>
            <Count value={card.value} />
            <p>{card.description}</p>
            {card.to && (
              <Link className="text-button" to={card.to}>
                {card.action}
              </Link>
            )}
          </article>
        ))}
      </section>
      <div className="overview-panels">
        <section className="panel">
          <h2>Этапы сотрудников</h2>
          <p className="overview-caption">
            Текущее состояние, независимо от периода анкет.
          </p>
          {total === null ? (
            <p>Данные о сотрудниках пока недоступны.</p>
          ) : total === 0 ? (
            <p>В выбранной области пока нет сотрудников.</p>
          ) : (
            <div className="overview-stages">
              {[
                ["До выхода", before],
                ["После выхода", after],
              ].map(([label, count]) => (
                <div key={String(label)}>
                  <span>{label}</span>
                  <strong>{count === null ? "—" : count}</strong>
                  {typeof count === "number" && total > 0 && (
                    <div className="overview-track" aria-hidden="true">
                      <span
                        style={{
                          width: `${Math.min(100, (count * 100) / total)}%`,
                        }}
                      />
                    </div>
                  )}
                </div>
              ))}
              <p className="overview-caption">
                Этап не означает завершение программы адаптации. В общем
                количестве могут быть сотрудники других этапов.
              </p>
            </div>
          )}
        </section>
        <section className="panel">
          <h2>Участие в анкетах</h2>
          <p className="overview-caption">
            Анкеты адаптации с доставленным первым вопросом за выбранный период.
            Отдельные рассылки показаны во вкладке «Опросы».
          </p>
          <SurveyRate
            title="Завершены"
            value={stats.completion}
            caption="Сотрудник прошёл анкету до конца."
          />
          <SurveyRate
            title="Есть хотя бы один ответ"
            value={stats.participation}
            caption="Учитываются анкеты с сохранённым ответом."
          />
          <div className="overview-survey-status">
            <span>
              Отказались: {observedCount(stats.surveys_declined) ?? "—"}
            </span>
            <span>
              Пока без ответа: {observedCount(stats.surveys_missing) ?? "—"}
            </span>
          </div>
        </section>
      </div>
      {role === "HEAD_TALENT" && (
        <section className="panel">
          <h2>Сотрудники по площадкам</h2>
          <p className="overview-caption">
            Численность в доступной области, а не рейтинг площадок.
          </p>
          {sites.length ? (
            <div className="overview-sites">
              {sites.map((site: any) => (
                <div key={site.site_id}>
                  <strong>{site.name || "Площадка"}</strong>
                  <div className="overview-track" aria-hidden="true">
                    <span
                      style={{
                        width: `${((observedCount(site.employees) || 0) * 100) / siteMax}%`,
                      }}
                    />
                  </div>
                  <span>{observedCount(site.employees) ?? "—"}</span>
                  {site.sensitive_metrics_suppressed && (
                    <small>Оценки малой группы скрыты</small>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p>В выбранной области нет данных по площадкам.</p>
          )}
        </section>
      )}
    </div>
  );
}
