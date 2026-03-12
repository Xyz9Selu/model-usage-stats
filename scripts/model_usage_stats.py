#!/usr/bin/env python3
"""OpenClaw model usage stats.

Data source: `openclaw sessions --json` (or `--all-agents`).
Counts: dedupe by sessionId to avoid double-counting `:run:` shadow keys.

Outputs:
- summary by modelProvider+model
- optional daily breakdown
- optional CSV export

Designed to run via: `uv run .../model_usage_stats.py`
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    key: str
    updated_at_ms: int
    agent_id: str
    kind: str
    model_provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


def _run_openclaw_sessions(all_agents: bool) -> Dict[str, Any]:
    cmd = ["openclaw", "sessions", "--json"]
    if all_agents:
        cmd.insert(2, "--all-agents")
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise SystemExit("openclaw not found in PATH")
    except subprocess.CalledProcessError as e:
        raise SystemExit(e.output.decode("utf-8", errors="replace"))
    return json.loads(out.decode("utf-8"))


def _safe_int(x: Any) -> int:
    try:
        if x is None:
            return 0
        if isinstance(x, bool):
            return int(x)
        return int(x)
    except Exception:
        return 0


def _parse_sessions(payload: Dict[str, Any]) -> List[SessionRow]:
    rows: List[SessionRow] = []
    for s in payload.get("sessions", []) or []:
        if not isinstance(s, dict):
            continue
        session_id = str(s.get("sessionId") or "").strip()
        if not session_id:
            # Without sessionId we can't dedupe; still keep a synthetic id.
            session_id = f"key:{s.get('key')}"
        key = str(s.get("key") or "")
        updated = _safe_int(s.get("updatedAt"))
        agent_id = str(s.get("agentId") or "")
        kind = str(s.get("kind") or "")
        model_provider = str(s.get("modelProvider") or "")
        model = str(s.get("model") or "")

        input_tokens = _safe_int(s.get("inputTokens"))
        output_tokens = _safe_int(s.get("outputTokens"))
        total_tokens = _safe_int(s.get("totalTokens"))
        if total_tokens == 0:
            total_tokens = input_tokens + output_tokens

        rows.append(
            SessionRow(
                session_id=session_id,
                key=key,
                updated_at_ms=updated,
                agent_id=agent_id,
                kind=kind,
                model_provider=model_provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
        )
    return rows


def _ms_to_local_date(ms: int) -> str:
    if ms <= 0:
        return "unknown"
    # ms is epoch milliseconds
    d = dt.datetime.fromtimestamp(ms / 1000.0)
    return d.strftime("%Y-%m-%d")


def _filter_since(rows: Iterable[SessionRow], since_ms: Optional[int]) -> List[SessionRow]:
    if since_ms is None:
        return list(rows)
    return [r for r in rows if r.updated_at_ms and r.updated_at_ms >= since_ms]


def _compute_since_ms(args: argparse.Namespace) -> Optional[int]:
    now = dt.datetime.now()
    if args.since_hours is not None:
        return int((now - dt.timedelta(hours=args.since_hours)).timestamp() * 1000)
    if args.since_days is not None:
        return int((now - dt.timedelta(days=args.since_days)).timestamp() * 1000)
    if args.since_date:
        try:
            d = dt.datetime.strptime(args.since_date, "%Y-%m-%d")
            return int(d.timestamp() * 1000)
        except ValueError:
            raise SystemExit("--since-date must be YYYY-MM-DD")
    return None


def _dedupe_by_session_id(rows: List[SessionRow]) -> List[SessionRow]:
    # Keep the latest record per session_id (most recent tokens tend to be fresher)
    by_id: Dict[str, SessionRow] = {}
    for r in rows:
        prev = by_id.get(r.session_id)
        if prev is None or r.updated_at_ms >= prev.updated_at_ms:
            by_id[r.session_id] = r
    return list(by_id.values())


def _agg(rows: List[SessionRow]) -> List[Dict[str, Any]]:
    bucket: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in rows:
        k = (r.model_provider or "(unknown-provider)", r.model or "(unknown-model)")
        b = bucket.get(k)
        if b is None:
            b = {
                "modelProvider": k[0],
                "model": k[1],
                "calls": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
                "agents": set(),
            }
            bucket[k] = b
        b["calls"] += 1
        b["inputTokens"] += r.input_tokens
        b["outputTokens"] += r.output_tokens
        b["totalTokens"] += r.total_tokens
        b["agents"].add(r.agent_id)

    out: List[Dict[str, Any]] = []
    for _, b in bucket.items():
        b["agents"] = ",".join(sorted(a for a in b["agents"] if a))
        out.append(b)

    out.sort(key=lambda x: (-(x["calls"]), -(x["totalTokens"]), x["modelProvider"], x["model"]))
    return out


def _agg_by_day(rows: List[SessionRow]) -> List[Dict[str, Any]]:
    bucket: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in rows:
        day = _ms_to_local_date(r.updated_at_ms)
        k = (day, r.model_provider or "(unknown-provider)", r.model or "(unknown-model)")
        b = bucket.get(k)
        if b is None:
            b = {
                "day": day,
                "modelProvider": k[1],
                "model": k[2],
                "calls": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
            }
            bucket[k] = b
        b["calls"] += 1
        b["inputTokens"] += r.input_tokens
        b["outputTokens"] += r.output_tokens
        b["totalTokens"] += r.total_tokens

    out = list(bucket.values())
    out.sort(key=lambda x: (x["day"], -x["calls"], -x["totalTokens"]))
    return out


def _print_table(rows: List[Dict[str, Any]], include_agents: bool) -> None:
    if not rows:
        print("(no data)")
        return

    headers = ["calls", "totalTokens", "inputTokens", "outputTokens", "modelProvider", "model"]
    if include_agents:
        headers.append("agents")

    # Column widths
    widths = {h: len(h) for h in headers}
    for r in rows:
        for h in headers:
            widths[h] = max(widths[h], len(str(r.get(h, ""))))

    def fmt_row(r: Dict[str, Any]) -> str:
        return " | ".join(str(r.get(h, "")).rjust(widths[h]) for h in headers)

    print(fmt_row({h: h for h in headers}))
    print("-+-".join("-" * widths[h] for h in headers))
    for r in rows:
        print(fmt_row(r))


def _write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-agents", action="store_true", help="Aggregate sessions across all configured agents")
    ap.add_argument("--since-hours", type=float, default=None)
    ap.add_argument("--since-days", type=float, default=None)
    ap.add_argument("--since-date", type=str, default=None, help="YYYY-MM-DD")
    ap.add_argument("--by", choices=["model", "day"], default="model")
    ap.add_argument("--csv", type=str, default=None, help="Write output CSV to this path")
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    ap.add_argument("--include-agents", action="store_true", help="Include agent ids in summary rows")
    args = ap.parse_args()

    since_ms = _compute_since_ms(args)

    payload = _run_openclaw_sessions(all_agents=args.all_agents)
    rows = _parse_sessions(payload)
    rows = _filter_since(rows, since_ms)
    rows = _dedupe_by_session_id(rows)

    if args.by == "day":
        out = _agg_by_day(rows)
    else:
        out = _agg(rows)

    if args.csv:
        _write_csv(args.csv, out)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _print_table(out, include_agents=args.include_agents and args.by == "model")


if __name__ == "__main__":
    main()
