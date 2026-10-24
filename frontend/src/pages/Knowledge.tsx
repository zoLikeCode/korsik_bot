import { useEffect, useRef, useState, type ReactNode } from "react";
import { Upload, FileText } from "lucide-react";
import { items } from "../api";
import { useActor } from "../App";
import BasicFormulations from "./BasicFormulations";
import { UploadDocument, DocumentDetail } from "./KnowledgeSimple";
import {
  PageTitle,
  useResource,
  Loading,
  ErrorBox,
  Empty,
  Status,
  SearchInput,
  date,
} from "../ui";

export type KnowledgeTab = "documents" | "basic-formulations";
const knowledgeTabs: { key: KnowledgeTab; label: string }[] = [
  { key: "documents", label: "Документы" },
  { key: "basic-formulations", label: "Основные формулировки" },
];

export function knowledgeTabForKey(
  active: KnowledgeTab,
  key: string,
): KnowledgeTab | null {
  if (key === "Home") return "documents";
  if (key === "End") return "basic-formulations";
  if (key === "ArrowLeft" || key === "ArrowRight") {
    const index = knowledgeTabs.findIndex((tab) => tab.key === active);
    return knowledgeTabs[
      (index + (key === "ArrowRight" ? 1 : -1) + knowledgeTabs.length) %
        knowledgeTabs.length
    ].key;
  }
  return null;
}

export function KnowledgeTabs({
  active,
  onChange,
}: {
  active: KnowledgeTab;
  onChange: (tab: KnowledgeTab) => void;
}) {
  const buttons = useRef<Partial<Record<KnowledgeTab, HTMLButtonElement>>>({});
  return (
    <div
      className="section-tabs knowledge-tabs"
      role="tablist"
      aria-label="Раздел базы знаний"
    >
      {knowledgeTabs.map(({ key, label }) => (
        <button
          key={key}
          ref={(node) => {
            if (node) buttons.current[key] = node;
          }}
          type="button"
          role="tab"
          id={`knowledge-tab-${key}`}
          aria-controls={`knowledge-panel-${key}`}
          aria-selected={active === key}
          tabIndex={active === key ? 0 : -1}
          className={active === key ? "active" : ""}
          onClick={() => onChange(key)}
          onKeyDown={(event) => {
            const next = knowledgeTabForKey(active, event.key);
            if (next) {
              event.preventDefault();
              onChange(next);
              buttons.current[next]?.focus();
            }
          }}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export function KnowledgePanel({
  tab,
  active,
  children,
}: {
  tab: KnowledgeTab;
  active: KnowledgeTab;
  children: ReactNode;
}) {
  return (
    <section
      id={`knowledge-panel-${tab}`}
      role="tabpanel"
      aria-labelledby={`knowledge-tab-${tab}`}
      className={`knowledge-tab-panel knowledge-${tab}-panel`}
      hidden={active !== tab}
      tabIndex={0}
    >
      {children}
    </section>
  );
}

export function KnowledgeDocumentCard({
  document: d,
  onOpen,
}: {
  document: any;
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      className="document-card"
      aria-label={`Документ: ${d.title}`}
      aria-haspopup="dialog"
      onClick={onOpen}
    >
      <div className="document-top">
        <span className="document-icon">
          <FileText size={25} />
        </span>
      </div>
      <Status value={d.latest_revision_status || d.status} />
      <h3>{d.title}</h3>
      <p>
        {(
          {
            REGULATION: "Регламент",
            INSTRUCTION: "Памятка / инструкция",
            OVERVIEW: "Презентация",
            OTHER: "Другое",
          } as Record<string, string>
        )[d.document_category] || "Материал базы знаний"}
      </p>
      <div className="document-bottom document-updated">
        <span>Обновлён {date(d.updated_at)}</span>
      </div>
    </button>
  );
}

export default function Knowledge() {
  const state = useResource("/knowledge/documents");
  const editable = ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(
    useActor().role,
  );
  const hasProcessingDocuments = items(state.data).some((document) =>
    [
      "UPLOADED",
      "PENDING",
      "QUEUED",
      "PARSING",
      "PROCESSING",
      "INDEXING",
    ].includes(document.latest_revision_status || document.status),
  );
  useEffect(() => {
    if (hasProcessingDocuments) {
      const timer = setInterval(state.reload, 3500);
      return () => clearInterval(timer);
    }
  }, [hasProcessingDocuments]);
  const [activeTab, setActiveTab] = useState<KnowledgeTab>("documents");
  const [query, setQuery] = useState(""),
    [upload, setUpload] = useState(false),
    [selected, setSelected] = useState<any>(null);
  const rows = items(state.data).filter((d) =>
    `${d.title} ${d.document_category || ""}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  return (
    <>
      <PageTitle
        title="База знаний"
        action={
          activeTab === "documents" && editable ? (
            <div className="button-row wrap">
              <button
                className="button primary"
                onClick={() => setUpload(true)}
              >
                <Upload size={18} />
                Загрузить документ
              </button>
            </div>
          ) : undefined
        }
      ></PageTitle>
      <KnowledgeTabs active={activeTab} onChange={setActiveTab} />
      <KnowledgePanel tab="documents" active={activeTab}>
        <div className="toolbar">
          <SearchInput
            value={query}
            onChange={setQuery}
            placeholder="Название документа или категория"
          />
          <span className="muted">{rows.length} документов</span>
        </div>
        <ErrorBox error={state.error} />
        {state.loading && !state.data ? (
          <Loading />
        ) : rows.length ? (
          <div className="document-grid">
            {rows.map((d) => (
              <KnowledgeDocumentCard
                key={d.id}
                document={d}
                onOpen={() => setSelected(d)}
              />
            ))}
          </div>
        ) : (
          <section className="panel">
            <Empty title="Добавьте знания вашей команды"></Empty>
          </section>
        )}
      </KnowledgePanel>
      <KnowledgePanel tab="basic-formulations" active={activeTab}>
        <BasicFormulations />
      </KnowledgePanel>
      {upload && (
        <UploadDocument
          onClose={() => setUpload(false)}
          onDone={(d) => {
            setUpload(false);
            state.reload();
            setSelected(d);
          }}
        />
      )}
      {selected && (
        <DocumentDetail
          doc={selected}
          onClose={() => setSelected(null)}
          onChange={state.reload}
        />
      )}
    </>
  );
}
