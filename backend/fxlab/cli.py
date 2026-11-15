"""Command line interface.

::

    fxlab serve                      run the API + ingestion service (one process, one worker)
    fxlab migrate                    apply database migrations
    fxlab verify-provider            probe what the configured Massive key can really do
    fxlab instruments                show the instrument registry
    fxlab history --symbol EURUSD --timeframe 1m --start 2026-01-01 --end 2026-11-14
    fxlab strategies                 the strategies a backtest can run, with their parameters
    fxlab backtest --strategy rsi_reversion --start 2026-01-01 --end 2026-11-14
    fxlab gaps list | scan | fill --id N
    fxlab reconcile --minutes 10     compare live ticks with the provider's quote history
    fxlab hash-password              turn a dashboard password into the hash for DASHBOARD_PASSWORD_HASH
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from fxlab.config import Settings, get_settings
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.logging import configure_logging, register_secret
from fxlab.timeutil import iso, parse_time


def _registry(settings: Settings) -> InstrumentRegistry:
    return InstrumentRegistry.from_yaml(settings.instruments_file)


def cmd_serve(settings: Settings, _args: argparse.Namespace) -> int:
    import uvicorn

    configure_logging(settings.log_level, settings.log_format)  # before uvicorn logs anything
    # exactly one worker: the provider allows a single WebSocket connection per account
    uvicorn.run(
        "fxlab.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        workers=1,
        log_config=None,
        timeout_graceful_shutdown=25,
    )
    return 0


def cmd_migrate(settings: Settings, _args: argparse.Namespace) -> int:
    from fxlab.storage.db import upgrade_database_sync

    upgrade_database_sync(settings.database_url)
    print("database is at the latest revision")
    return 0


def cmd_instruments(settings: Settings, _args: argparse.Namespace) -> int:
    print(f"{'SYMBOL':8} {'ACTIVE':6} {'PIP':>8} {'PREC':>4} {'STALE ms':>9} {'MAX SPREAD':>10}  DESCRIPTION")
    for inst in _registry(settings).all():
        dq = inst.data_quality
        print(
            f"{inst.symbol:8} {'yes' if inst.active else 'no':6} {inst.pip_size:>8g} {inst.price_precision:>4} "
            f"{dq.stale_after_ms:>9} {dq.max_spread_pips:>10g}  {inst.description}"
        )
    return 0


def cmd_verify_provider(settings: Settings, args: argparse.Namespace) -> int:
    from fxlab.services.provider_check import format_report, verify_provider

    registry = _registry(settings)

    async def run() -> dict[str, Any]:
        report = await verify_provider(settings, registry, ws_seconds=args.seconds, include_ws=not args.no_ws)
        if not args.no_db and report.get("instruments"):
            report["database_updated"] = await _store_instrument_status(settings, registry, report["instruments"])
        return report

    report = asyncio.run(run())
    print(json.dumps(report, indent=2, default=str) if args.json else format_report(report))
    checks = {c["name"]: c["status"] for c in report.get("checks", [])}
    return 0 if checks.get("rest_auth_market_status") == "ok" and not report.get("error") else 1


async def _store_instrument_status(
    settings: Settings, registry: InstrumentRegistry, instruments: list[dict[str, Any]]
) -> bool:
    from fxlab.storage import repo
    from fxlab.storage.db import make_engine

    engine = make_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            await repo.sync_instruments(conn, registry)
            for entry in instruments:
                await repo.set_provider_status(
                    conn,
                    entry["symbol"],
                    entry["status"],
                    {"ticker": entry["ticker"], "by": "verify-provider"},
                )
        return True
    except Exception as exc:
        print(
            f"note: could not store instrument availability in the database ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return False
    finally:
        await engine.dispose()


def cmd_history(settings: Settings, args: argparse.Namespace) -> int:
    from fxlab.clock import SystemClock
    from fxlab.marketdata.massive.rest import MassiveRestClient
    from fxlab.services.history import HistoryService
    from fxlab.storage import repo
    from fxlab.storage.db import make_engine

    if not settings.api_key:
        print("MASSIVE_API_KEY is not set", file=sys.stderr)
        return 2

    async def run() -> dict[str, Any]:
        registry = _registry(settings)
        engine = make_engine(settings.database_url)
        rest = MassiveRestClient(
            base_url=settings.massive_rest_url,
            api_key=settings.api_key or "",
            max_calls_per_min=settings.massive_rest_max_calls_per_min,
            timeout_s=settings.massive_rest_timeout_s,
            max_retries=settings.massive_rest_max_retries,
        )
        try:
            async with engine.begin() as conn:
                ids = await repo.sync_instruments(conn, registry)
            history = HistoryService(
                engine=engine, registry=registry, rest=rest, instrument_ids=ids, clock=SystemClock()
            )
            symbol, timeframe = args.symbol.upper(), args.timeframe
            start_ms, end_ms = parse_time(args.start), parse_time(args.end)
            history.validate(symbol, timeframe, start_ms, min(end_ms, start_ms + 1))  # everything but the length
            if end_ms <= start_ms:
                raise ValueError("end must be after start")
            # one job covers a limited range (a guard for the dashboard): a longer one becomes several
            step = history.max_range_ms(timeframe)
            jobs = []
            for chunk_start in range(start_ms, end_ms, step):
                job_id = await history.create_job(symbol, timeframe, chunk_start, min(chunk_start + step, end_ms))
                jobs.append(await history.run_job(job_id))
                if jobs[-1]["status"] != "done":
                    break
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "start": iso(start_ms),
                "end": iso(end_ms),
                "status": "done" if all(j["status"] == "done" for j in jobs) else "failed",
                "rows_written": sum(j["rows_written"] for j in jobs),
                "jobs": jobs,
            }
        finally:
            await rest.aclose()
            await engine.dispose()

    try:
        result = asyncio.run(run())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "done" else 1


def cmd_strategies(_settings: Settings, args: argparse.Namespace) -> int:
    from fxlab.research.strategies import STRATEGIES

    if args.json:
        print(json.dumps([s.describe() for s in STRATEGIES.values()], indent=2))
        return 0
    for strategy in STRATEGIES.values():
        print(f"{strategy.key}  ({strategy.title}; usually on {strategy.timeframe} bars)")
        print(f"    {strategy.summary}")
        for p in strategy.params:
            limits = f"one of {', '.join(p.choices)}" if p.choices else f"{p.minimum:g} to {p.maximum:g}"
            print(f"    -p {p.name}={p.default!s:8} {p.label} ({limits})" + (f" - {p.help}" if p.help else ""))
        print()
    return 0


def _pairs(items: list[str] | None, what: str) -> dict[str, str]:
    """``name=value`` arguments as a dictionary."""
    out: dict[str, str] = {}
    for item in items or []:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"{what} are given as name=value, not {item!r}")
        out[name.strip()] = value.strip()
    return out


def _read_request(source: str) -> dict[str, Any]:
    """A backtest request as JSON, from a file or from standard input (``-``)."""
    from pathlib import Path

    from fxlab.research.report import BacktestError

    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        data = json.loads(text)
    except OSError as exc:
        raise BacktestError(f"cannot read the request: {exc}") from None
    except json.JSONDecodeError as exc:
        raise BacktestError(f"the request is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise BacktestError("the request must be a JSON object")
    return data


def cmd_backtest(settings: Settings, args: argparse.Namespace) -> int:
    from fxlab.research.report import BacktestError, BacktestRequest, format_report
    from fxlab.services.backtest import history_inventory, run_backtest
    from fxlab.storage.db import make_engine

    registry = _registry(settings)
    as_json = args.json or args.request is not None

    async def run(given: dict[str, Any] | None) -> dict[str, Any]:
        engine = make_engine(settings.database_url)
        try:
            data = given
            if data is None:
                if not args.strategy:
                    raise BacktestError("--strategy is required (see: fxlab strategies)")
                active = registry.active()
                symbol = (args.symbol or (active[0].symbol if active else "")).upper()
                start, end = args.start, args.end
                if start is None or end is None:  # no period given: everything that is stored
                    stored = {h["timeframe"]: h for h in await history_inventory(engine, symbol)}.get(args.base)
                    if stored is None:
                        raise BacktestError(
                            f"no {args.base} history of {symbol} is stored. Download it first: "
                            f"fxlab history --symbol {symbol} --timeframe {args.base} --start ... --end ..."
                        )
                    start = start or stored["first"]
                    end = end or parse_time(stored["last"]) + 60_000
                data = {
                    "symbol": symbol,
                    "strategy": args.strategy,
                    "start": start,
                    "end": end,
                    "timeframe": args.timeframe,
                    "base_timeframe": args.base,
                    "params": _pairs(args.param, "parameters"),
                    "execution": _pairs(args.set, "settings"),
                    "split": args.split,
                }
            return await run_backtest(engine, registry, BacktestRequest.from_mapping(data))
        finally:
            await engine.dispose()

    try:
        report = asyncio.run(run(_read_request(args.request) if args.request is not None else None))
    except ValueError as exc:  # BacktestError included: the request cannot be run as asked
        if as_json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report) if as_json else format_report(report, trades=args.trades, days=args.days))
    return 0


def cmd_reconcile(settings: Settings, args: argparse.Namespace) -> int:
    from fxlab.clock import SystemClock
    from fxlab.services.reconcile import format_reconciliation, reconcile
    from fxlab.storage.db import make_engine

    registry = _registry(settings)
    active = registry.active()
    symbol = (args.symbol or (active[0].symbol if active else "")).upper()

    async def run() -> dict[str, Any]:
        engine = make_engine(settings.database_url)
        try:
            return await reconcile(
                settings,
                registry,
                engine,
                symbol=symbol,
                now_ms=SystemClock().now_ms(),
                minutes=min(max(args.minutes, 1), 240),
                end_ms=parse_time(args.end) if args.end else None,
            )
        finally:
            await engine.dispose()

    try:
        report = asyncio.run(run())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else format_reconciliation(report))
    return 1 if report.get("error") else 0


def cmd_gaps(settings: Settings, args: argparse.Namespace) -> int:
    import httpx

    if args.action == "list":
        from fxlab.storage import repo
        from fxlab.storage.db import make_engine

        async def run() -> list[dict[str, Any]]:
            engine = make_engine(settings.database_url)
            try:
                async with engine.connect() as conn:
                    return await repo.list_gaps(conn, status=args.status)
            finally:
                await engine.dispose()

        rows = asyncio.run(run())
        print(f"{'ID':>5} {'SYMBOL':8} {'KIND':12} {'STATUS':17} {'SECONDS':>8}  START (UTC)               DETAIL")
        for g in rows:
            detail = json.dumps(g["detail"])[:120]
            print(
                f"{g['id']:>5} {g['symbol']:8} {g['kind']:12} {g['status']:17} {g['seconds']:>8}  "
                f"{iso(g['start_ms'])}  {detail}"
            )
        return 0

    # scan / fill act on the running application so that live bars are rebuilt consistently
    base = args.api.rstrip("/")
    headers = {}
    if settings.password_hash:  # the application needs a login: this process holds the same secret
        from fxlab.api.auth import Auth

        headers["Authorization"] = f"Bearer {Auth(settings.password_hash).issue(ttl_s=120)}"
    try:
        if args.action == "scan":
            resp = httpx.post(f"{base}/api/gaps/scan", timeout=60, headers=headers)
        else:
            if args.id is None:
                print("--id is required for 'fill'", file=sys.stderr)
                return 2
            resp = httpx.post(f"{base}/api/gaps/{args.id}/fill", timeout=60, headers=headers)
    except httpx.HTTPError as exc:
        print(f"could not reach the running application at {base}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(resp.json(), indent=2))
    return 0 if resp.status_code < 400 else 1


def cmd_hash_password(_settings: Settings, args: argparse.Namespace) -> int:
    import getpass

    from fxlab.api.auth import check_new_password, hash_password

    if args.stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Dashboard password: ")
        if getpass.getpass("Again: ") != password:
            print("the two entries differ", file=sys.stderr)
            return 2
    try:
        check_new_password(password)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(hash_password(password))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fxlab", description="FX Signal Lab command line")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the API and the ingestion service").set_defaults(func=cmd_serve)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("instruments", help="show the instrument registry").set_defaults(func=cmd_instruments)

    verify = sub.add_parser("verify-provider", help="probe the Massive API with the configured key")
    verify.add_argument("--seconds", type=float, default=20.0, help="how long to listen on the WebSocket")
    verify.add_argument("--no-ws", action="store_true", help="skip the WebSocket probe (e.g. while the app is running)")
    verify.add_argument("--no-db", action="store_true", help="do not store instrument availability in the database")
    verify.add_argument("--json", action="store_true", help="print the raw report as JSON")
    verify.set_defaults(func=cmd_verify_provider)

    history = sub.add_parser("history", help="download provider aggregate bars into the database")
    history.add_argument("--symbol", required=True)
    history.add_argument("--timeframe", default="1m")
    history.add_argument("--start", required=True, help="ISO-8601 or epoch")
    history.add_argument("--end", required=True, help="ISO-8601 or epoch")
    history.set_defaults(func=cmd_history)

    strategies = sub.add_parser("strategies", help="list the strategies a backtest can run, with their parameters")
    strategies.add_argument("--json", action="store_true")
    strategies.set_defaults(func=cmd_strategies)

    bt = sub.add_parser(
        "backtest",
        help="run a strategy on the stored history (read-only)",
        description="Run a strategy on the provider's historical bars stored in the database. Reads only.",
    )
    bt.add_argument("--strategy", help="see: fxlab strategies")
    bt.add_argument("--symbol", help="default: the first active instrument")
    bt.add_argument("--start", help="ISO-8601 or epoch (default: the start of the stored history)")
    bt.add_argument("--end", help="ISO-8601 or epoch (default: the end of the stored history)")
    bt.add_argument("--timeframe", help="bars the strategy reads, e.g. 1m 5m 15m (default: the strategy's usual one)")
    bt.add_argument("--base", default="1m", help="stored bars the trades are simulated on (default 1m)")
    bt.add_argument("-p", "--param", action="append", metavar="NAME=VALUE", help="a strategy parameter (repeatable)")
    bt.add_argument(
        "-x",
        "--set",
        action="append",
        metavar="NAME=VALUE",
        help="a trading assumption, e.g. max_clips=1 spread_pips=0.5 target_pips=5 stop_pips=10 (repeatable)",
    )
    bt.add_argument(
        "--split", default="auto", help="first out-of-sample day YYYY-MM-DD, 'auto' (the last 3 days in 10) or 'none'"
    )
    bt.add_argument("--trades", type=int, default=0, metavar="N", help="also list the last N trades")
    bt.add_argument("--days", type=int, default=0, metavar="N", help="also list the last N days")
    bt.add_argument("--json", action="store_true", help="print the whole report as JSON")
    bt.add_argument("--request", metavar="FILE", help="read the request as JSON from FILE ('-' = standard input)")
    bt.set_defaults(func=cmd_backtest)

    rec = sub.add_parser("reconcile", help="compare live ticks with the provider's quote history (read-only)")
    rec.add_argument("--symbol", help="default: the first active instrument")
    rec.add_argument("--minutes", type=int, default=10, help="length of the window (1 to 240)")
    rec.add_argument("--end", help="end of the window, ISO-8601 or epoch (default: the last settled minute)")
    rec.add_argument("--json", action="store_true", help="print the raw report as JSON")
    rec.set_defaults(func=cmd_reconcile)

    hp = sub.add_parser("hash-password", help="make the value for DASHBOARD_PASSWORD_HASH (asks for the password)")
    hp.add_argument("--stdin", action="store_true", help="read the password from standard input (one line)")
    hp.set_defaults(func=cmd_hash_password)

    gaps = sub.add_parser("gaps", help="inspect and repair data gaps")
    gaps.add_argument("action", choices=["list", "scan", "fill"])
    gaps.add_argument("--id", type=int, help="gap id (fill)")
    gaps.add_argument("--status", help="filter (list): open|filled|no_provider_data|unfillable|failed")
    gaps.add_argument("--api", default="http://127.0.0.1:8000", help="base URL of the running application")
    gaps.set_defaults(func=cmd_gaps)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.command != "serve":  # the server configures logging itself
        configure_logging("WARNING", "console")
    register_secret(settings.api_key)
    return int(args.func(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
