"""Title-based event extraction from raw OpenDART lists and a multi-type diagnostic screen.

Reuses diagnostics.event_table/placebo with date-only events (no contract figures). One event per
(symbol, type, receipt date); amended filings excluded. Pre-registered types live in
config/event_types_screen_v1.json. Development segment only. No parameter search.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean

from . import diagnostics as dx
from .market import Market
from .opendart import AMEND_PREFIX, RawStore
from .opendart_normalize import load_filings
from .weekly_v1 import EventRow

PAREN = re.compile(r"\((.*)\)\s*$")


def clean_title(report_nm: str) -> tuple[str, str | None, str]:
    """(core title without prefix/parenthetical, parenthetical text, prefix tag or '')."""
    name = re.sub(r"\s+", " ", report_nm or "").strip()
    m = AMEND_PREFIX.match(name)
    tag = m.group(1) if m else ""
    core = AMEND_PREFIX.sub("", name).strip()
    paren = None
    pm = PAREN.search(core)
    if pm and not core.startswith("주요사항보고서"):
        paren = pm.group(1)
        core = core[:pm.start()].strip()
    return core, paren, tag


def load_type_config(path: str | Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for name, spec in raw["types"].items():
        out[name] = {"title": re.compile(spec["title"]), "paren": re.compile(spec["paren"]) if spec.get("paren") else None,
                     "expected": spec.get("expected", "unknown"), "source": spec.get("source", "")}
    return out


def extract_events(store: RawStore, types: dict, *, min_date: date | None = None) -> tuple[dict[str, list[EventRow]], dict]:
    """Map type name -> EventRow list (contract fields unused). Amendments and duplicates dropped."""
    rows = load_filings(store)
    seen = set()
    out: dict[str, list[EventRow]] = defaultdict(list)
    stats = defaultdict(int)
    for r in rows:
        d = r.get("rcept_dt", "")
        if len(d) != 8:
            continue
        rd = date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        if min_date and rd < min_date:
            continue
        core, paren, tag = clean_title(r.get("report_nm", ""))
        if tag:
            stats["amended_skipped"] += 1
            continue
        sym = (r.get("stock_code") or "").strip()
        if not sym:
            continue
        for name, spec in types.items():
            if not spec["title"].search(core):
                continue
            if spec["paren"] is not None and not (paren and spec["paren"].search(paren)):
                continue
            key = (name, sym, rd)
            if key in seen:
                stats["duplicate_same_day"] += 1
                continue
            seen.add(key)
            out[name].append(EventRow(r["rcept_no"], sym, rd, name, 0.0, 0.0, rd, False))
            stats[f"type:{name}"] += 1
    return dict(out), dict(stats)


def screen(market: Market, events_by_type: dict[str, list[EventRow]], *, segment: tuple[date, date],
           proxy: list[float], min_turnover: float) -> dict:
    """Per type: event vs placebo stats plus by-year post returns."""
    report = {}
    for name, events in events_by_type.items():
        rows, funnel = dx.event_table(market, events, segment=segment, proxy=proxy, caps={}, min_turnover=min_turnover,
                                      require_revenue=False)
        liquid = [r for r in rows if r["liquid"]]
        placebo = dx.placebo_rows(market, liquid, proxy, min_turnover=min_turnover)
        metrics = ("runup_20_abn", "reaction_abn", "post_1_abn", "post_4_abn", "post_10_abn", "post_20_abn",
                   "exec_open1_to_open5_abn")
        by_year = defaultdict(list)
        for r in liquid:
            by_year[r["reaction_date"][:4]].append(r)
        report[name] = {
            "n_events_total": len(events), "n_rows": len(rows), "n_liquid": len(liquid), "n_placebo": len(placebo),
            "funnel": funnel,
            "events": {m: dx.stats([r[m] for r in liquid]) for m in metrics},
            "placebo": {m: dx.stats([r[m] for r in placebo]) for m in metrics},
            "by_year_post4": {y: dx.stats([r["post_4_abn"] for r in rs]) for y, rs in sorted(by_year.items())},
            "by_year_post20": {y: dx.stats([r["post_20_abn"] for r in rs]) for y, rs in sorted(by_year.items())},
            "breakout_at_r": {g: dx.stats([r["exec_open1_to_open5_abn"] for r in liquid if str(r["breakout_at_r"]) == g])
                              for g in ("True", "False")},
        }
    return report


def markdown_table(report: dict, types: dict) -> str:
    def pct(st, key="mean"):
        return f"{st[key] * 100:+.2f}%" if st.get("n") else "-"
    lines = ["| 유형 | 기대 | n(유동) | 반응 | 사후4 | 플라시보4 | 사후20 | 플라시보20 | 체결4 | 플라시보체결 | 연도별 사후4 양수/전체 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, rep in sorted(report.items(), key=lambda kv: -kv[1]["n_liquid"]):
        ev, pl = rep["events"], rep["placebo"]
        years = rep["by_year_post4"]
        pos_years = sum(1 for y, st in years.items() if st.get("n", 0) >= 10 and st["mean"] > 0)
        tot_years = sum(1 for st in years.values() if st.get("n", 0) >= 10)
        lines.append(f"| {name} | {types[name]['expected']} | {rep['n_liquid']} | {pct(ev['reaction_abn'])} | "
                     f"{pct(ev['post_4_abn'])} | {pct(pl['post_4_abn'])} | {pct(ev['post_20_abn'])} | {pct(pl['post_20_abn'])} | "
                     f"{pct(ev['exec_open1_to_open5_abn'])} | {pct(pl['exec_open1_to_open5_abn'])} | {pos_years}/{tot_years} |")
    return "\n".join(lines)
