import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Users,
  ClipboardCheck,
  MessagesSquare,
  Sparkles,
  ArrowUpRight,
  CircleHelp,
} from "lucide-react";
import { dashboardRole, observedCount } from "../analyticsOverview";
import "./AnalyticsDashboard.css";

type Row = Record<string, any>;
const COLORS = ["#497b66", "#77a9c5", "#d6ab62", "#a2a6af", "#bdce9e"];
const number = (v: unknown) => observedCount(v)?.toLocaleString("ru-RU") ?? "—";
function percent(n: unknown, d: unknown) {
  return typeof n === "number" && typeof d === "number" && d > 0
    ? Math.round((n * 100) / d)
    : null;
}
function Empty({
  children = "Данные появятся, когда сотрудники начнут пользоваться Корсиком.",
}: {
  children?: React.ReactNode;
}) {
  return (
    <div className="insight-empty">
      <CircleHelp size={25} />
      <p>{children}</p>
    </div>
  );
}
function Panel({
  title,
  caption,
  children,
  wide = false,
}: {
  title: string;
  caption: string;
  children: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <section className={`panel insight-panel${wide ? " insight-wide" : ""}`}>
      <header>
        <h2>{title}</h2>
        <p>{caption}</p>
      </header>
      {children}
    </section>
  );
}
export function Donut({
  rows,
  label,
}: {
  rows: { label: string; value: number; color?: string }[];
  label: string;
}) {
  const safe = rows.filter((r) => observedCount(r.value) !== null),
    total = safe.reduce((n, r) => n + r.value, 0);
  let offset = 0;
  if (!total) return <Empty>Пока нет данных для диаграммы.</Empty>;
  return (
    <div className="insight-donut">
      <svg viewBox="0 0 180 180" role="img" aria-label={`${label}: ${total}`}>
        <circle
          cx="90"
          cy="90"
          r="65"
          fill="none"
          stroke="var(--line)"
          strokeWidth="23"
        />
        {safe.map((r, i) => {
          const start = offset;
          offset += (r.value / total) * 100;
          return (
            <circle
              key={r.label}
              cx="90"
              cy="90"
              r="65"
              fill="none"
              stroke={r.color || COLORS[i % COLORS.length]}
              strokeWidth="23"
              pathLength="100"
              strokeDasharray={`${(r.value / total) * 100} ${100 - (r.value / total) * 100}`}
              strokeDashoffset={-start}
              transform="rotate(-90 90 90)"
            >
              <title>{`${r.label}: ${r.value}`}</title>
            </circle>
          );
        })}
        <text x="90" y="90" textAnchor="middle" className="donut-total">
          {total}
        </text>
        <text x="90" y="112" textAnchor="middle" className="donut-caption">
          {label}
        </text>
      </svg>
      <ul className="insight-legend">
        {safe.map((r, i) => (
          <li key={r.label}>
            <i style={{ background: r.color || COLORS[i % COLORS.length] }} />
            <span>{r.label}</span>
            <strong>{number(r.value)}</strong>
          </li>
        ))}
      </ul>
    </div>
  );
}
export function Trend({ data }: { data: Row[] }) {
  const max = Math.max(1, ...data.map((p) => p.questions || 0));
  if (!data.some((p) => p.questions > 0))
    return (
      <Empty>За этот период сотрудники ещё не задавали вопросов Корсику.</Empty>
    );
  const width = 760,
    height = 210,
    left = 36,
    top = 15,
    plotHeight = 155;
  const x = (i: number) =>
    left + (i * (width - left - 15)) / Math.max(1, data.length - 1);
  const y = (v: number) => top + plotHeight * (1 - v / max);
  return (
    <>
      <svg
        className="insight-trend"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label="Динамика вопросов, ответов Корсика и передач HR"
      >
        {[0, 0.5, 1].map((t) => (
          <g key={t}>
            <line
              x1={left}
              x2={width - 10}
              y1={y(max * t)}
              y2={y(max * t)}
              stroke="var(--line)"
              strokeDasharray="4 5"
            />
            <text x="25" y={y(max * t) + 4} textAnchor="end">
              {Math.round(max * t)}
            </text>
          </g>
        ))}
        {(["questions", "automatic", "handoff"] as const).map((key, n) => (
          <g key={key}>
            <polyline
              fill="none"
              stroke={COLORS[n]}
              strokeWidth="3"
              strokeLinejoin="round"
              points={data.map((p, i) => `${x(i)},${y(p[key] || 0)}`).join(" ")}
            />
            {data.map((p, i) => (
              <circle
                key={p.date}
                cx={x(i)}
                cy={y(p[key] || 0)}
                r="4"
                fill={COLORS[n]}
              >
                <title>{`${p.date}: ${key === "questions" ? "Вопросы" : key === "automatic" ? "Ответы Корсика" : "Передано HR"} — ${p[key]}`}</title>
              </circle>
            ))}
          </g>
        ))}
        {data
          .filter(
            (_, i) =>
              i === 0 ||
              i === data.length - 1 ||
              i === Math.floor(data.length / 2),
          )
          .map((p) => (
            <text
              key={p.date}
              x={x(data.indexOf(p))}
              y="199"
              textAnchor="middle"
            >
              {p.date.slice(8, 10)}.{p.date.slice(5, 7)}
            </text>
          ))}
      </svg>
      <div className="chart-key">
        {["Вопросы", "Ответы Корсика", "Передано HR"].map((s, i) => (
          <span key={s}>
            <i style={{ background: COLORS[i] }} />
            {s}
          </span>
        ))}
      </div>
    </>
  );
}
function SurveyBars({ rows }: { rows: Row[] }) {
  const [all, setAll] = useState(false);
  if (!rows.length)
    return (
      <Empty>
        За выбранный период нет анкет с доставленным первым вопросом.
      </Empty>
    );
  return (
    <>
      <div className="survey-chart">
        {rows.slice(0, all ? undefined : 6).map((row) => (
          <article key={row.key}>
            <div className="chart-row-heading">
              <strong>{row.title}</strong>
              <span>{row.custom ? "HR-опрос" : "Адаптация"}</span>
            </div>
            {row.suppressed ? (
              <p className="insight-private">
                Малая группа: статистика ответов скрыта.
              </p>
            ) : (
              <>
                <div
                  className="stacked-bar"
                  role="img"
                  aria-label={`${row.title}: завершено ${row.completed} из ${row.delivered}`}
                >
                  {(
                    ["completed", "in_progress", "declined", "expired"] as const
                  ).map((key, i) => (
                    <span
                      key={key}
                      title={`${["Завершены", "В процессе", "Отказ", "Срок истёк"][i]}: ${row[key]}`}
                      style={{
                        width: `${percent(row[key], row.delivered) ?? 0}%`,
                        background: COLORS[i],
                      }}
                    />
                  ))}
                </div>
                <div className="survey-bar-caption">
                  <span>
                    Завершено {number(row.completed)} из {number(row.delivered)}
                  </span>
                  <strong>{percent(row.completed, row.delivered) ?? 0}%</strong>
                </div>
              </>
            )}
          </article>
        ))}
      </div>
      <div className="chart-key">
        {["Завершены", "В процессе", "Отказ", "Срок истёк"].map((s, i) => (
          <span key={s}>
            <i style={{ background: COLORS[i] }} />
            {s}
          </span>
        ))}
      </div>
      {rows.length > 6 && (
        <button className="text-button" onClick={() => setAll(!all)}>
          {all ? "Свернуть" : `Все опросы (${rows.length})`}
        </button>
      )}
    </>
  );
}
export function Results({ rows }: { rows: Row[] }) {
  const [all, setAll] = useState(false);
  if (!rows.length)
    return (
      <Empty>
        Результаты появятся после ответов на вопросы со шкалой или вариантами
        выбора.
      </Empty>
    );
  return (
    <>
      <div className="insight-results">
        {rows.slice(0, all ? undefined : 6).map((row) => (
          <article key={row.key}>
            <span className="result-survey">{row.survey}</span>
            <h3>{row.question}</h3>
            {row.suppressed ||
            observedCount(row.respondents) === null ||
            row.respondents < 5 ? (
              <p className="insight-private">
                Для показа результата нужны ответы минимум пяти сотрудников.
              </p>
            ) : row.type === "NUMERIC" && typeof row.average === "number" ? (
              <>
                <div className="result-score">
                  <strong>
                    {row.average.toLocaleString("ru-RU", {
                      maximumFractionDigits: 2,
                    })}
                  </strong>
                  <span>
                    из {row.maximum} · {row.respondents} участников
                  </span>
                </div>
                <div className="result-track">
                  <span
                    style={{
                      width: `${Math.max(0, Math.min(100, ((row.average - row.minimum) / Math.max(1, row.maximum - row.minimum)) * 100))}%`,
                    }}
                  />
                </div>
                <div className="result-scale">
                  <span>{row.minimum}</span>
                  <span>{row.maximum}</span>
                </div>
              </>
            ) : Array.isArray(row.distribution) ? (
              <div className="result-choices">
                {row.distribution.map((o: Row) => (
                  <div key={o.label}>
                    <span>{o.label}</span>
                    <div className="result-track">
                      <span
                        style={{
                          width: `${percent(o.count, row.respondents) ?? 0}%`,
                        }}
                      />
                    </div>
                    <strong>{o.count}</strong>
                  </div>
                ))}
              </div>
            ) : (
              <p className="insight-private">
                Распределение скрыто: в отдельных вариантах меньше пяти ответов.
              </p>
            )}
          </article>
        ))}
      </div>
      {rows.length > 6 && (
        <button className="text-button" onClick={() => setAll(!all)}>
          {all ? "Свернуть" : `Все вопросы (${rows.length})`}
        </button>
      )}
    </>
  );
}
export default function AnalyticsDashboard({
  data,
  role,
  onDepartment,
}: {
  data: any;
  role: string;
  onDepartment: (id: string) => void;
}) {
  const [tab, setTab] = useState("overview");
  const s = data?.summary || {},
    view = dashboardRole(role);
  const [active, completed, open] = [
    observedCount(s.adapting),
    observedCount(s.completed),
    observedCount(s.open_questions),
  ];
  const completion =
    typeof s.completion_rate === "number"
      ? Math.round(s.completion_rate * 100)
      : null;
  const resolved =
    typeof s.automatic === "number" &&
    typeof s.handoff === "number" &&
    typeof s.clarify === "number"
      ? s.automatic + s.handoff + s.clarify
      : null;
  const auto = percent(s.automatic, resolved);
  const adaptation = data?.adaptation || {},
    rows = data?.surveys || [],
    results = data?.survey_results || [];
  const stages = [
    ["in_progress", "В процессе"],
    ["completed", "Завершили"],
    ["scheduled", "Ожидают запуска"],
    ["paused", "На паузе"],
    ["not_assigned", "Без программы"],
  ];
  const attention =
    open !== null && open > 0
      ? `Ждут ответа HR: ${number(open)}. Начните с диалогов без решения.`
      : s.employees > 0
        ? "Нерешённых вопросов в диалогах нет. Следите за прохождением опросов и адаптации."
        : "Начните с сотрудников и опросов — дашборды будут обновляться по мере появления данных.";
  return (
    <div className="hr-insights">
      <section className="insight-heading">
        <div>
          <span className="insight-eyebrow">{view.title}</span>
          <h2>Адаптация и обратная связь</h2>
          <p>{view.caption}</p>
        </div>
        <div className="insight-cohort">
          <strong>{number(s.employees)}</strong>
          <span>
            активных сотрудников
            <br />в выбранной области
          </span>
        </div>
      </section>
      <nav className="insight-tabs" aria-label="Дашборды аналитики">
        {[
          ["overview", "Общая картина"],
          ["surveys", "Опросы и обратная связь"],
          ["support", "Поддержка и подразделения"],
        ].map(([id, title]) => (
          <button key={id} aria-pressed={tab === id} onClick={() => setTab(id)}>
            {title}
          </button>
        ))}
      </nav>
      <div className="insight-kpis">
        {[
          {
            title: "На адаптации",
            value: active === null ? "—" : number(active),
            caption:
              completed === null
                ? "Текущее состояние программ"
                : `Завершили программу: ${number(completed)}`,
            icon: Users,
          },
          {
            title: "Завершение опросов",
            value: completion === null ? "—" : `${completion}%`,
            caption:
              s.surveys_delivered === null
                ? "Малая группа: результат скрыт"
                : `${number(s.surveys_completed)} из ${number(s.surveys_delivered)} доставленных анкет`,
            icon: ClipboardCheck,
          },
          {
            title: "Нужен ответ HR",
            value: number(s.open_questions),
            caption: `Диалогов без решения: ${number(s.problem_conversations)}`,
            icon: MessagesSquare,
          },
          {
            title: "Ответил Корсик",
            value: auto === null ? "—" : `${auto}%`,
            caption: `${number(s.automatic)} ответов по документам за период`,
            icon: Sparkles,
          },
        ].map((c) => (
          <article className="panel insight-kpi" key={c.title}>
            <div>
              <span>{c.title}</span>
              <c.icon size={19} />
            </div>
            <strong>{c.value}</strong>
            <p>{c.caption}</p>
          </article>
        ))}
      </div>
      <div className="insight-notice">
        <span className="insight-notice-dot" />
        <p>{attention}</p>
        {role !== "MANAGER" && open !== null && open > 0 && (
          <Link to="/chat">
            Диалоги <ArrowUpRight size={16} />
          </Link>
        )}
      </div>
      <div className="insight-grid">
        {tab === "overview" && (
          <>
            <Panel
              title="Как идёт адаптация"
              caption="По одной категории на сотрудника. Состояние программ на текущий момент."
            >
              <Donut
                label="сотрудников"
                rows={stages.map(([key, label]) => ({
                  label,
                  value: adaptation[key] || 0,
                }))}
              />
            </Panel>
            <Panel
              title="Где нужна помощь"
              caption="Проблемы, по которым ещё нет подтверждённого решения."
            >
              <div className="attention-list">
                {[
                  ["Вопросы без ответа HR", s.open_questions],
                  ["Трудности в адаптации", s.open_blockers],
                  ["Открытые обращения", s.open_cases],
                  ["Вопросы в обработке", s.pending],
                ].map(([label, count]) => (
                  <div key={label}>
                    <span>{label}</span>
                    <strong
                      data-attention={typeof count === "number" && count > 0}
                    >
                      {number(count)}
                    </strong>
                  </div>
                ))}
              </div>
              <p className="insight-footnote">
                Передача вопроса HR не считается решением. Счётчик уменьшается
                после ответа на конкретный вопрос.
              </p>
            </Panel>
          </>
        )}
        {tab !== "support" && (
          <Panel
            title="Как проходят опросы"
            caption="Анкеты адаптации и отдельные HR-опросы с доставленным первым вопросом за выбранный период."
            wide={tab === "surveys"}
          >
            <SurveyBars rows={rows} />
          </Panel>
        )}
        {tab !== "support" && (
          <Panel
            title="Что говорят сотрудники"
            caption="Средние значения в исходной шкале вопроса. Отдельные редакции не смешиваются; личные тексты не показываются."
            wide={tab === "surveys"}
          >
            <Results rows={results} />
          </Panel>
        )}
        {tab !== "surveys" && (
          <>
            <Panel
              title="Динамика вопросов"
              caption={`Вопросы за выбранный период${data?.trend_bucket_days > 1 ? ` · интервалы по ${data.trend_bucket_days} дн.` : ""}. Наведите на точку, чтобы увидеть значение.`}
              wide
            >
              <Trend data={data?.trend || []} />
            </Panel>
            <Panel
              title="Как решаются вопросы"
              caption="Результат обработки за период. Запрос уточнения не считается ответом по документам."
            >
              <Donut
                label="вопросов"
                rows={[
                  ["Ответил Корсик", s.automatic],
                  ["Передано HR", s.handoff],
                  ["Нужно уточнение", s.clarify],
                  ["В обработке", s.pending],
                ].map(([label, value]) => ({
                  label,
                  value: observedCount(value) ?? 0,
                }))}
              />
            </Panel>
            <Panel
              title="Подразделения: кому нужна поддержка"
              caption="Только доступные вам сотрудники. Нажмите на подразделение, чтобы отфильтровать дашборды."
            >
              {!data?.departments?.length ? (
                <Empty>В выбранной области пока нет сотрудников.</Empty>
              ) : (
                <div className="department-chart">
                  {data.departments.map((r: Row) => (
                    <button
                      disabled={!r.id}
                      key={r.id || "none"}
                      onClick={() => onDepartment(r.id)}
                    >
                      <div>
                        <strong>{r.name}</strong>
                        <span>
                          Сотрудников: {r.employees} · на адаптации:{" "}
                          {r.adapting}
                        </span>
                      </div>
                      <b data-attention={r.open_questions > 0}>
                        {r.open_questions > 0
                          ? `Вопросов: ${r.open_questions}`
                          : "Без открытых вопросов"}
                      </b>
                    </button>
                  ))}
                </div>
              )}
            </Panel>
          </>
        )}
      </div>
      <p className="insight-footnote">
        Результаты опросов скрываются для групп меньше пяти участников. Текущая
        нагрузка считается на сейчас; результаты опросов и динамика — за
        выбранный период.
      </p>
    </div>
  );
}
