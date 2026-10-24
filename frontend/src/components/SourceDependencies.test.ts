import { describe, expect, it } from "vitest";
import { pinSourceDependency } from "./SourceDependencies";

describe("pinned source evidence", () => {
  it("hashes exact UTF-8 source bytes and pins immutable extraction, including figure image", async () => {
    const blocks = [
      { id: "text", kind: "TEXT", text: "Обед с 12:00." },
      {
        id: "figure",
        kind: "FIGURE",
        text: "",
        image_sha256: "image-version-hash",
      },
    ];
    const pin = await pinSourceDependency(
      "revision-a",
      { extraction_revision_id: "extraction-a" },
      blocks,
      "Точное время обеда",
    );
    expect(pin.source_block_sha256.text).toBe(
      "b074983b3e479dfe23b18d4469101a7498251f393174af4316d892f454c2a086",
    );
    expect(pin.source_block_image_sha256).toEqual({
      figure: "image-version-hash",
    });
    expect(pin.source_revision_id).toBe("revision-a");
    expect(pin.source_extraction_revision_id).toBe("extraction-a");
    blocks[0].text = "Обед с 14:00.";
    const changed = await pinSourceDependency(
      "revision-a",
      { id: "extraction-b" },
      [blocks[0]],
      "Изменилось время",
    );
    expect(changed.source_block_sha256.text).not.toBe(
      pin.source_block_sha256.text,
    );
    expect(pin.source_extraction_revision_id).toBe("extraction-a");
  });
});
