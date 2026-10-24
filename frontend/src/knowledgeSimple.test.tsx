import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  documentFileError,
  DocumentAudience,
  UploadDocument,
  publicationBlockers,
  uploadDocumentRevision,
  revisionSelection,
  extractionEditSnapshot,
  extractionEditRequest,
  extractionPagePath,
  isExtractionConflict,
  extractionResourceError,
  isRevisionLocked,
  RecognizedContentStatus,
} from "./pages/KnowledgeSimple";

import { api, ApiError } from "./api";

afterEach(() => vi.restoreAllMocks());
const file = (name = "Регламент.pdf", size = 100) => ({ name, size });
describe("single document upload", () => {
  it("rejects a multiple-file drop as a whole instead of keeping its first file", () => {
    expect(documentFileError([file(), file("Второй.pdf")])).toContain(
      "один файл",
    );
    expect(documentFileError([])).toContain("один файл");
  });
  it("accepts supported text, scan and presentation formats within the limit", () => {
    for (const name of [
      "PDF.PDF",
      "слайды.pptx",
      "текст.txt",
      "описание.md",
      "скан.png",
      "скан.jpg",
      "скан.jpeg",
    ])
      expect(documentFileError([file(name, 50 * 1024 * 1024)])).toBe("");
    expect(documentFileError([file("файл.exe")])).toContain("Поддерживаются");
    expect(
      documentFileError([file("текст.pdf", 50 * 1024 * 1024 + 1)]),
    ).toContain("50 МБ");
    expect(documentFileError([file("текст.pdf", 0)])).toContain("пустой");
  });
  it("renders one file input and only the requested upload fields", () => {
    const html = renderToStaticMarkup(
      <UploadDocument onClose={() => {}} onDone={() => {}} />,
    );
    expect(html.match(/type="file"/g)).toHaveLength(1);
    expect(html).not.toContain("multiple");
    for (const label of [
      "Название документа",
      "Категория",
      "Кому доступен документ",
      "Доступен всем",
    ])
      expect(html).toContain(label);
    for (const removed of [
      "Действует с",
      "Действует до",
      "Что изменилось",
      "новые сроки",
      "несколько файлов",
    ])
      expect(html).not.toContain(removed);
  });
  it("keeps replacement audience and title immutable instead of broadening an existing document", () => {
    const html = renderToStaticMarkup(
      <UploadDocument
        existing={{ id: "d", title: "Существующий регламент" }}
        onClose={() => {}}
        onDone={() => {}}
      />,
    );
    expect(html).toContain("Обновить документ");
    expect(html).toMatch(
      /disabled=""[^>]*value="Существующий регламент"|value="Существующий регламент"[^>]*disabled=""/,
    );
    expect(html).not.toContain("Кому доступен документ");
    expect(html).not.toContain("Категория");
  });
});
describe("knowledge publication and audience controls", () => {
  it("disables unit selection when document is available to all authorized recipients", () => {
    const html = renderToStaticMarkup(
      <DocumentAudience
        units={[{ id: "u1", title: "Отдел" }]}
        all
        selected={["u1"]}
        onAll={() => {}}
        onSelected={() => {}}
      />,
    );
    expect(html).toMatch(/type="checkbox"[^>]*checked=""/);
    expect(html).toMatch(/<button[^>]*disabled=""/);
    expect(html).toContain('aria-expanded="false"');
  });
  it("allows explicit unit selection when available-to-all is unchecked", () => {
    const html = renderToStaticMarkup(
      <DocumentAudience
        units={[]}
        all={false}
        selected={[]}
        onAll={() => {}}
        onSelected={() => {}}
      />,
    );
    expect(html).not.toContain('disabled=""');
    expect(html).toContain("Выберите подразделения");
  });
  it("shows compact server readiness blockers without hiding failures", () => {
    expect(
      publicationBlockers({
        blockers: [
          { message: "Проверьте изображение на странице 2" },
          "Документ обрабатывается",
        ],
      }),
    ).toEqual([
      "Проверьте изображение на странице 2",
      "Документ обрабатывается",
    ]);
    expect(publicationBlockers(null)).toEqual([]);
  });
});

describe("simple document upload API handshake", () => {
  it("opens the nested revision id and uploads one file with the simple workflow flag and fresh ETag", async () => {
    const source = new File(["%PDF-1.7"], "Регламент.pdf", {
      type: "application/pdf",
    });
    vi.spyOn(api, "get").mockResolvedValue({
      data: { id: "doc" },
      etag: '"document:7"',
    });
    const uploaded = vi.spyOn(api, "upload").mockResolvedValue({
      data: {
        revision: { id: "new-revision" },
        operation: { id: "ingest-operation" },
      },
      etag: '"document:8"',
    });
    await expect(uploadDocumentRevision("doc", source)).resolves.toBe(
      "new-revision",
    );
    expect(api.get).toHaveBeenCalledWith("/knowledge/documents/doc");
    const [path, form, etag] = uploaded.mock.calls[0];
    expect(path).toBe("/knowledge/documents/doc/revisions");
    expect(etag).toBe('"document:7"');
    expect(form.getAll("file")).toHaveLength(1);
    expect(form.get("file")).toBe(source);
    expect(form.get("simple_workflow")).toBe("true");
  });
  it("preserves a stale-write failure instead of pretending a revision was created", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ data: {}, etag: '"document:7"' });
    const stale = new ApiError(412, "stale_etag");
    vi.spyOn(api, "upload").mockRejectedValue(stale);
    await expect(
      uploadDocumentRevision("doc", new File(["text"], "text.txt")),
    ).rejects.toBe(stale);
  });
  it("never mistakes a top-level operation id for a usable document revision", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ data: {}, etag: '"document:7"' });
    vi.spyOn(api, "upload").mockResolvedValue({
      data: { id: "operation-only" },
      etag: null,
    });
    await expect(
      uploadDocumentRevision("doc", new File(["text"], "text.txt")),
    ).rejects.toThrow("Сервер не вернул версию документа");
  });
});

describe("document revision and concurrent correction safety", () => {
  it("keeps a replacement selected while the revisions list still contains only the old version", () => {
    const oldList = [{ id: "old" }];
    expect(revisionSelection("new", oldList, "new")).toEqual({
      selected: "new",
      pending: "new",
    });
    const loaded = revisionSelection("new", [{ id: "new" }, ...oldList], "new");
    expect(loaded).toEqual({ selected: "new", pending: "" });
    expect(
      revisionSelection(
        loaded.selected,
        [{ id: "new" }, ...oldList],
        loaded.pending,
      ),
    ).toEqual(loaded);
    expect(revisionSelection("old", [{ id: "new" }, ...oldList], "")).toEqual({
      selected: "old",
      pending: "",
    });
  });
  it("cannot silently repin old typed edits to another editor's newer extraction after a stale-write failure", async () => {
    const displayed = {
      id: "block",
      kind: "TEXT",
      text: "Первоначальный текст",
    };
    const draft = extractionEditSnapshot(
      displayed,
      "extraction-v1",
      '"extraction:1"',
    );
    displayed.text = "Правка другого HR, пришедшая при обновлении";
    const patch = vi
      .spyOn(api, "patch")
      .mockRejectedValue(new ApiError(412, "stale_etag"));
    const request = extractionEditRequest(draft, "Мой несохранённый текст");
    await expect(
      api.patch(
        `/knowledge/revisions/r/extraction/blocks/${request.blockId}`,
        request.body,
        request.etag,
      ),
    ).rejects.toMatchObject({ status: 412 });
    expect(patch.mock.calls[0][2]).toBe('"extraction:1"');
    expect(request.body.expected_extraction_revision_id).toBe("extraction-v1");
    expect(draft.block.text).toBe("Первоначальный текст");
    expect(extractionEditRequest(draft, "Повторная попытка").etag).toBe(
      '"extraction:1"',
    );
    const explicitlyReloaded = extractionEditSnapshot(
      displayed,
      "extraction-v2",
      '"extraction:2"',
    );
    expect(
      extractionEditRequest(explicitlyReloaded, displayed.text),
    ).toMatchObject({
      etag: '"extraction:2"',
      body: {
        expected_extraction_revision_id: "extraction-v2",
        text: displayed.text,
      },
    });
  });
  it("does not open an editable snapshot until both extraction identity and ETag are loaded", () => {
    expect(() => extractionEditSnapshot({ id: "b" }, "e", null)).toThrow(
      "Дождитесь",
    );
    expect(() => extractionEditSnapshot({ id: "b" }, "", '"1"')).toThrow(
      "Дождитесь",
    );
  });
  it("keeps table and image corrections on the same captured extraction", () => {
    const table = extractionEditRequest(
      extractionEditSnapshot({ id: "t", kind: "TABLE" }, "e", '"1"'),
      "Часы\tМесто\n12:00\tСтоловая",
    );
    expect(table.body).toMatchObject({
      patch_type: "TABLE",
      grid: [
        ["Часы", "Место"],
        ["12:00", "Столовая"],
      ],
      expected_extraction_revision_id: "e",
    });
    const image = extractionEditRequest(
      extractionEditSnapshot({ id: "f", kind: "FIGURE" }, "e", '"1"'),
      "Подтверждённое описание",
    );
    expect(image.body).toMatchObject({
      patch_type: "FIGURE_DESCRIPTION",
      description: "Подтверждённое описание",
      expected_extraction_revision_id: "e",
    });
  });
  it("explicitly prepares an uploaded duplicate editable revision with its own ETag, never the document ETag", async () => {
    vi.spyOn(api, "get")
      .mockResolvedValueOnce({ data: {}, etag: '"document:7"' })
      .mockResolvedValueOnce({ data: { id: "r" }, etag: '"revision:3"' });
    vi.spyOn(api, "upload").mockResolvedValue({
      data: {
        duplicate: true,
        revision: { id: "r", status: "REVIEW_REQUIRED" },
      },
      etag: null,
    });
    const prepare = vi.spyOn(api, "post").mockResolvedValue({
      data: { revision: { id: "r" } },
      etag: '"revision:4"',
    });
    await expect(
      uploadDocumentRevision("doc", new File(["text"], "text.txt")),
    ).resolves.toBe("r");
    expect(prepare).toHaveBeenCalledWith(
      "/knowledge/revisions/r/simple-prepare",
      {},
      '"revision:3"',
    );
  });
  it("does not mutate a published duplicate or enqueue prepare merely by opening it", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ data: {}, etag: '"document:7"' });
    vi.spyOn(api, "upload").mockResolvedValue({
      data: {
        duplicate: true,
        revision: { id: "published", status: "PUBLISHED" },
      },
      etag: null,
    });
    const prepare = vi.spyOn(api, "post");
    await expect(
      uploadDocumentRevision("doc", new File(["text"], "text.txt")),
    ).resolves.toBe("published");
    expect(prepare).not.toHaveBeenCalled();
  });
});

describe("large document page loading", () => {
  it("requests the selected physical page rather than reusing a truncated document-wide first500-block response", () => {
    expect(extractionPagePath("/knowledge/revisions/r", 73)).toBe(
      "/knowledge/revisions/r/extraction?page_from=73&limit=2000",
    );
    expect(extractionPagePath("/knowledge/revisions/r", -1)).toContain(
      "page_from=1&limit=2000",
    );
  });
});

describe("correction conflicts", () => {
  it("requires an explicit reload for stale ETag, stale extraction and rejected URI without treating validation errors as permission to repin", () => {
    for (const error of [
      new ApiError(412, "stale_etag"),
      new ApiError(409, "stale_extraction"),
      new ApiError(414, "URI Too Long"),
    ])
      expect(isExtractionConflict(error)).toBe(true);
    expect(isExtractionConflict(new ApiError(422, "invalid_table_grid"))).toBe(
      false,
    );
    expect(isExtractionConflict(new Error("network unavailable"))).toBe(false);
  });
});

describe("normal preparation versus real failures", () => {
  it("does not show expected not-ready extraction/pages as red errors while processing", () => {
    expect(extractionResourceError("extraction_not_ready", true)).toBe("");
    expect(extractionResourceError("extraction_not_ready", false)).toContain(
      "ещё не готово",
    );
  });
  it("keeps network, parser and access failures visible even during processing", () => {
    for (const error of [
      "Failed to fetch",
      "parser_unavailable",
      "file_signature_mismatch",
      "Недостаточно прав для этого действия.",
    ])
      expect(extractionResourceError(error, true)).toBe(error);
  });
  it("keeps rejected/cancelled revisions read-only while a failed draft remains repairable", () => {
    for (const status of [
      "PUBLISHED",
      "ARCHIVED",
      "PURGED",
      "PURGE_PENDING",
      "REJECTED",
      "CANCELLED",
    ])
      expect(isRevisionLocked(status)).toBe(true);
    for (const status of ["REVIEW_REQUIRED", "FAILED", "APPROVED"])
      expect(isRevisionLocked(status)).toBe(false);
  });
});

describe("whole-version approval label", () => {
  it("truthfully labels the published approval without manufacturing reviewed page flags", () => {
    const page = Object.freeze({
      review_status: "PENDING",
      status: "PROCESSED",
    });
    const html = renderToStaticMarkup(
      <RecognizedContentStatus
        revision={{
          status: "PUBLISHED",
          simple_approval: { method: "EXPLICIT_WHOLE_REVISION_APPROVAL" },
        }}
        page={page}
      />,
    );
    expect(html).toContain("Версия подтверждена");
    expect(html).not.toContain("Нужна проверка");
    expect(page.review_status).toBe("PENDING");
  });
  it("does not label draft, legacy published or an unrelated approval as confirmed", () => {
    for (const revision of [
      {
        status: "REVIEW_REQUIRED",
        simple_approval: { method: "EXPLICIT_WHOLE_REVISION_APPROVAL" },
      },
      { status: "PUBLISHED" },
      { status: "PUBLISHED", simple_approval: { method: "OTHER" } },
    ]) {
      const html = renderToStaticMarkup(
        <RecognizedContentStatus
          revision={revision}
          page={{ review_status: "PENDING" }}
        />,
      );
      expect(html).not.toContain("Версия подтверждена");
      expect(html).toContain("Нужна проверка");
    }
  });
});
