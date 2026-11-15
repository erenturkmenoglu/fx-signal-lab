import { useCallback, useEffect, useState } from "react";
import { App } from "./App";
import { auth, onUnauthorized } from "./auth";
import { Login } from "./components/Login";

type Gate = "checking" | "login" | "open";

/** Decides between the login form and the dashboard.  Without a password on the server it is always the dashboard. */
export function Root() {
  const [gate, setGate] = useState<Gate>("checking");
  const [loginRequired, setLoginRequired] = useState(false);

  useEffect(() => {
    let cancelled = false;
    auth
      .session()
      .then((session) => {
        if (cancelled) return;
        setLoginRequired(session.required);
        setGate(session.required && !session.authenticated ? "login" : "open");
      })
      // backend unreachable: the dashboard has its own "not reachable" notice and keeps retrying
      .catch(() => !cancelled && setGate("open"));
    return () => {
      cancelled = true;
    };
  }, []);

  // any 401 from the API means the session ended (expired, password changed, signed out elsewhere)
  useEffect(
    () =>
      onUnauthorized(() => {
        setLoginRequired(true);
        setGate("login");
      }),
    [],
  );

  const signOut = useCallback(() => {
    void auth.logout().then(() => setGate("login"));
  }, []);

  if (gate === "checking") return null;
  if (gate === "login") return <Login onDone={() => setGate("open")} />;
  return <App onSignOut={loginRequired ? signOut : undefined} />;
}
