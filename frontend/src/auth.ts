/** Login state of this browser.  The session itself is an HttpOnly cookie the script never sees. */

export interface Session {
  /** false: this installation has no password (private network only) */
  required: boolean;
  authenticated: boolean;
}

export type LoginResult = { ok: true } | { ok: false; message: string };

const listeners = new Set<() => void>();

/** Called by the API client whenever the backend answers 401: the session ended. */
export function notifyUnauthorized(): void {
  for (const listener of listeners) listener();
}

export function onUnauthorized(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** What the login form shows for each way a login can fail. */
export function loginMessage(status: number, detail: string | undefined, retryAfter: string | null): string {
  if (status === 401) return detail && detail !== "wrong password" ? detail : "Wrong password.";
  if (status === 429) {
    const seconds = Number(retryAfter);
    return Number.isFinite(seconds) && seconds > 0
      ? `Too many failed attempts. Try again in ${Math.ceil(seconds)} s.`
      : "Too many failed attempts. Try again in a minute.";
  }
  return `The backend answered ${status}. Try again.`;
}

export const auth = {
  async session(): Promise<Session> {
    const response = await fetch("/api/auth/session");
    if (!response.ok) throw new Error(`session check failed: ${response.status}`);
    return (await response.json()) as Session;
  },

  async login(password: string): Promise<LoginResult> {
    let response: Response;
    try {
      response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
    } catch {
      return { ok: false, message: "The backend is not reachable. Try again." };
    }
    if (response.ok) return { ok: true };
    let detail: string | undefined;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // no body: the status is enough
    }
    return { ok: false, message: loginMessage(response.status, detail, response.headers.get("Retry-After")) };
  },

  async logout(): Promise<void> {
    await fetch("/api/auth/logout", { method: "POST" }).catch(() => undefined);
  },
};
