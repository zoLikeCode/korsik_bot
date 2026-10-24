import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  KnowledgeDocumentCard,
  KnowledgePanel,
  KnowledgeTabs,
  knowledgeTabForKey,
} from "./pages/Knowledge";

describe("knowledge section navigation", () => {
  it("exposes only documents and base formulations, without control questions", () => {
    const html = renderToStaticMarkup(
      <KnowledgeTabs active="documents" onChange={() => {}} />,
    );
    expect(html.match(/role="tab"/g)).toHaveLength(2);
    expect(html).toContain("Документы");
    expect(html).toContain("Основные формулировки");
    expect(html).not.toContain("Контрольные вопросы");
  });
  it("wraps keyboard focus between the two remaining tabs", () => {
    for (const key of ["ArrowRight", "ArrowLeft", "End"])
      expect(knowledgeTabForKey("documents", key)).toBe("basic-formulations");
    for (const key of ["ArrowRight", "ArrowLeft", "Home"])
      expect(knowledgeTabForKey("basic-formulations", key)).toBe("documents");
    expect(knowledgeTabForKey("documents", "Tab")).toBeNull();
  });
  it("preserves an inactive document form without rendering removed question panels", () => {
    const html = renderToStaticMarkup(
      <KnowledgePanel tab="documents" active="basic-formulations">
        <input defaultValue="Регламент" />
      </KnowledgePanel>,
    );
    expect(html).toContain('hidden=""');
    expect(html).toContain('value="Регламент"');
    expect(html).not.toContain("control-questions");
  });

  it("keeps document cards native keyboard buttons without decorative navigation cues", () => {
    const html = renderToStaticMarkup(
      <KnowledgeDocumentCard
        document={{
          title: "Антикоррупционная политика",
          latest_revision_status: "PUBLISHED",
          document_category: "REGULATION",
          updated_at: "2026-10-24T10:00:00Z",
        }}
        onOpen={() => {}}
      />,
    );
    expect(html).toMatch(/^<button type="button"/);
    expect(html).toContain('aria-label="Документ: Антикоррупционная политика"');
    expect(html).toContain('aria-haspopup="dialog"');
    expect(html).toContain("Регламент");
    expect(html).toContain("Обновлён");
    expect(html).not.toContain("Открыть");
    expect(html).not.toContain("lucide-arrow-up-right");
    expect(html).not.toContain("lucide-chevron-right");
  });
});
