import { api } from "./api";

export type BindingReview = {
  id: string;
  etag: string;
  displayName: string;
  maxUserId: string;
  expiresAt: string | null;
  key: string;
};

export function bindingReview(
  data: any,
  etag: string | null,
): BindingReview | null {
  const id = data?.binding_id || data?.id;
  const displayName = String(data?.display_name || "").trim();
  const maxUserId = String(data?.max_user_id || "").trim();
  if (
    data?.status !== "PENDING_VERIFICATION" ||
    !id ||
    !etag ||
    !displayName ||
    !maxUserId
  )
    return null;
  return {
    id,
    etag,
    displayName,
    maxUserId,
    expiresAt: data.verification_expires_at || null,
    key: JSON.stringify([id, etag, displayName, maxUserId, data.episode_id]),
  };
}

export function canApproveBinding(
  review: BindingReview | null,
  checkedKey: string | null,
  reason: string,
) {
  return (
    !!review &&
    checkedKey === review.key &&
    reason.trim().length >= 3 &&
    reason.trim().length <= 2000 &&
    (!review.expiresAt || new Date(review.expiresAt).getTime() > Date.now())
  );
}

export async function approveReviewedBinding(
  review: BindingReview | null,
  checkedKey: string | null,
  reason: string,
) {
  if (!canApproveBinding(review, checkedKey, reason))
    throw new Error(
      "Проверьте указанный аккаунт MAX по известному каналу, отметьте подтверждение личности и укажите основание.",
    );
  const response = await api.post(
    `/channel-bindings/${review!.id}/approve`,
    {
      verified_method: "HR_CONFIRMED_KNOWN_CHANNEL",
      reason: reason.trim(),
    },
    review!.etag,
  );
  if (
    response.data?.status !== "ACTIVE" ||
    (response.data.binding_id || response.data.id) !== review!.id
  )
    throw new Error(
      "Сервер не подтвердил активацию привязки. Обновите карточку перед повторной проверкой.",
    );
  return response;
}
