import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiError } from "./api";
import {
  approveReviewedBinding,
  bindingReview,
  canApproveBinding,
} from "./bindingReview";
import { BindingIdentity } from "./pages/EmployeeMaxBinding";

const row = {
  id: "binding-one",
  binding_id: "binding-one",
  status: "PENDING_VERIFICATION",
  display_name: "Тестовый <MAX>",
  max_user_id: "987654321",
  episode_id: "episode-one",
  verification_expires_at: "2099-01-01T00:00:00Z",
};
afterEach(() => vi.unstubAllGlobals());

describe("reviewing an employee MAX identity before approval", () => {
  it("shows the actual returned MAX name and user ID as escaped text", () => {
    const review = bindingReview(row, '"binding:2"')!;
    const html = renderToStaticMarkup(<BindingIdentity review={review} />);
    expect(html).toContain("Тестовый &lt;MAX&gt;");
    expect(html).toContain("987654321");
    expect(html).toContain("ID пользователя MAX");
    expect(
      bindingReview({ ...row, max_user_id: null }, '"binding:2"'),
    ).toBeNull();
    expect(
      bindingReview({ ...row, status: "ACTIVE" }, '"binding:2"'),
    ).toBeNull();
  });

  it("makes no approval request without explicit confirmation of the displayed identity", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const review = bindingReview(row, '"binding:2"')!;
    expect(canApproveBinding(review, null, "Личная проверка")).toBe(false);
    await expect(
      approveReviewedBinding(review, null, "Личная проверка"),
    ).rejects.toThrow("Проверьте");
    await expect(
      approveReviewedBinding(review, review.key, " "),
    ).rejects.toThrow("Проверьте");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("invalidates confirmation when the account or displayed server version changes", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const oldReview = bindingReview(row, '"binding:2"')!;
    const newReview = bindingReview(
      { ...row, max_user_id: "different-account" },
      '"binding:3"',
    )!;
    expect(canApproveBinding(newReview, oldReview.key, "Личная проверка")).toBe(
      false,
    );
    await expect(
      approveReviewedBinding(newReview, oldReview.key, "Личная проверка"),
    ).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("sends the exact reviewed ETag, CSRF and explicit reason and waits for ACTIVE", async () => {
    vi.stubGlobal("document", { cookie: "korsik_csrf=review-csrf" });
    const fetch = vi.fn(
      async (_url: string, _options?: RequestInit) =>
        new Response(JSON.stringify({ binding_id: row.id, status: "ACTIVE" }), {
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetch);
    const review = bindingReview(row, '"binding:2"')!;
    const response = await approveReviewedBinding(
      review,
      review.key,
      "  Лично сверены аккаунт и сотрудник  ",
    );
    const [url, options] = fetch.mock.calls[0];
    expect(url).toBe("/api/v1/channel-bindings/binding-one/approve");
    expect(new Headers(options?.headers).get("If-Match")).toBe('"binding:2"');
    expect(new Headers(options?.headers).get("X-CSRF-Token")).toBe(
      "review-csrf",
    );
    expect(options?.credentials).toBe("include");
    expect(JSON.parse(options?.body as string)).toEqual({
      verified_method: "HR_CONFIRMED_KNOWN_CHANNEL",
      reason: "Лично сверены аккаунт и сотрудник",
    });
    expect(response.data.status).toBe("ACTIVE");
  });

  it.each([412, 500])(
    "preserves server failure %i without claiming approval",
    async (status) => {
      vi.stubGlobal("document", { cookie: "" });
      vi.stubGlobal(
        "fetch",
        vi.fn(
          async () =>
            new Response(JSON.stringify({ detail: "Not approved" }), {
              status,
              headers: { "Content-Type": "application/json" },
            }),
        ),
      );
      const review = bindingReview(row, '"binding:2"')!;
      await expect(
        approveReviewedBinding(review, review.key, "Личная проверка"),
      ).rejects.toBeInstanceOf(ApiError);
    },
  );

  it("rejects a successful HTTP response that does not confirm this exact active binding", async () => {
    vi.stubGlobal("document", { cookie: "" });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({ binding_id: "other-binding", status: "ACTIVE" }),
            { headers: { "Content-Type": "application/json" } },
          ),
      ),
    );
    const review = bindingReview(row, '"binding:2"')!;
    await expect(
      approveReviewedBinding(review, review.key, "Личная проверка"),
    ).rejects.toThrow("не подтвердил");
  });
});
