import { useEffect, useState } from "react";
import { ConnectionLog } from "./components/ConnectionLog";
import { FeedHealth } from "./components/FeedHealth";
import { GapsPanel } from "./components/GapsPanel";
import { Header, MockBanner, type View } from "./components/Header";
import { HistoryPanel } from "./components/HistoryPanel";
import { MarketMonitor } from "./components/MarketMonitor";
import { type ChartSource, PriceChart } from "./components/PriceChart";
import { QuoteBoard } from "./components/QuoteBoard";
import { SecondsTape } from "./components/SecondsTape";
import { Board } from "./board/Board";
import { Lab } from "./lab/Lab";
import { useLive } from "./live";
import type { Job } from "./types";

const viewFromAddress = (): View =>
  window.location.hash === "#lab" ? "lab" : window.location.hash === "#board" ? "board" : "live";

export function App({ onSignOut }: { onSignOut?: () => void }) {
  const live = useLive();
  const { status, link } = live;
  // which page is shown lives in the address (#lab, #board), so a reload or a bookmark comes back to it
  const [view, setView] = useState<View>(viewFromAddress);
  useEffect(() => {
    const follow = () => setView(viewFromAddress());
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, []);
  const show = (next: View) => {
    window.history.replaceState(null, "", next === "live" ? window.location.pathname + window.location.search : `#${next}`);
    setView(next);
    window.scrollTo({ top: 0 });
  };
  // the lab and the board are kept once they have been opened: a result is still there after a look at another page
  const [opened, setOpened] = useState({ lab: false, board: false });
  useEffect(() => {
    if (view !== "live") setOpened((before) => (before[view] ? before : { ...before, [view]: true }));
  }, [view]);
  const [selected, setSelected] = useState<string | null>(null);
  const [source, setSource] = useState<ChartSource>("live");
  const [providerRange, setProviderRange] = useState<{ timeframe: string; start: number; end: number } | null>(null);

  const instruments = status?.instruments ?? [];
  useEffect(() => {
    if (!instruments.some((i) => i.symbol === selected)) setSelected(instruments[0]?.symbol ?? null);
  }, [instruments, selected]);
  const inst = instruments.find((i) => i.symbol === selected) ?? null;

  const showJob = (job: Job) => {
    setSelected(job.symbol);
    setProviderRange({ timeframe: job.timeframe, start: job.start_ms, end: job.end_ms });
    setSource("provider");
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  return (
    <div className="min-h-screen">
      <Header status={status?.startup_error ? null : status} link={link} onSignOut={onSignOut} view={view} onView={show} />
      {status?.provider?.synthetic ? <MockBanner /> : null}

      {!status ? (
        <Notice title={link === "closed" ? "The backend is not reachable" : "Connecting to the backend"}>
          {link === "closed"
            ? "The dashboard is retrying. Check that the application is running: docker compose ps, then docker compose logs app."
            : "Waiting for the first status message."}
        </Notice>
      ) : status.startup_error ? (
        <Notice title="The application could not start" tone="bad">
          <code className="num break-words">{status.startup_error}</code>
          <br />
          Fix the cause, then restart the application. Nothing is being collected in the meantime.
        </Notice>
      ) : (
        <>
          {opened.lab || view === "lab" ? (
            <div hidden={view !== "lab"}>
              <Lab symbol={inst?.symbol ?? null} />
            </div>
          ) : null}
          {opened.board || view === "board" ? (
            <div hidden={view !== "board"}>
              <Board symbol={inst?.symbol ?? null} />
            </div>
          ) : null}
        </>
      )}
      {status && !status.startup_error && view === "live" ? (
        <main className="mx-auto flex max-w-[1680px] flex-col gap-2 p-2">
          {status.provider.label === "NOT CONFIGURED" ? (
            <Notice title="No market data provider is configured" tone="bad" inline>
              Set <code className="num">MASSIVE_API_KEY</code> in the <code className="num">.env</code> file and restart. Until
              then nothing is collected and no prices are shown. For development without a subscription, set{" "}
              <code className="num">DATA_PROVIDER=mock</code> to run on clearly labelled synthetic data.
            </Notice>
          ) : null}
          {status.provider.label === "TEST ENDPOINT" ? (
            <Notice title="Connected to a test endpoint, not to Massive" inline>
              The feed host is <code className="num">{status.provider.feed_host}</code>. Prices from it are not market data.
            </Notice>
          ) : null}

          <MarketMonitor instruments={instruments} selected={selected} onSelect={setSelected} />

          {inst ? (
            <div className="grid grid-cols-1 gap-2 xl:grid-cols-[minmax(0,2fr)_minmax(340px,1fr)]">
              <div className="flex min-w-0 flex-col gap-2">
                <QuoteBoard inst={inst} now={status.server_time_ms} />
                <PriceChart
                  inst={inst}
                  live={live}
                  synthetic={status.provider.synthetic}
                  source={source}
                  onSourceChange={setSource}
                  providerRange={providerRange}
                />
                <SecondsTape observations={live.observations(inst.symbol)} now={status.server_time_ms} inst={inst} />
              </div>
              <div className="flex min-w-0 flex-col gap-2">
                <FeedHealth status={status} inst={inst} />
                <ConnectionLog events={live.events} />
              </div>
            </div>
          ) : (
            <Notice title="No active instruments" inline>
              Set <code className="num">active: true</code> for at least one pair in{" "}
              <code className="num">config/instruments.yaml</code> and restart.
            </Notice>
          )}

          <div className="grid grid-cols-1 gap-2 xl:grid-cols-2">
            <GapsPanel canFill={status.provider.rest_available} refreshKey={status.gaps.processed} />
            <HistoryPanel
              symbol={inst?.symbol ?? "EURUSD"}
              available={status.provider.rest_available && inst != null}
              unavailableReason={
                status.provider.synthetic
                  ? "Historical data comes from the provider's REST API. The mock provider has no history, so nothing can be downloaded in mock mode."
                  : "Historical data needs the Massive REST API. Configure an API key to download bars."
              }
              now={status.server_time_ms}
              onShow={showJob}
            />
          </div>

          <footer className="note px-1 py-2">
            Decision support only. FX Signal Lab places no orders and no figure on this page is a trading recommendation. All
            times are UTC.
          </footer>
        </main>
      ) : null}
    </div>
  );
}

function Notice({
  title,
  children,
  tone = "warn",
  inline = false,
}: {
  title: string;
  children: React.ReactNode;
  tone?: "warn" | "bad";
  inline?: boolean;
}) {
  return (
    <div
      role="alert"
      className={`panel px-4 py-3 ${inline ? "" : "mx-auto mt-10 max-w-[70ch]"}`}
      style={{ borderLeft: `3px solid var(--color-${tone === "bad" ? "bad" : "amber"})` }}
    >
      <h2 className="m-0 text-[15px] font-semibold">{title}</h2>
      <p className="m-0 mt-1 max-w-[80ch] text-[13px] text-ink-2">{children}</p>
    </div>
  );
}
