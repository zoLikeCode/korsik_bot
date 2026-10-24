export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : formatDetail(detail));
    this.status = status;
    this.detail = detail;
  }
}
export function apiSource(value: unknown, fallback: string): string {
  return typeof value === "string" &&
    value.startsWith("/api/v1/") &&
    !/[\r\n]/.test(value)
    ? value
    : fallback;
}
function formatDetail(detail: unknown): string {
  if (Array.isArray(detail))
    return detail
      .map(
        (x: any) =>
          `${x.loc?.slice(1).join(".") || "Поле"}: ${x.msg || "проверьте значение"}`,
      )
      .join("; ");
  if (detail && typeof detail === "object") {
    const d = detail as any;
    return (
      d.message ||
      d.error ||
      (d.code ? explainIssue(d.code) : JSON.stringify(detail))
    );
  }
  return "Не удалось выполнить запрос";
}
export type ResponseData<T = any> = { data: T; etag: string | null };
// API ETags encode immutable entity versions used for compare-and-swap. HTTP
// compression proxies can add W/ to the representation header; the version
// token itself must remain exact when sent back to the application.
function versionEtag(value: string | null | undefined) {
  return value ? value.replace(/^W\//, "") : null;
}

export function csrfToken() {
  return decodeURIComponent(
    document.cookie
      .split("; ")
      .find((x) => /^(csrf_token|csrf|korsik_csrf)=/.test(x))
      ?.split("=")
      .slice(1)
      .join("=") || "",
  );
}
export async function request<T = any>(
  path: string,
  options: RequestInit = {},
  etag?: string | null,
): Promise<ResponseData<T>> {
  const headers = new Headers(options.headers);
  const method = (options.method || "GET").toUpperCase();
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
    headers.set("Idempotency-Key", crypto.randomUUID());
  }
  if (etag) headers.set("If-Match", versionEtag(etag)!);
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers,
    credentials: "include",
  });
  let data: any = null;
  const type = response.headers.get("content-type") || "";
  if (response.status !== 204)
    data = type.includes("json")
      ? await response.json()
      : await response.text();
  if (!response.ok) {
    if (response.status === 401)
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(
      response.status,
      data?.detail ?? data ?? response.statusText,
    );
  }
  return { data, etag: versionEtag(response.headers.get("ETag")) };
}
export const api = {
  get: <T = any>(path: string) => request<T>(path),
  post: <T = any>(path: string, data: any = {}, etag?: string | null) =>
    request<T>(path, { method: "POST", body: JSON.stringify(data) }, etag),
  put: <T = any>(path: string, data: any, etag?: string | null) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(data) }, etag),
  patch: <T = any>(path: string, data: any, etag?: string | null) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(data) }, etag),
  delete: <T = any>(path: string, etag?: string | null) =>
    request<T>(path, { method: "DELETE" }, etag),
  upload: <T = any>(path: string, data: FormData, etag?: string | null) =>
    request<T>(path, { method: "POST", body: data }, etag),
};
export function items(data: any): any[] {
  return Array.isArray(data) ? data : (data?.items ?? data?.results ?? []);
}
export function errorText(e: unknown) {
  if (e instanceof ApiError && e.status === 412)
    return "Этот объект уже изменён другим пользователем. Обновите страницу перед повторным сохранением.";
  if (e instanceof ApiError && e.status === 403)
    return "Недостаточно прав для этого действия.";
  return e instanceof Error ? explainIssue(e.message) : "Произошла ошибка";
}
export function explainIssue(code: string): string {
  return (
    (
      {
        drain_required:
          "Дождитесь завершения текущих ответов перед переключением профиля.",
        pipeline_identity_changed:
          "Код поиска или формирования ответа изменился. Создайте и проверьте новый профиль.",
        invalid_generation_policy:
          "Настройки способа ответа несовместимы. Создайте новый профиль.",
        full_requires_local_generation:
          "В режиме FULL требуется профиль с локальной моделью ответа.",
        generation_disabled:
          "Локальная генерация выключена в конфигурации сервера. Обратитесь к администратору.",
        deployment_model_mismatch:
          "Выбранные модели отличаются от развёрнутых на сервере. Проверьте актуальные модели и создайте совместимый профиль.",
        current_semantic_review_required:
          "Для текущей модели и кода нет действующей успешной смысловой проверки. Прежняя техническая проверка не разрешает активацию.",
        live_runtime_unavailable:
          "Не удалось связаться с локальным сервисом модели. Проверьте его состояние.",
        live_runtime_identity_mismatch:
          "Локальные сервисы ещё не готовы или используют другие версии моделей либо кода. Проверьте конфигурацию и повторите проверку профиля.",
        enabled_scoped_hr_owner_required:
          "Выберите действующего HR-специалиста или администратора HR. Снять ответственного без замены нельзя.",
        owner_scope_does_not_cover_document:
          "Выбранный HR не имеет доступа ко всей аудитории документа. Выберите ответственного с подходящей областью полномочий.",
        owner_change_reason_required:
          "Укажите основание передачи ответственности за документ.",
        comparison_requires_immutable_extractions:
          "Сначала дождитесь завершения распознавания обеих редакций, затем повторите сравнение.",
        comparison_requires_same_document:
          "Для сравнения выберите две редакции одного документа.",
        file_signature_mismatch:
          "Содержимое файла не соответствует его расширению. Выберите исходный файл нужного формата.",
        source_changes_require_explicit_review:
          "Проверьте изменения обязательных шагов исходной программы и подтвердите их согласование.",
        scope_expansion_requires_archive_and_new_revision_review:
          "Для расширения аудитории действующего документа архивируйте его и проверьте новую ревизию.",
        rollback_requires_impact_review:
          "Перед восстановлением оцените влияние на связанные инструкции.",
        not_cancellable:
          "Для этой операции отмена недоступна. Дождитесь результата.",
        CONTROL_QUESTION_REGRESSION_FAILED:
          "Контрольные ответы не совпали с ожиданиями. Проверьте новую версию документа и закреплённые источники.",
        control_question_manual_review_required:
          "Оцените смысл контрольных ответов и сохраните ручную проверку перед согласованием.",
        control_question_manual_review_not_passed:
          "Ручная проверка ответов не пройдена. Исправьте материал и повторите QA.",
        machine_failure_cannot_be_overridden:
          "Неуспешную машинную проверку нельзя согласовать вручную. Исправьте причину и повторите QA.",
        qa_content_or_candidate_changed:
          "После проверки изменился документ или индекс. Повторите проверку актуального кандидата.",
        candidate_is_stale:
          "Кандидат устарел после изменений. Сначала соберите его заново.",
        stale_snapshot:
          "База знаний уже обновлена. Обновите страницу и пересоберите кандидат с актуальной базой.",
        published_revision_is_immutable:
          "Опубликованная версия защищена от изменений. Загрузите новую ревизию.",
        revision_not_candidate_member:
          "Сначала соберите индекс кандидата для этой ревизии.",
        configured_knowledge_retention_policy_required:
          "Удаление требует утверждённой политики хранения знаний.",
        legal_hold_prevents_purge:
          "Документ защищён обязательным хранением и пока не может быть удалён.",
        preview_profile_outside_grant:
          "Профиль проверки находится вне вашей области доступа.",
        source_review_incomplete:
          "Проверьте все страницы и устраните замечания к таблицам и изображениям.",
        revision_ids: "Версии источников не совпали с ожиданием",
        block_ids: "Фрагменты источников не совпали с ожиданием",
        mode: "Способ ответа не совпал с ожиданием",
        required_literals: "В ответе нет обязательной точной фразы",
        excluded_literals: "В ответ попала недопустимая фраза",
        current_scope: "Ответ ссылается на недоступный источник",
        instruction_revision:
          "В ответе нет закреплённой согласованной инструкции",
        no_citations_when_abstaining:
          "При передаче HR появились недопустимые ссылки",
      } as Record<string, string>
    )[code] || code
  );
}
