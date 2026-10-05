import argparse
from dataclasses import asdict
from datetime import date, timedelta
import json
from pathlib import Path
import sys

from .rules import Bar, Costs, Event, Policy, plan_entry, signal_at_close
from .validate import format_text, validate_all
from . import opendart as od
from . import datagokr as dg
from . import weekly_v1 as wv
from . import diagnostics as dx
from . import screen as sc
from . import fundamentals as fd
from .market import load_market
from .opendart_normalize import normalize

DEFAULT_RAW = "data/raw/opendart"
DEFAULT_PRICE_RAW = "data/raw/datagokr"
DEFAULT_NORMALIZED = "data/normalized"


def run_demo(args):
    policy = Policy.load(Path(args.config))
    # Fictional continuous session dates; deliberately NOT a KR trading calendar.
    start = date(2024, 1, 1)
    bars = [Bar(start + timedelta(days=i), 10000, 10100, 9900, 10000, 200000)
            for i in range(20)]
    reaction = Bar(start + timedelta(days=20), 10100, 10500, 10000, 10400, 400000)
    bars.append(reaction)
    event = Event("SYNTHETIC-001", "DEMO", bars[-2].date, "new_contract",
                  10_000_000_000, 100_000_000_000, bars[0].date,
                  True, bars[0].date, "synthetic://not-a-real-filing")
    signal = signal_at_close(event, bars, policy)
    assert signal is not None
    next_bar = Bar(reaction.date + timedelta(days=1), 10400, 10600, 10200, 10500, 300000)
    # Zero costs ONLY for synthetic arithmetic demonstration; NOT live tax/fee assumptions.
    plan = plan_entry(signal, next_bar, expected_session=next_bar.date,
                      equity=10_000_000, available_cash=10_000_000, open_positions=0,
                      policy=policy, costs=Costs(0, 0, 0, 0))
    print(json.dumps({"data_kind": "SYNTHETIC_NOT_INVESTMENT_ADVICE",
                      "profitability_validated": False, "costs": "zero_demo_only",
                      "signal": asdict(signal), "plan": asdict(plan) if plan else None},
                     ensure_ascii=False, indent=2, default=str))


def run_validate(args):
    if args.prices is None and args.events is None and args.trades is None:
        sys.exit("validate: give --prices, --events and/or --trades")
    result = validate_all(args.prices, args.events, args.trades)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_text(result))
    sys.exit(0 if result["ok"] else 1)


def run_dart_collect(args):
    """Fetch 단일판매ㆍ공급계약 filing lists (and optionally documents) into the raw store.

    Resumable: documents already present are skipped. --max-requests caps this run so the
    OpenDART daily quota (20,000/day for personal keys) is never exceeded silently.
    """
    try:
        key = od.get_api_key()
    except od.OpenDartError as exc:
        sys.exit(f"dart-collect: {exc}")
    store = od.RawStore(Path(args.raw))
    client = od.Client(key, store, min_interval_s=args.interval)
    bgn, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    requests_made = 0

    def count(**_):
        nonlocal requests_made
        requests_made += 1
        if requests_made > args.max_requests:
            raise od.OpenDartError(f"--max-requests {args.max_requests} reached; rerun to resume")

    inner = client.fetcher

    def counting_fetcher(url, params):
        count()
        return inner(url, params)

    client.fetcher = counting_fetcher
    summary = {"start": args.start, "end": args.end, "corp_cls": args.market, "raw": str(store.root)}
    try:
        if args.pblntf_ty:
            rows = od.search_filings(client, bgn, end, pblntf_ty=args.pblntf_ty, pblntf_detail_ty=args.detail_ty,
                                     corp_cls=args.market, last_reprt_at="N")
            summary["list_rows"] = len(rows)
            summary["filter"] = {"pblntf_ty": args.pblntf_ty, "pblntf_detail_ty": args.detail_ty}
            if args.documents:
                raise od.OpenDartError("--documents is only supported for the supply-contract default filter")
        else:
            rows = od.search_supply_contract_filings(client, bgn, end, corp_cls=args.market)
            summary["supply_contract_filings"] = len(rows)
        if args.documents:
            fetched = skipped = 0
            wanted = [r for r in rows if args.documents_all
                      or od.classify_report_name(r["report_nm"], r.get("rm", "")).kind == "new_contract"]
            summary["documents_wanted"] = len(wanted)
            for r in wanted:
                rc = r["rcept_no"]
                if (store.root / "document" / f"{rc}.zip").exists():
                    skipped += 1
                    continue
                od.fetch_document(client, rc)
                fetched += 1
            summary.update(documents_fetched=fetched, documents_skipped_existing=skipped)
    except od.OpenDartError as exc:
        summary["stopped"] = od.mask(str(exc), key)
    summary["requests_made"] = requests_made
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    sys.exit(1 if "stopped" in summary else 0)


def run_dart_normalize(args):
    store = od.RawStore(Path(args.raw))
    summary = normalize(store, Path(args.out))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def run_prices_collect(args):
    try:
        key = dg.get_api_key()
    except dg.DataGoKrError as exc:
        sys.exit(f"prices-collect: {exc}")
    store = od.RawStore(Path(args.raw))
    client = dg.Client(key, store, min_interval_s=args.interval)
    try:
        summary = dg.collect(client, date.fromisoformat(args.start), date.fromisoformat(args.end),
                             max_requests=args.max_requests)
    except dg.DataGoKrError as exc:
        summary = {"stopped": dg.mask(str(exc), key)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    sys.exit(1 if "stopped" in summary else 0)


def run_prices_normalize(args):
    summary = dg.normalize(od.RawStore(Path(args.raw)), Path(args.out))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def run_backtest_weekly(args):
    import hashlib
    import platform
    import subprocess
    segments = json.loads(Path(args.segments).read_text(encoding="utf-8"))
    seg = segments[args.segment]
    segment = (date.fromisoformat(seg["start"]), date.fromisoformat(seg["end"]))
    overrides = {"signal_variant": args.variant}
    if args.baseline == "events_only":
        overrides.update(require_breakout=False, require_volume=False)
    policy = wv.WeeklyPolicy.load(args.config, **overrides)
    costs = wv.CostTable.load(args.costs)
    market = load_market(args.normalized)
    events = wv.load_events(args.events)
    signals, funnel = wv.generate_signals(market, events, policy, segment=segment, risk_filter=args.risk_filter)
    result = wv.run_portfolio(market, signals, policy, costs, equity0=args.equity, funnel=funnel)
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        commit = "unknown"
    sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]
    result.labels = {
        "experiment": args.exp, "strategy": policy.strategy_id, "variant": policy.signal_variant, "baseline": args.baseline,
        "segment": args.segment, "segment_range": [seg["start"], seg["end"]],
        "risk_filter": args.risk_filter,
        "RISK_FILTER_NOT_APPLIED": args.risk_filter == "none",
        "data_kind": "REAL_DATA_HYPOTHESIS_STAGE_NOT_VALIDATED",
        "costs_status": costs.status, "equity0": args.equity,
        "git_commit": commit, "python": platform.python_version(),
        "hashes": {"events": sha(args.events), "prices": sha(Path(args.normalized) / "prices.csv"),
                   "config": sha(args.config), "costs": sha(args.costs), "segments": sha(args.segments)},
    }
    summary = wv.summarize(result)
    out_dir = Path(args.out) / args.exp
    wv.write_outputs(result, summary, out_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"outputs: {out_dir}", file=sys.stderr)


def run_diagnose_events(args):
    import subprocess
    segments = json.loads(Path(args.segments).read_text(encoding="utf-8"))
    seg = segments[args.segment]
    segment = (date.fromisoformat(seg["start"]), date.fromisoformat(seg["end"]))
    market = load_market(args.normalized)
    events = [e for e in wv.load_events(args.events) if e.event_type == "new_contract"]
    proxy = dx.market_proxy(market, statistic=args.proxy)
    # market caps on the base session (last close before reaction) for events in range
    need = set()
    for ev in events:
        if ev.receipt_date < market.sessions[0]:
            continue
        r = market.next_session_after(ev.receipt_date)
        if r is None or not segment[0] <= market.sessions[r] <= segment[1]:
            continue
        d_idx = market.index.get(ev.receipt_date)
        base = (d_idx if d_idx is not None else r) - 1
        if base >= 0:
            need.add(market.sessions[base])
    caps = dx.load_market_caps(Path(args.price_raw), need)
    rows, funnel = dx.event_table(market, events, segment=segment, proxy=proxy, caps=caps,
                                  min_turnover=args.min_turnover)
    out_dir = Path(args.out) / args.exp
    out_dir.mkdir(parents=True, exist_ok=True)
    dx.write_table(rows, out_dir / "event_table.csv")
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        commit = "unknown"
    liquid_rows = [r for r in rows if r["liquid"]]
    placebo = dx.placebo_rows(market, liquid_rows, proxy, min_turnover=args.min_turnover)
    dx.write_table(placebo, out_dir / "placebo_table.csv")
    report = {"labels": {"experiment": args.exp, "segment": args.segment, "segment_range": [seg["start"], seg["end"]],
                         "risk_filter": "none", "market_proxy": f"equal_weight_{args.proxy}_daily_return_common_stocks",
                         "placebo": "one random liquid common stock per event on the same reaction date (seed 7)",
                         "data_kind": "REAL_DATA_DIAGNOSTIC_NO_PARAMETER_SEARCH", "git_commit": commit,
                         "min_turnover_krw": args.min_turnover, "cap_dates_loaded": len(need), "caps_found": len(caps)},
              "funnel": funnel,
              "liquid": dx.summarize(rows, liquid_only=True),
              "placebo_liquid": dx.summarize_placebo(placebo),
              "all": dx.summarize(rows, liquid_only=False)}
    (out_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"funnel": funnel, "n_liquid": report["liquid"]["n_events"], "n_all": report["all"]["n_events"]},
                     ensure_ascii=False, indent=2))
    print(f"outputs: {out_dir}", file=sys.stderr)


def run_screen_events(args):
    import subprocess
    segments = json.loads(Path(args.segments).read_text(encoding="utf-8"))
    seg = segments[args.segment]
    segment = (date.fromisoformat(seg["start"]), date.fromisoformat(seg["end"]))
    types = sc.load_type_config(args.types)
    if args.only:
        types = {k: v for k, v in types.items() if k in set(args.only.split(","))}
    store = od.RawStore(Path(args.raw))
    events_by_type, ext_stats = sc.extract_events(store, types, min_date=date(2019, 12, 1))
    market = load_market(args.normalized)
    proxy = dx.market_proxy(market, statistic="mean")
    report = sc.screen(market, events_by_type, segment=segment, proxy=proxy, min_turnover=args.min_turnover)
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        commit = "unknown"
    out_dir = Path(args.out) / args.exp
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"labels": {"experiment": args.exp, "segment": args.segment, "segment_range": [seg["start"], seg["end"]],
                          "types_config": args.types, "git_commit": commit, "min_turnover_krw": args.min_turnover,
                          "data_kind": "REAL_DATA_SCREEN_NO_PARAMETER_SEARCH", "risk_filter": "none"},
               "extraction": ext_stats, "report": report}
    (out_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    table = sc.markdown_table(report, types)
    (out_dir / "table.md").write_text(table + "\n", encoding="utf-8")
    print(table)
    print(f"outputs: {out_dir}", file=sys.stderr)


def run_fundamentals_collect(args):
    import csv as _csv
    try:
        key = od.get_api_key()
    except od.OpenDartError as exc:
        sys.exit(f"fundamentals-collect: {exc}")
    store = od.RawStore(Path(args.raw))
    client = od.Client(key, store, min_interval_s=args.interval)
    with open(Path(args.normalized) / "securities.csv", encoding="utf-8", newline="") as fh:
        symbols = {r["symbol"] for r in _csv.DictReader(fh) if r["is_common_stock"] == "true"}
    mapping = fd.corp_map(client, store, symbols)
    years = list(range(args.start_year, args.end_year + 1))
    try:
        summary = fd.collect(client, store, sorted(mapping.values()), years, max_requests=args.max_requests)
    except od.OpenDartError as exc:
        summary = {"stopped": od.mask(str(exc), key)}
    summary.update(symbols_common=len(symbols), symbols_mapped=len(mapping))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    sys.exit(1 if "stopped" in summary else 0)


def run_fundamentals_normalize(args):
    store = od.RawStore(Path(args.raw))
    counts = fd.normalize(store, Path(args.out))
    print(json.dumps(counts, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(prog="trading_helper", description="Unvalidated research starter")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="synthetic single-trade arithmetic; not real prices")
    demo.add_argument("--config", default="config/strategy_v0.json")
    demo.set_defaults(func=run_demo)
    val = sub.add_parser("validate", help="check CSV files against docs/DATA_CONTRACT.md")
    val.add_argument("--prices")
    val.add_argument("--events")
    val.add_argument("--trades", help="broker fill records; see docs/LIVE_RECONCILIATION.md")
    val.add_argument("--json", action="store_true")
    val.set_defaults(func=run_validate)
    col = sub.add_parser("dart-collect", help="fetch OpenDART supply-contract filings into data/raw (key from env)")
    col.add_argument("--start", required=True, help="YYYY-MM-DD")
    col.add_argument("--end", required=True, help="YYYY-MM-DD")
    col.add_argument("--market", choices=["Y", "K"], default=None, help="Y=KOSPI, K=KOSDAQ; default both")
    col.add_argument("--documents", action="store_true",
                     help="also download original documents for new_contract filings (1 request each)")
    col.add_argument("--documents-all", action="store_true",
                     help="with --documents: also fetch amendment/cancellation documents")
    col.add_argument("--max-requests", type=int, default=2000)
    col.add_argument("--interval", type=float, default=0.25, help="seconds between requests")
    col.add_argument("--raw", default=DEFAULT_RAW)
    col.add_argument("--pblntf-ty", default=None, help="collect raw lists of another type instead (e.g. B)")
    col.add_argument("--detail-ty", default=None, help="with --pblntf-ty, e.g. B001 주요사항보고서")
    col.set_defaults(func=run_dart_collect)
    nrm = sub.add_parser("dart-normalize", help="build events/related/review CSVs from raw OpenDART store")
    nrm.add_argument("--raw", default=DEFAULT_RAW)
    nrm.add_argument("--out", default=DEFAULT_NORMALIZED)
    nrm.set_defaults(func=run_dart_normalize)
    pc = sub.add_parser("prices-collect", help="fetch daily whole-market prices from data.go.kr (key from env)")
    pc.add_argument("--start", required=True, help="YYYY-MM-DD (coverage starts 2020-01-02)")
    pc.add_argument("--end", required=True, help="YYYY-MM-DD")
    pc.add_argument("--max-requests", type=int, default=2000, help="daily quota is 10,000 for a dev key")
    pc.add_argument("--interval", type=float, default=0.2)
    pc.add_argument("--raw", default=DEFAULT_PRICE_RAW)
    pc.set_defaults(func=run_prices_collect)
    pn = sub.add_parser("prices-normalize", help="build prices/securities/calendar/halts CSVs from raw daily files")
    pn.add_argument("--raw", default=DEFAULT_PRICE_RAW)
    pn.add_argument("--out", default=DEFAULT_NORMALIZED)
    pn.set_defaults(func=run_prices_normalize)
    bt = sub.add_parser("backtest-weekly", help="weekly_execution_v1 backtest on normalized real data (hypothesis stage)")
    bt.add_argument("--exp", required=True, help="experiment id, e.g. W1-A-01")
    bt.add_argument("--segment", choices=["development", "validation", "final"], default="development")
    bt.add_argument("--risk-filter", choices=list(wv.RISK_FILTER_MODES), default="approved_only",
                    help="'none' runs without the financial-risk filter and is labeled as such")
    bt.add_argument("--baseline", choices=["none", "events_only"], default="none")
    bt.add_argument("--variant", choices=["A", "B"], default="A",
                    help="A: reaction day must be week end (strict); B: breakout judged at week end")
    bt.add_argument("--equity", type=float, default=10_000_000)
    bt.add_argument("--config", default="config/strategy_weekly_v1.json")
    bt.add_argument("--costs", default="config/costs_kr_assumed.json")
    bt.add_argument("--segments", default="config/segments_v1.json")
    bt.add_argument("--events", default="data/normalized/opendart_events.csv")
    bt.add_argument("--normalized", default="data/normalized")
    bt.add_argument("--out", default="outputs/experiments")
    bt.set_defaults(func=run_backtest_weekly)
    dg_ = sub.add_parser("diagnose-events", help="parameter-free event study on the development segment")
    dg_.add_argument("--exp", required=True, help="e.g. DIAG-01")
    dg_.add_argument("--segment", choices=["development"], default="development")
    dg_.add_argument("--min-turnover", type=float, default=1_000_000_000)
    dg_.add_argument("--proxy", choices=["mean", "median"], default="mean")
    dg_.add_argument("--segments", default="config/segments_v1.json")
    dg_.add_argument("--events", default="data/normalized/opendart_events.csv")
    dg_.add_argument("--normalized", default="data/normalized")
    dg_.add_argument("--price-raw", default="data/raw/datagokr")
    dg_.add_argument("--out", default="outputs/experiments")
    dg_.set_defaults(func=run_diagnose_events)
    scn = sub.add_parser("screen-events", help="multi-type title-based event screen (development segment)")
    scn.add_argument("--exp", required=True, help="e.g. SCREEN-01")
    scn.add_argument("--types", default="config/event_types_screen_v1.json")
    scn.add_argument("--only", default=None, help="comma-separated subset of type names")
    scn.add_argument("--segment", choices=["development"], default="development")
    scn.add_argument("--min-turnover", type=float, default=1_000_000_000)
    scn.add_argument("--segments", default="config/segments_v1.json")
    scn.add_argument("--raw", default=DEFAULT_RAW)
    scn.add_argument("--normalized", default=DEFAULT_NORMALIZED)
    scn.add_argument("--out", default="outputs/experiments")
    scn.set_defaults(func=run_screen_events)
    fc = sub.add_parser("fundamentals-collect", help="OpenDART key accounts (fnlttMultiAcnt) for all common stocks")
    fc.add_argument("--start-year", type=int, default=2019)
    fc.add_argument("--end-year", type=int, default=2026)
    fc.add_argument("--max-requests", type=int, default=3000)
    fc.add_argument("--interval", type=float, default=0.3)
    fc.add_argument("--raw", default=DEFAULT_RAW)
    fc.add_argument("--normalized", default=DEFAULT_NORMALIZED)
    fc.set_defaults(func=run_fundamentals_collect)
    fn = sub.add_parser("fundamentals-normalize", help="build fundamentals.csv from raw key-account files")
    fn.add_argument("--raw", default=DEFAULT_RAW)
    fn.add_argument("--out", default=str(Path(DEFAULT_NORMALIZED) / "fundamentals.csv"))
    fn.set_defaults(func=run_fundamentals_normalize)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
