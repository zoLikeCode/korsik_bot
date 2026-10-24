import { useEffect, useState } from "react";
import { api } from "./api";
import "./DialogNavBadge.css";

export default function DialogNavBadge() {
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    let live = true;
    let loading = false;
    async function refresh() {
      if (loading) return;
      loading = true;
      try {
        const result = await api.get("/conversations/problem-summary");
        if (
          live &&
          Number.isSafeInteger(result.data.problem_conversations_count)
        ) {
          setCount(Math.max(0, result.data.problem_conversations_count));
        }
      } catch {
        /* Retain the last observed count until a successful refresh. */
      } finally {
        loading = false;
      }
    }
    refresh();
    const timer = setInterval(refresh, 15000);
    window.addEventListener("korsik:dialog-problems-updated", refresh);
    return () => {
      live = false;
      clearInterval(timer);
      window.removeEventListener("korsik:dialog-problems-updated", refresh);
    };
  }, []);
  return <DialogCount count={count} />;
}

export function DialogCount({ count }: { count: number | null }) {
  return count && count > 0 ? (
    <span
      className="dialog-nav-badge"
      aria-label={`Диалогов с вопросами без ответа: ${count}`}
    >
      {count}
    </span>
  ) : null;
}
