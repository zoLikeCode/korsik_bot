import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, errorText, items, apiSource } from "./api";
afterEach(() => vi.unstubAllGlobals());
describe("authenticated API transport", () => {
  it("sends CSRF, an idempotency key and the precise server ETag on mutations", async () => {
    vi.stubGlobal("document", { cookie: "korsik_csrf=csrf-example" });
    const fetch = vi.fn(
      async (_url: string, _options?: RequestInit) =>
        new Response(JSON.stringify({ id: "created" }), {
          status: 201,
          headers: { "Content-Type": "application/json", ETag: '"v2"' },
        }),
    );
    vi.stubGlobal("fetch", fetch);
    const result = await api.put(
      "/scenario-versions/example",
      { graph: { steps: [] } },
      '"v1"',
    );
    const [url, options] = fetch.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/api/v1/scenario-versions/example");
    expect(options.credentials).toBe("include");
    const headers = new Headers(options.headers);
    expect(headers.get("X-CSRF-Token")).toBe("csrf-example");
    expect(headers.get("If-Match")).toBe('"v1"');
    expect(headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(result.etag).toBe('"v2"');
  });
  it("preserves the exact entity version through a compression proxy's weak ETag", async () => {
    vi.stubGlobal("document", { cookie: "korsik_csrf=csrf-example" });
    const fetch = vi.fn(
      async (_url: string, _options?: RequestInit) =>
        new Response(JSON.stringify({ extraction_revision_id: "revision" }), {
          headers: {
            "Content-Type": "application/json",
            ETag: 'W/"revision:7"',
          },
        }),
    );
    vi.stubGlobal("fetch", fetch);
    const current = await api.get("/knowledge/revisions/source/extraction");
    expect(current.etag).toBe('"revision:7"');
    await api.patch(
      "/knowledge/revisions/source/extraction/blocks/block",
      { patch_type: "TEXT", text: "Verified source" },
      current.etag,
    );
    expect(new Headers(fetch.mock.calls[1][1]?.headers).get("If-Match")).toBe(
      '"revision:7"',
    );
  });
  it("leaves multipart boundaries to the browser", async () => {
    vi.stubGlobal("document", { cookie: "" });
    const fetch = vi.fn(
      async (_url: string, _options?: RequestInit) =>
        new Response("{}", { headers: { "Content-Type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetch);
    const form = new FormData();
    form.append("file", new Blob(["test"]), "test.txt");
    await api.upload("/knowledge/documents/id/revisions", form);
    const options = fetch.mock.calls[0][1] as RequestInit;
    expect(new Headers(options.headers).has("Content-Type")).toBe(false);
    expect(options.body).toBe(form);
  });
  it("surfaces a conflict instead of presenting a saved state", async () => {
    vi.stubGlobal("document", { cookie: "" });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: "stale" }), {
            status: 412,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    await expect(api.put("/scenario-versions/id", {})).rejects.toBeInstanceOf(
      ApiError,
    );
    expect(errorText(new ApiError(412, "stale"))).toContain("уже изменён");
  });
  it("keeps an activation conflict failed and explains the missing current semantic review", async () => {
    vi.stubGlobal("document", { cookie: "" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({
        detail: { code: "current_semantic_review_required", role: "generator" },
      }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      })),
    );
    let failure: unknown;
    try {
      await api.post("/local-ai/profiles/example/activate", {});
    } catch (e) {
      failure = e;
    }
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(409);
    expect((failure as ApiError).detail).toEqual({
      code: "current_semantic_review_required", role: "generator",
    });
    expect(errorText(failure)).toContain("смысловой проверки");
    expect(errorText(failure)).toContain("не разрешает активацию");
  });
  it("normalizes backend list envelopes without inventing records", () => {
    expect(items({ items: [{ id: 1 }] })).toEqual([{ id: 1 }]);
    expect(items(null)).toEqual([]);
    expect(items({ error: "offline" })).toEqual([]);
  });
});

it("keeps evidence links inside the authorized local API", () => {
  expect(apiSource("javascript:alert(1)", "/api/v1/citations/id/source")).toBe(
    "/api/v1/citations/id/source",
  );
  expect(
    apiSource("https://outside.example/source", "/api/v1/citations/id/source"),
  ).toBe("/api/v1/citations/id/source");
  expect(apiSource("/api/v1/citations/ok/source", "/fallback")).toBe(
    "/api/v1/citations/ok/source",
  );
});
