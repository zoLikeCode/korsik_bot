import { api } from "./api";

type Version = { id: string; etag: string; status: string } & Record<
  string,
  any
>;
type Current = {
  templateId: string | null;
  version: Version | null;
  body: Record<string, any>;
  onCreatedTemplate: (id: string) => void;
  onSavedVersion?: (version: Version) => void;
};
type Transport = Pick<typeof api, "get" | "post" | "patch">;

export function surveyPublicationLabel(template: {
  published_version_id?: string | null;
}) {
  return template.published_version_id ? "Опубликованный" : "Неопубликованный";
}

export async function saveSurveyDraft(
  current: Current,
  transport: Transport = api,
) {
  let templateId = current.templateId;
  if (!templateId) {
    const created = await transport.post("/survey-templates", current.body);
    templateId = created.data.id;
    if (!templateId)
      throw new Error("Не получен идентификатор сохранённого опроса");
    // Remember a successful creation before the following read/publish. A
    // failed second request must not cause another template to be created.
    current.onCreatedTemplate(templateId);
    const detail = await transport.get(`/survey-templates/${templateId}`);
    const version = detail.data.versions?.find(
      (row: Version) => row.status === "DRAFT",
    );
    if (!version) throw new Error("Не найден сохранённый черновик опроса");
    return { templateId, version: version as Version };
  }
  if (!current.version || current.version.status !== "DRAFT")
    throw new Error(
      "Для изменения опубликованного опроса создайте черновик из версии",
    );
  const saved = await transport.patch(
    `/survey-template-versions/${current.version.id}`,
    current.body,
    current.version.etag,
  );
  return { templateId, version: saved.data as Version };
}

export async function publishSurvey(
  current: Current & { dirty: boolean; reason: string },
  transport: Transport = api,
) {
  const saved =
    !current.templateId ||
    (current.version?.status === "DRAFT" && current.dirty)
      ? await saveSurveyDraft(current, transport)
      : { templateId: current.templateId, version: current.version };
  if (saved.version) current.onSavedVersion?.(saved.version);
  if (
    !saved.version ||
    !["DRAFT", "REVIEW_REQUIRED", "APPROVED"].includes(saved.version.status)
  )
    throw new Error(
      "Эта версия уже опубликована или недоступна для публикации",
    );
  const published = await transport.post(
    `/survey-template-versions/${saved.version.id}/publish`,
    { reason: current.reason },
    saved.version.etag,
  );
  return { templateId: saved.templateId, version: published.data as Version };
}
