"""
research/scripts/htf_event_runner.py
====================================
Canonical HTF (H1) Structure Event Runner with M15 Canonical Index Mapping (T54.1.10).

Translates Higher-Timeframe (H1) structure events into the Lower-Timeframe (M15)
canonical bar index space, guaranteeing:
1. Zero lookahead: H1 event confirmed at H1 candle close is only available at
   the M15 bar that closes at or after the H1 close time.
2. Index space alignment: event.index is canonical_m15_index (0 <= index < N_m15).
3. Deterministic sorting and identity hashing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from engine.data_feed import DataFeed
from smc.data_contract import normalize_ohlcv
from smc.structure.bos_choch import detect_structure_events
from smc.structure.swings import detect_swings


def generate_htf_events_m15(
    start: str = "2022-01-01 00:00:00",
    end: str = "2024-09-30 23:59:59",
    source_timeframe: str = "H1",
    execution_timeframe: str = "M15",
    limit: int = 10000,
    strength: int = 5,
) -> dict:
    """
    Generates H1 structure events and projects them into the M15 canonical index space.
    """
    feed = DataFeed()

    # 1. Load M15 execution bars
    m15_rows = feed.get_candles(
        timeframe=execution_timeframe,
        start_time=start,
        end_time=end,
        limit=limit,
    )
    if not m15_rows:
        raise ValueError(f"No {execution_timeframe} candles found for range [{start}, {end}]")

    m15 = normalize_ohlcv(m15_rows)
    m15_bar_count = len(m15)
    m15_start_ts = m15.index[0]
    m15_end_ts = m15.index[-1]
    m15_close_times = m15.index + pd.Timedelta(minutes=15)

    # 2. Load H1 bars covering the range (plus lookahead boundary buffer)
    h1_end_buffer = m15_end_ts + pd.Timedelta(hours=3)
    h1_rows = feed.get_candles(
        timeframe=source_timeframe,
        start_time=start,
        end_time=str(h1_end_buffer),
        limit=100_000,
    )
    if not h1_rows:
        raise ValueError(f"No {source_timeframe} candles found for range [{start}, {h1_end_buffer}]")

    h1 = normalize_ohlcv(h1_rows)
    h1["bar_index"] = range(len(h1))

    # 3. Detect H1 swings and structure events
    swings = detect_swings(h1, strength=strength, mode="swing", only_confirmed=True)
    events = detect_structure_events(
        h1,
        swings=swings,
        strength=strength,
        mode="swing",
        current_bar_index=len(h1) - 1,
    )

    # 4. Map H1 events to M15 canonical index
    mapped_events = []
    for ev in events:
        h1_open_time = ev.time
        if h1_open_time.tzinfo is None:
            h1_open_time = h1_open_time.tz_localize("UTC")
        else:
            h1_open_time = h1_open_time.tz_convert("UTC")

        # Break occurs on H1 close -> event is available when H1 bar closes (1 hour after open)
        event_available_time = h1_open_time + pd.Timedelta(hours=1)

        # Exclude events confirmed before the first M15 bar closed
        if event_available_time < m15_close_times[0]:
            continue

        # Exclude events confirmed after the last M15 bar closed
        if event_available_time > m15_close_times[-1]:
            continue

        # Find the FIRST M15 bar whose close_time >= event_available_time
        canonical_m15_idx = int(m15_close_times.searchsorted(event_available_time))

        # Enforce canonical range [0, m15_bar_count - 1]
        if not (0 <= canonical_m15_idx < m15_bar_count):
            continue

        ev_dict = {
            "index": canonical_m15_idx,
            "canonical_m15_index": canonical_m15_idx,
            "time": event_available_time.isoformat(),
            "event_time": h1_open_time.isoformat(),
            "event_available_time": event_available_time.isoformat(),
            "source_h1_index": int(ev.index),
            "event_type": str(ev.event_type),
            "direction": str(ev.direction),
            "broken_swing_index": int(ev.broken_swing_index),
            "broken_swing_price": float(ev.broken_swing_price),
            "close_price": float(ev.close_price),
            "displacement": bool(ev.displacement),
            "mode": str(ev.mode),
            "confirmed_swing_at": int(ev.confirmed_swing_at),
            "body_size": float(ev.body_size),
            "atr_value": float(ev.atr_value),
            "break_type": str(ev.break_type),
            "structure_leg_id": str(ev.structure_leg_id) if ev.structure_leg_id else None,
        }
        mapped_events.append(ev_dict)

    # 5. Deterministic sorting: canonical_m15_index, event_time, event_type, direction
    mapped_events.sort(
        key=lambda e: (
            e["canonical_m15_index"],
            e["event_time"],
            e["event_type"],
            e["direction"],
        )
    )

    payload = {
        "runner_version": "1.0.0",
        "source_timeframe": source_timeframe,
        "execution_timeframe": execution_timeframe,
        "event_index_space": "M15_CANONICAL",
        "start": start,
        "end": end,
        "m15_start_time": m15_start_ts.isoformat(),
        "m15_end_time": m15_end_ts.isoformat(),
        "bar_count": m15_bar_count,
        "event_count": len(mapped_events),
        "strength": strength,
        "events": mapped_events,
    }

    # Compute deterministic payload hash (excluding hash field itself)
    payload_json = json.dumps(payload, indent=2, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    payload["payload_sha256"] = payload_hash

    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate canonical HTF structure events mapped to M15 index.")
    parser.add_argument("--start", default="2022-01-01 00:00:00")
    parser.add_argument("--end", default="2024-09-30 23:59:59")
    parser.add_argument("--source-timeframe", default="H1")
    parser.add_argument("--execution-timeframe", default="M15")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--strength", type=int, default=5)
    parser.add_argument("--output", default="research/runs/t54_1_htf_events_m15_10000.json")
    args = parser.parse_args()

    payload = generate_htf_events_m15(
        start=args.start,
        end=args.end,
        source_timeframe=args.source_timeframe,
        execution_timeframe=args.execution_timeframe,
        limit=args.limit,
        strength=args.strength,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({
        "status": "SUCCESS",
        "output": str(output_path),
        "source_timeframe": payload["source_timeframe"],
        "execution_timeframe": payload["execution_timeframe"],
        "bar_count": payload["bar_count"],
        "event_count": payload["event_count"],
        "payload_sha256": payload["payload_sha256"],
        "first_event_index": payload["events"][0]["index"] if payload["events"] else None,
        "last_event_index": payload["events"][-1]["index"] if payload["events"] else None,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
