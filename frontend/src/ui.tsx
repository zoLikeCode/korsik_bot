import {
  useEffect,
  useState,
  useId,
  cloneElement,
  isValidElement,
  type ReactNode,
  type ReactElement,
} from "react";
import {
  AlertCircle,
  CheckCircle2,
  LoaderCircle,
  X,
  Search,
  ArrowUpRight,
} from "lucide-react";
import { api, errorText } from "./api";
export function useResource<T = any>(
  path: string | null,
  deps: unknown[] = [],
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [etag, setEtag] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    if (!path) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    api
      .get<T>(path)
      .then((r) => {
        if (active) {
          setData(r.data);
          setEtag(r.etag);
        }
      })
      .catch((e) => active && setError(errorText(e)))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [path, revision, ...deps]);
  return {
    data,
    setData,
    error,
    loading,
    etag,
    reload: () => setRevision((v) => v + 1),
  };
}
export function PageTitle({
  title,
  action,
}: {
  title: string;
  action?: ReactNode;
}) {
  useEffect(() => {
    document.title = title;
  }, [title]);
  return (
    <header className="page-title">
      <div>
        <h1>{title}</h1>
      </div>
      {action}
    </header>
  );
}
export function ErrorBox({ error }: { error: unknown }) {
  return error ? (
    <div className="alert error" role="alert">
      <AlertCircle size={18} />
      <span>{String(error)}</span>
    </div>
  ) : null;
}
export function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="alert info">
      <AlertCircle size={18} />
      <span>{children}</span>
    </div>
  );
}
export function Success({ children }: { children: ReactNode }) {
  return (
    <div className="alert success" role="status">
      <CheckCircle2 size={18} />
      <span>{children}</span>
    </div>
  );
}
export function Loading() {
  return (
    <div className="loading" role="status">
      <LoaderCircle className="spin" size={22} /> Загружаем данные…
    </div>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-mark">К</div>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}
export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
export const blockLabels: Record<string, string> = {
  TEXT: "Текст",
  TABLE: "Таблица",
  FIGURE: "Изображение",
  HEADING: "Заголовок",
  LIST: "Список",
};
export const statusLabels: Record<string, string> = {
  ACTIVE: "Активен",
  PREBOARDING: "До выхода",
  PENDING_VERIFICATION: "Ожидает подтверждения",
  DRAFT: "Черновик",
  APPROVED: "Согласовано",
  PUBLISHED: "Опубликовано",
  ARCHIVED: "Архив",
  PARSING: "Разбор документа",
  PROCESSING: "Обрабатывается",
  INDEXING: "Индексация",
  EXTRACTED: "Проверка",
  READY: "Готово",
  INDEXED: "Проиндексировано",
  FAILED: "Ошибка",
  OPEN: "Открыто",
  ACKNOWLEDGED: "Принято",
  IN_PROGRESS: "В работе",
  RESOLVED: "Решено",
  COMPLETED: "Завершено",
  CANCELLED: "Отменено",
  PAUSED: "Пауза",
  HUMAN: "Диалог с HR",
  BOT: "Ассистент",
  PENDING: "В очереди",
  NEW: "Новое",
  REVIEW_REQUIRED: "Нужна проверка",
  UPLOADED: "Загружено",
  UPLOADING: "Загрузка файла",
  REVIEWED: "Проверено",
  SCHEDULED: "Запланировано",
  PLANNED: "Запланировано",
  NOT_APPLICABLE: "Не применяется",
  WAITING_HR: "Ожидает HR",
  AWAITING_ANSWER: "Ожидает ответа",
  WAITING_ANSWER: "Ожидает ответа",
  PASSED: "Проверки пройдены",
  PASS: "Соответствует",
  FAIL: "Нужна доработка",
  EXPIRED: "Срок истёк",
  PURGE_PENDING: "Удаление данных",
  PURGED: "Данные удалены",
  ISSUED: "Приглашение создано",
  DISABLED: "Доступ выключен",
  NOT_SUBMITTED: "Ещё не отправлен",
  ACCEPTED: "Принято MAX",
  SUPPRESSED: "Доставка остановлена",
  REJECTED: "Отклонено",
  REVOKED: "Отозвано",
  PENDING_WEB_CONFIRM: "Подтвердите в кабинете",
  ENDED: "Работа завершена",
  WAITING_REVIEW: "Ожидает оценки HR",
  NOT_CONFIGURED: "Не настроено",
  QUEUED: "В очереди",
  RUNNING: "Выполняется",
  SUCCEEDED: "Выполнено",
  VERIFIED: "Подтверждено",
  UNCONFIGURED: "Не настроено",
  NEEDS_REVIEW: "Нужно решение HR",
  WITHDRAW: "Будет снято с выдачи",
  PENDING_HR_REVIEW: "Ожидает оценки HR",
  REOPENED: "Открыто повторно",
  ASSIGNED: "Назначено",
  WAITING_EMPLOYEE_CONFIRMATION: "Ожидает сотрудника",
  CONFIRMED: "Решение подтверждено",
  CLOSED_WITHOUT_CONFIRMATION: "Закрыто по основанию HR",
  RESOLVED_PENDING_CONFIRMATION: "Ожидает подтверждения сотрудника",
  WAITING_EVENT: "После выполнения условия",
  NOT_SELECTED_OR_WAITING_ANSWER: "Зависит от ответа сотрудника",
};
export function Status({ value }: { value: string }) {
  return (
    <Badge
      tone={
        [
          "PUBLISHED",
          "ACTIVE",
          "COMPLETED",
          "RESOLVED",
          "READY",
          "INDEXED",
        ].includes(value)
          ? "green"
          : ["FAILED", "EXPIRED"].includes(value)
            ? "red"
            : [
                  "DRAFT",
                  "PENDING",
                  "OPEN",
                  "REVIEW_REQUIRED",
                  "PAUSED",
                ].includes(value)
              ? "amber"
              : "neutral"
      }
    >
      {statusLabels[value] || value || "—"}
    </Badge>
  );
}
export function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  useEffect(() => {
    const fn = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", fn);
    return () => document.removeEventListener("keydown", fn);
  }, [onClose]);
  return (
    <div className="modal-shade" onClick={onClose}>
      <section
        className={`modal ${wide ? "wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <header>
          <h2>{title}</h2>
          <button
            className="icon-button"
            aria-label="Закрыть"
            onClick={onClose}
          >
            <X size={20} />
          </button>
        </header>
        {children}
      </section>
    </div>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  const generatedId = useId();
  const child = isValidElement(children)
    ? (children as ReactElement<any>)
    : null;
  const controlId = child?.props.id || generatedId;
  return (
    <label className="field" htmlFor={controlId}>
      <span>{label}</span>
      {child
        ? cloneElement(child, {
            id: controlId,
            "aria-label": child.props["aria-label"] || label,
          })
        : children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
export function SearchInput({
  value,
  onChange,
  placeholder = "Найти…",
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="search">
      <Search size={17} />
      <input
        aria-label={placeholder}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}
export function Metric({
  label,
  value,
  detail,
}: {
  label: string;
  value: ReactNode;
  detail?: string;
}) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value ?? "—"}</strong>
      {detail && (
        <small>
          {detail} <ArrowUpRight size={13} />
        </small>
      )}
    </div>
  );
}
export function date(v: any) {
  return v
    ? utcDate(v).toLocaleDateString("ru-RU", {
        day: "numeric",
        month: "short",
        year: "numeric",
        timeZone: "Europe/Moscow",
      })
    : "—";
}
export function time(v: any, zone = "Europe/Moscow") {
  return v
    ? utcDate(v).toLocaleTimeString("ru-RU", {
        hour: "2-digit",
        minute: "2-digit",
        timeZone: zone,
      })
    : "";
}
function utcDate(v: any): Date {
  // Older SQLite exports contain UTC timestamps without an explicit suffix.
  // Date-only employment anchors remain calendar dates.
  const normalized =
    typeof v === "string" &&
    /T\d\d:\d\d/.test(v) &&
    !/(Z|[+-]\d\d:\d\d)$/.test(v)
      ? `${v}Z`
      : v;
  return new Date(normalized);
}
export function JSONView({ data }: { data: any }) {
  return <pre className="json-view">{JSON.stringify(data, null, 2)}</pre>;
}
