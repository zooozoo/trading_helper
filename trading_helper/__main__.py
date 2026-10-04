import argparse
from dataclasses import asdict
from datetime import date, timedelta
import json
from pathlib import Path
import sys

from .rules import Bar, Costs, Event, Policy, plan_entry, signal_at_close
from .validate import format_text, validate_all
from . import opendart as od
from .opendart_normalize import normalize

DEFAULT_RAW = "data/raw/opendart"
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
        rows = od.search_supply_contract_filings(client, bgn, end, corp_cls=args.market)
        summary["supply_contract_filings"] = len(rows)
        if args.documents:
            fetched = skipped = 0
            for r in rows:
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
    col.add_argument("--documents", action="store_true", help="also download original documents (1 request each)")
    col.add_argument("--max-requests", type=int, default=2000)
    col.add_argument("--interval", type=float, default=0.25, help="seconds between requests")
    col.add_argument("--raw", default=DEFAULT_RAW)
    col.set_defaults(func=run_dart_collect)
    nrm = sub.add_parser("dart-normalize", help="build events/related/review CSVs from raw OpenDART store")
    nrm.add_argument("--raw", default=DEFAULT_RAW)
    nrm.add_argument("--out", default=DEFAULT_NORMALIZED)
    nrm.set_defaults(func=run_dart_normalize)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
