import { useState } from "react";
import { errorText } from "../api";
import {
  approveReviewedBinding,
  bindingReview,
  canApproveBinding,
  type BindingReview,
} from "../bindingReview";
import { ErrorBox, Field, Loading, Notice, useResource } from "../ui";

export function BindingIdentity({ review }: { review: BindingReview }) {
  return (
    <div
      className="detail-facts"
      aria-label="Ожидающий подтверждения аккаунт MAX"
    >
      <span>
        <strong>Имя в MAX:</strong> {review.displayName}
      </span>
      <span>
        <strong>ID пользователя MAX:</strong> {review.maxUserId}
      </span>
    </div>
  );
}

export default function EmployeeMaxBinding({
  bindingId,
  onApproved,
}: {
  bindingId: string;
  onApproved: () => void;
}) {
  const binding = useResource(`/channel-bindings/${bindingId}`);
  const review = bindingReview(binding.data, binding.etag);
  const [checkedKey, setCheckedKey] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  const refresh = () => {
    setCheckedKey(null);
    setError("");
    binding.reload();
  };
  return (
    <form
      onSubmit={async (event) => {
        event.preventDefault();
        setBusy(true);
        setError("");
        try {
          await approveReviewedBinding(review, checkedKey, reason);
          setSaved(true);
          onApproved();
        } catch (e) {
          setCheckedKey(null);
          setError(errorText(e));
          // Any retry requires reviewing the current account and version again.
          binding.reload();
        } finally {
          setBusy(false);
        }
      }}
    >
      <h4>Проверка личности сотрудника</h4>
      <ErrorBox error={error || binding.error} />
      {binding.loading ? (
        <Loading />
      ) : review ? (
        <>
          <BindingIdentity review={review} />
          <Notice>
            Сверьте этот аккаунт с сотрудником по заранее известному каналу. Имя
            в MAX может быть изменено владельцем аккаунта; перехода по
            приглашению недостаточно для подтверждения личности.
          </Notice>
          {review.expiresAt && (
            <p className="muted">
              Подтверждение доступно до{" "}
              {new Date(review.expiresAt).toLocaleString("ru-RU", {
                timeZoneName: "short",
              })}
              .
            </p>
          )}
          <label className="check-field spaced">
            <input
              type="checkbox"
              checked={checkedKey === review.key}
              disabled={busy || saved}
              onChange={(event) =>
                setCheckedKey(event.target.checked ? review.key : null)
              }
            />
            Я проверил(а) личность сотрудника и принадлежность указанного
            аккаунта MAX
          </label>
          <Field label="Основание подтверждения">
            <textarea
              value={reason}
              minLength={3}
              maxLength={2000}
              required
              disabled={busy || saved}
              placeholder="Например: лично сверены аккаунт и сотрудник"
              onChange={(event) => setReason(event.target.value)}
            />
          </Field>
          <div className="button-row wrap">
            <button
              type="submit"
              className="button primary"
              disabled={
                busy || saved || !canApproveBinding(review, checkedKey, reason)
              }
            >
              {saved
                ? "Привязка подтверждена"
                : busy
                  ? "Подтверждаем…"
                  : "Подтвердить привязку"}
            </button>
            <button
              type="button"
              className="button secondary"
              disabled={busy || saved}
              onClick={refresh}
            >
              Обновить аккаунт
            </button>
          </div>
        </>
      ) : (
        <>
          {!binding.error && (
            <Notice>
              Нет доступной ожидающей привязки. Обновите карточку; при истёкшем
              сроке выдайте новое приглашение.
            </Notice>
          )}
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={refresh}
          >
            Обновить аккаунт
          </button>
        </>
      )}
    </form>
  );
}
