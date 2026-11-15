import { type FormEvent, useState } from "react";
import { auth } from "../auth";

export function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !password) return;
    setBusy(true);
    setError(null);
    const result = await auth.login(password);
    setBusy(false);
    if (result.ok) {
      onDone();
    } else {
      setError(result.message);
      setPassword("");
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <form onSubmit={submit} className="panel w-full max-w-[360px] px-5 py-5" aria-labelledby="login-title">
        <h1 id="login-title" className="m-0 text-[19px] font-semibold tracking-tight">
          FX Signal Lab
        </h1>
        <p className="note m-0 mt-1">This dashboard is private. Enter the password to open it.</p>

        {/* lets a password manager file the entry under a name */}
        <input type="text" name="username" autoComplete="username" value="fxlab" readOnly hidden />
        <label className="mt-4 block text-[13px] text-ink-2" htmlFor="login-password">
          Password
        </label>
        <input
          id="login-password"
          className="field mt-1 w-full"
          type="password"
          name="password"
          autoComplete="current-password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          aria-invalid={error != null}
          aria-describedby={error ? "login-error" : undefined}
        />
        {error ? (
          <p id="login-error" role="alert" className="m-0 mt-2 text-[13px]" style={{ color: "var(--color-down)" }}>
            {error}
          </p>
        ) : null}
        <button type="submit" className="btn btn-primary mt-4 w-full" disabled={busy || !password}>
          {busy ? "Checking" : "Open the dashboard"}
        </button>
      </form>
    </div>
  );
}
