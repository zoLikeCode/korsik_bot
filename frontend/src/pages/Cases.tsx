import { useState } from "react";
import {
  CheckCheck,
  Clock,
  Inbox,
  Plus,
  ArrowUpRight,
  UserCheck,
  BookOpen,
  AlertTriangle,
} from "lucide-react";
import { useActor } from "../App";
import GapInstruction from "./GapInstruction";
import { CreateSupportItem, CaseExtraActions } from "./CaseActions";
import { api, items, errorText } from "../api";
import {
  PageTitle,
  useResource,
  Loading,
  ErrorBox,
  Success,
  Empty,
  Status,
  Modal,
  Field,
  SearchInput,
  date,
  Notice,
} from "../ui";
export default function Cases() {
  const actor = useActor(),
    manager = actor.role === "MANAGER";
  const [tab, setTab] = useState(manager ? "tasks" : "cases"),
    [selected, setSelected] = useState<any>(null),
    [query, setQuery] = useState(""),
    [createItem, setCreateItem] = useState(false);
  const state = useResource(
    tab === "tasks"
      ? "/manager-tasks"
      : tab === "gaps"
        ? "/knowledge/gaps"
        : "/cases",
  );
  const employees = useResource("/employees");
  const employeeNames = new Map(
    items(employees.data).map((e) => [e.id, e.name]),
  );
  const rows = items(state.data).filter((c) =>
    `${c.question || c.reason || c.title || c.summary || ""} ${c.employee_name || employeeNames.get(c.employee_id) || ""}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  return (
    <>
      <PageTitle
        title={manager ? "Мои задачи" : "Обращения"}
        action={
          !manager && tab !== "gaps" ? (
            <button
              className="button primary"
              onClick={() => setCreateItem(true)}
            >
              <Plus size={16} />
              {tab === "tasks" ? "Новое поручение" : "Новое обращение"}
            </button>
          ) : undefined
        }
      />
      <div className="section-tabs">
        {!manager && (
          <button
            className={tab === "cases" ? "active" : ""}
            onClick={() => setTab("cases")}
          >
            <Inbox size={16} />
            Обращения
          </button>
        )}
        <button
          className={tab === "tasks" ? "active" : ""}
          onClick={() => setTab("tasks")}
        >
          <CheckCheck size={16} />
          Задачи руководителей
        </button>
        {!manager && (
          <button
            className={tab === "gaps" ? "active" : ""}
            onClick={() => setTab("gaps")}
          >
            <BookOpen size={16} />
            Пробелы в знаниях
          </button>
        )}
      </div>
      <div className="toolbar">
        <SearchInput
          value={query}
          onChange={setQuery}
          placeholder="Найти по теме или сотруднику"
        />
        <span className="muted">{rows.length} записей</span>
      </div>
      <ErrorBox error={state.error} />
      <section className="panel flush">
        {state.loading ? (
          <Loading />
        ) : rows.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Тема</th>
                  <th>Сотрудник</th>
                  <th>Срок / создано</th>
                  <th>Статус</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr
                    key={r.id}
                    className="clickable"
                    onClick={() => setSelected(r)}
                  >
                    <td>
                      <div className="case-subject">
                        {r.kind === "BLOCKER" ? (
                          <AlertTriangle size={17} />
                        ) : tab === "tasks" ? (
                          <CheckCheck size={17} />
                        ) : (
                          <Inbox size={17} />
                        )}
                        <strong>
                          {(tab === "gaps" ? r.question : null) ||
                            r.reason ||
                            r.title ||
                            r.summary ||
                            r.question ||
                            r.description?.slice(0, 80) ||
                            "Обращение сотрудника"}
                        </strong>
                      </div>
                    </td>
                    <td>
                      {r.employee_name ||
                        employeeNames.get(r.employee_id) ||
                        "Сотрудник недоступен"}
                    </td>
                    <td>{date(r.due_at || r.created_at)}</td>
                    <td>
                      <Status value={r.status} />
                    </td>
                    <td>
                      <ArrowUpRight size={17} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title={
              tab === "gaps"
                ? "Новых пробелов в знаниях нет"
                : tab === "tasks"
                  ? "Все текущие задачи обработаны"
                  : "Открытых обращений нет"
            }
          >
            Новые записи появятся здесь автоматически после вопросов, анкет и
            сигналов сотрудников.
          </Empty>
        )}
      </section>
      {createItem && (
        <CreateSupportItem
          task={tab === "tasks"}
          onClose={() => setCreateItem(false)}
          onCreated={() => {
            setCreateItem(false);
            state.reload();
          }}
        />
      )}
      {selected &&
        (tab === "gaps" ? (
          <GapInstruction
            gap={selected}
            onClose={() => setSelected(null)}
            onChange={state.reload}
          />
        ) : (
          <CaseDetail
            item={selected}
            task={tab === "tasks"}
            onClose={() => setSelected(null)}
            onChange={state.reload}
          />
        ))}
    </>
  );
}
function CaseDetail({
  item,
  task,
  onClose,
  onChange,
}: {
  item: any;
  task: boolean;
  onClose: () => void;
  onChange: () => void;
}) {
  const path = task ? `/manager-tasks/${item.id}` : `/cases/${item.id}`;
  const detail = useResource(path),
    directory = useResource("/directory"),
    events = useResource(task ? null : `${path}/events`);
  const [summary, setSummary] = useState(""),
    [owner, setOwner] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [success, setSuccess] = useState("");
  const d: any = detail.data || item;
  async function action(action: string, body: any) {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const latest = await api.get(path);
      await api.post(`${path}/${action}`, body, latest.etag);
      detail.reload();
      events.reload();
      onChange();
      setSuccess("Изменения сохранены");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={task ? "Задача руководителя" : "Обращение сотрудника"}
      onClose={onClose}
    >
      <div className="detail-heading">
        <span className="document-icon">
          {task ? <CheckCheck /> : <Inbox />}
        </span>
        <div>
          <h3>{d.reason || d.title || d.summary || "Помощь в адаптации"}</h3>
          <p>
            {d.employee_name ||
              item.employee_name ||
              `Сотрудник ${d.employee_id?.slice(0, 8) || ""}`}
          </p>
        </div>
        <Status value={d.status} />
      </div>
      <p className="case-description">{d.description || d.text || d.summary}</p>
      <div className="detail-facts">
        <span>
          <Clock size={16} />
          {d.due_at
            ? `Срок: ${date(d.due_at)}`
            : `Создано: ${date(d.created_at)}`}
        </span>
        <span>
          {d.assignee_name || d.owner_name || "Ответственный не указан"}
        </span>
      </div>
      <ErrorBox error={error || detail.error} />
      {success && <Success>{success}</Success>}
      {![
        "COMPLETED",
        "RESOLVED",
        "CANCELLED",
        "CLOSED",
        "CONFIRMED",
        "CLOSED_WITHOUT_CONFIRMATION",
        "RESOLVED_PENDING_CONFIRMATION",
      ].includes(d.status) && (
        <>
          <div className="button-row">
            <button
              className="button secondary"
              disabled={
                busy || !["OPEN", "ASSIGNED", "REOPENED"].includes(d.status)
              }
              onClick={() => action(task ? "acknowledge" : "claim", {})}
            >
              <UserCheck size={16} />
              Принять в работу
            </button>
          </div>
          <Field label="Результат / что сделано">
            <textarea
              rows={5}
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              placeholder="Опишите конкретное решение для сотрудника"
            />
          </Field>
          <button
            className="button primary"
            disabled={
              busy ||
              !summary.trim() ||
              (task && !["ACKNOWLEDGED", "IN_PROGRESS"].includes(d.status))
            }
            onClick={() =>
              action(
                task ? "complete" : "resolve",
                task
                  ? {
                      result_code: "DONE",
                      result_summary: summary,
                      expected_assignment_revision: d.assignment_revision ?? 1,
                    }
                  : { result_summary: summary },
              )
            }
          >
            <CheckCheck size={16} />
            {task ? "Завершить задачу" : "Отправить решение"}
          </button>
          {!task && (
            <>
              <Notice>
                Для препятствия адаптации решение отправляется сотруднику.
                Подтверждение результата фиксируется отдельно.
              </Notice>
              <details className="advanced">
                <summary>Назначить другого ответственного</summary>
                <Field label="Ответственный специалист">
                  <select
                    value={owner}
                    onChange={(e) => setOwner(e.target.value)}
                  >
                    <option value="">Выберите ответственного</option>
                    {(directory.data?.users || [])
                      .filter((u: any) =>
                        ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(
                          u.role,
                        ),
                      )
                      .map((u: any) => (
                        <option key={u.id} value={u.id}>
                          {u.name}
                        </option>
                      ))}
                  </select>
                </Field>
                <button
                  className="button secondary"
                  disabled={!owner || busy}
                  onClick={() =>
                    action("assign", {
                      owner_id: owner,
                      reason: "Переназначение обращения по решению HR",
                    })
                  }
                >
                  Назначить
                </button>
              </details>
            </>
          )}
        </>
      )}
      {d.result_summary && (
        <div className="resolution">
          <strong>Зафиксированный результат</strong>
          <p>{d.result_summary}</p>
        </div>
      )}
      {d.status === "RESOLVED_PENDING_CONFIRMATION" && (
        <Notice>
          Решение ожидает подтверждения сотрудника. Оно не считается
          подтверждённо выполненным.
        </Notice>
      )}
      {!task && (
        <CaseExtraActions
          item={d}
          onDone={() => {
            detail.reload();
            events.reload();
            onChange();
          }}
        />
      )}
      {!task && items(events.data).length > 0 && (
        <>
          <h3 className="section-heading">История обработки</h3>
          <div className="timeline">
            {items(events.data).map((e: any, i: number) => (
              <div className="timeline-item" key={e.id || i}>
                <span className="timeline-dot" />
                <div>
                  <strong>
                    {(
                      {
                        REQUESTED: "Обращение создано",
                        CREATED: "Обращение зарегистрировано",
                        INTERNAL_NOTE: "Рабочая заметка HR",
                        CLAIMED: "Принято HR",
                        ASSIGNED: "Назначен ответственный",
                        RESOLVED: "Решение отправлено",
                        RESOLVED_ATTEMPT: "Решение ожидает подтверждения",
                        REOPENED: "Открыто повторно",
                        ADMINISTRATIVE_CLOSE:
                          "Закрыто по административному основанию",
                        NOTE: "Заметка HR",
                      } as Record<string, string>
                    )[e.event_type || e.action] ||
                      e.event_type ||
                      e.action ||
                      "Событие обращения"}
                  </strong>
                  <p>
                    {e.text || e.result_summary || e.description || e.reason}
                  </p>
                  <small>{date(e.created_at)}</small>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </Modal>
  );
}
