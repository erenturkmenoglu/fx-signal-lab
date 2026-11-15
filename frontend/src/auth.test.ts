import { describe, expect, it, vi } from "vitest";
import { loginMessage, notifyUnauthorized, onUnauthorized } from "./auth";

describe("loginMessage", () => {
  it("says a wrong password plainly", () => {
    expect(loginMessage(401, "wrong password", null)).toBe("Wrong password.");
    expect(loginMessage(401, undefined, null)).toBe("Wrong password.");
  });

  it("passes on the server's reason when the login itself is broken", () => {
    const reason = "DASHBOARD_PASSWORD_HASH is unusable (not a password hash); nobody can log in until it is fixed";
    expect(loginMessage(401, reason, null)).toBe(reason);
  });

  it("tells how long to wait after too many attempts", () => {
    expect(loginMessage(429, "too many failed logins", "37")).toBe("Too many failed attempts. Try again in 37 s.");
    expect(loginMessage(429, undefined, null)).toBe("Too many failed attempts. Try again in a minute.");
    expect(loginMessage(429, undefined, "soon")).toBe("Too many failed attempts. Try again in a minute.");
  });

  it("names any other status", () => {
    expect(loginMessage(503, undefined, null)).toBe("The backend answered 503. Try again.");
  });
});

describe("unauthorized notifications", () => {
  it("reach every listener until it unsubscribes", () => {
    const first = vi.fn();
    const second = vi.fn();
    const stopFirst = onUnauthorized(first);
    onUnauthorized(second);
    notifyUnauthorized();
    stopFirst();
    notifyUnauthorized();
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(2);
  });
});
