import { useState } from "react";
import type { Actor } from "./App";
import { api, errorText } from "./api";
import { ErrorBox, Field } from "./ui";

export default function CredentialsForm({
  actor,
  onDone,
}: {
  actor: Actor;
  onDone: (actor: Actor) => void;
}) {
  const [login, setLogin] = useState(actor.login || actor.email || "");
  const [current, setCurrent] = useState(""),
    [password, setPassword] = useState(""),
    [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <form
      onSubmit={async (event) => {
        event.preventDefault();
        if (busy) return;
        if (password !== repeat) {
          setError("Новые пароли не совпадают");
          return;
        }
        setError("");
        setBusy(true);
        try {
          const result = await api.patch("/auth/credentials", {
            current_password: current,
            login,
            new_password: password,
          });
          onDone(result.data.actor);
        } catch (e) {
          setError(errorText(e));
        } finally {
          setBusy(false);
        }
      }}
    >
      <ErrorBox error={error} />
      <Field label="Логин">
        <input
          required
          minLength={3}
          maxLength={255}
          autoComplete="username"
          value={login}
          onChange={(e) => setLogin(e.target.value)}
          disabled={busy}
        />
      </Field>
      <Field label="Текущий пароль">
        <input
          type="password"
          autoComplete="current-password"
          required
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          disabled={busy}
        />
      </Field>
      <Field label="Новый пароль">
        <input
          type="password"
          autoComplete="new-password"
          required
          minLength={12}
          maxLength={256}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          disabled={busy}
        />
      </Field>
      <Field label="Повторите новый пароль">
        <input
          type="password"
          autoComplete="new-password"
          required
          minLength={12}
          value={repeat}
          onChange={(e) => setRepeat(e.target.value)}
          disabled={busy}
        />
      </Field>
      <div className="form-actions">
        <button className="button primary" disabled={busy}>
          {busy ? "Сохраняем…" : "Сохранить изменения"}
        </button>
      </div>
    </form>
  );
}
