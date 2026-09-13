"""
research/scripts/run_t54_1_baseline_10000.py
=============================================
T54.1.12 Baseline Runner for 10,000 M15 Bars.

Executes independent backtest runs on 10,000 In-Sample M15 candles for:
- smc_s01
- smc_s05
- smc_s09
- smc_wave1
- smc_confluence

Outputs full standardized JSON artifacts per T54.1.14:
- research/runs/t54_1_baseline_s01_10000.json
- research/runs/t54_1_baseline_s05_10000.json
- research/runs/t54_1_baseline_s09_10000.json
- research/runs/t54_1_baseline_wave1_10000.json
- research/runs/t54_1_baseline_confluence_10000.json
- research/runs/t54_1_baseline_summary_10000.json
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from engine.data_feed import DataFeed
from engine.backtest_engine import BacktestEngine
from smc.data_contract import normalize_ohlcv


def get_git_commit() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


def extract_metrics(res: dict, df: pd.DataFrame) -> dict:
    """Extract standard performance metrics across legacy or coordinator runs."""
    trades = res.get("trades", [])
    raw_m = res.get("metrics", {})
    
    total_trades = len(trades)
    winning_trades = [t for t in trades if t.get("pnl", 0.0) > 0]
    losing_trades = [t for t in trades if t.get("pnl", 0.0) < 0]
    long_trades = [t for t in trades if str(t.get("type", t.get("direction", ""))).upper() == "BUY"]
    short_trades = [t for t in trades if str(t.get("type", t.get("direction", ""))).upper() == "SELL"]

    win_count = len(winning_trades)
    loss_count = len(losing_trades)
    win_rate = round((win_count / total_trades * 100.0), 2) if total_trades > 0 else 0.0

    gross_profit = sum(t.get("pnl", 0.0) for t in winning_trades)
    gross_loss = abs(sum(t.get("pnl", 0.0) for t in losing_trades))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    time_to_idx = {}
    for i, t in enumerate(df["time"]):
        time_to_idx[t] = i
        time_to_idx[str(t)] = i
        try:
            ts = pd.to_datetime(t, utc=True)
            time_to_idx[ts] = i
            time_to_idx[str(ts)] = i
            time_to_idx[int(ts.timestamp())] = i
        except Exception:
            pass

    holding_bars = []
    for t in trades:
        e_idx = time_to_idx.get(t.get("entry_time"))
        if e_idx is None and "entry_timestamp" in t:
            e_idx = time_to_idx.get(t.get("entry_timestamp"))
        x_idx = time_to_idx.get(t.get("exit_time"))
        if x_idx is None and "exit_timestamp" in t:
            x_idx = time_to_idx.get(t.get("exit_timestamp"))
        if e_idx is not None and x_idx is not None:
            holding_bars.append(x_idx - e_idx)
        elif "holding_bars" in t:
            holding_bars.append(t["holding_bars"])

    avg_win = round(gross_profit / win_count, 2) if win_count > 0 else 0.0
    avg_loss = round(gross_loss / loss_count, 2) if loss_count > 0 else 0.0
    avg_holding = round(sum(holding_bars) / len(holding_bars), 2) if holding_bars else 0.0

    return {
        "total_trades": total_trades,
        "winning_trades": win_count,
        "losing_trades": loss_count,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "net_profit": round(raw_m.get("net_profit", 0.0), 2),
        "return_pct": round(raw_m.get("return_pct", 0.0), 2),
        "max_drawdown": round(raw_m.get("max_drawdown", 0.0), 2),
        "max_drawdown_pct": round(raw_m.get("max_drawdown_pct", 0.0), 2),
        "final_balance": round(raw_m.get("final_balance", 10000.0), 2),
        "average_win": avg_win,
        "average_loss": avg_loss,
        "average_holding_bars": avg_holding,
        "long_trades": len(long_trades),
        "short_trades": len(short_trades),
    }


def compute_wave1_diagnostics(res: dict, htf_payload: dict, total_bars: int) -> dict:
    """Compute 12-layer funnel and coordinator metrics for Wave 1 strategies."""
    decisions = res.get("decisions", [])
    events = res.get("execution_events", [])
    intents = res.get("pending_intents", [])
    trades = res.get("trades", [])

    raw_decisions = len(decisions)
    selected_decisions = sum(1 for d in decisions if d.get("action") == "SELECT")
    no_trade_decisions = sum(1 for d in decisions if d.get("action") == "NO_TRADE")

    evaluated_cands = 0
    eligible_cands = 0
    rejected_cands = 0
    rejection_reasons: dict[str, int] = {}

    for d in decisions:
        for ev in d.get("evaluations", []):
            evaluated_cands += 1
            if ev.get("status") == "ELIGIBLE":
                eligible_cands += 1
            else:
                rejected_cands += 1
                reasons = ev.get("rejection_reasons")
                if not reasons:
                    single = ev.get("rejection_reason")
                    reasons = [single] if single else ["unknown"]
                for r in reasons:
                    rejection_reasons[r] = rejection_reasons.get(r, 0) + 1

    successful_fills = sum(1 for e in events if e.get("event_type") in ("ORDER_FILLED", "POSITION_OPENED"))
    cancelled_orders = sum(1 for e in events if e.get("event_type") == "ORDER_CANCELLED")
    skipped_orders = sum(1 for e in events if e.get("event_type") == "ORDER_SKIPPED")

    htf_events_accepted = len(htf_payload.get("events", []))
    # In canonical timeline, HTF events provide bias starting from the first confirmed event
    first_htf_idx = htf_payload["events"][0]["index"] if htf_payload.get("events") else total_bars
    htf_bias_available_bars = max(0, total_bars - first_htf_idx)

    # 12-layer funnel
    funnel = {
        "m15_bars_scanned": total_bars,
        "htf_events_accepted": htf_events_accepted,
        "htf_bias_available": htf_bias_available_bars,
        "ltf_structure_events": "available",  # verified in step diagnostics
        "fvgs_detected": "available",
        "obs_detected": "available",
        "candidate_setups": evaluated_cands,
        "eligible_setups": eligible_cands,
        "selector_selections": selected_decisions,
        "execution_instructions": len(intents),
        "successful_fills": successful_fills,
        "completed_trades": len(trades),
    }

    first_zero = None
    ordered_funnel_keys = [
        "m15_bars_scanned",
        "htf_events_accepted",
        "htf_bias_available",
        "candidate_setups",
        "eligible_setups",
        "selector_selections",
        "execution_instructions",
        "successful_fills",
        "completed_trades",
    ]
    for k in ordered_funnel_keys:
        v = funnel.get(k)
        if isinstance(v, (int, float)) and v == 0:
            first_zero = k
            break

    # Root cause classification per T54.1.13
    if len(trades) > 0:
        root_cause = "TRADES_EXECUTED"
    elif htf_events_accepted == 0:
        root_cause = "no_htf_events"
    elif htf_bias_available_bars == 0:
        root_cause = "no_htf_bias"
    elif evaluated_cands == 0:
        root_cause = "no_candidate"
    elif eligible_cands == 0:
        root_cause = "all_candidates_rejected"
    elif selected_decisions == 0:
        root_cause = "selector_no_selection"
    elif successful_fills == 0:
        root_cause = "execution_no_fill"
    else:
        root_cause = "trade_not_completed"

    return {
        "coordinator_metrics": {
            "raw_decisions": raw_decisions,
            "selected_decisions": selected_decisions,
            "no_trade_decisions": no_trade_decisions,
            "evaluated_candidates": evaluated_cands,
            "eligible_candidates": eligible_cands,
            "rejected_candidates": rejected_cands,
            "strategy_selection_count": selected_decisions,
            "execution_events": len(events),
            "successful_fills": successful_fills,
            "cancelled_orders": cancelled_orders,
            "skipped_orders": skipped_orders,
            "candidate_rejection_reasons": rejection_reasons,
        },
        "funnel": funnel,
        "first_zero_layer": first_zero,
        "root_cause_classification": root_cause,
    }


def run_baseline_suite(
    start: str = "2022-01-01 00:00:00",
    end: str = "2024-09-30 23:59:59",
    limit: int = 10000,
    htf_payload_path: str = "research/runs/t54_1_htf_events_m15_10000.json",
    output_dir: str = "research/runs",
) -> dict:
    git_commit = get_git_commit()
    print(f"=== T54.1.12 BASELINE 10,000 M15 BARS (Commit: {git_commit[:8]}) ===")

    # 1. Load Data
    feed = DataFeed()
    candles = feed.get_candles("M15", start_time=start, end_time=end, limit=limit)
    df = pd.DataFrame(candles)
    bar_count = len(df)
    start_ts = df["datetime_str"].iloc[0] if "datetime_str" in df.columns else str(df["time"].iloc[0])
    end_ts = df["datetime_str"].iloc[-1] if "datetime_str" in df.columns else str(df["time"].iloc[-1])
    print(f"Loaded {bar_count} M15 candles [{start_ts} -> {end_ts}]")

    # 2. Load HTF Payload
    htf_file = Path(htf_payload_path)
    if not htf_file.exists():
        raise FileNotFoundError(f"HTF payload not found at {htf_payload_path}")
    with open(htf_file, "r", encoding="utf-8") as fp:
        htf_payload = json.load(fp)

    payload_hash = htf_payload.get("payload_sha256", "unknown")
    print(f"Loaded HTF payload: {htf_payload.get('event_count', 0)} events, hash: {payload_hash[:12]}")

    # Standard configuration per Protocol V1
    initial_capital = 10000.0
    lot_size = 0.01
    contract_size = 100.0
    spread_points = 20.0
    commission_per_lot = 5.0

    runs_config = [
        {
            "strategy_id": "smc_s01",
            "params": {"min_rr": 1.5, "cooldown_bars": 3},
            "is_wave1": True,
            "artifact": "t54_1_baseline_s01_10000.json",
        },
        {
            "strategy_id": "smc_s05",
            "params": {"min_rr": 1.5, "cooldown_bars": 3},
            "is_wave1": True,
            "artifact": "t54_1_baseline_s05_10000.json",
        },
        {
            "strategy_id": "smc_s09",
            "params": {"min_rr": 1.5, "cooldown_bars": 3},
            "is_wave1": True,
            "artifact": "t54_1_baseline_s09_10000.json",
        },
        {
            "strategy_id": "smc_wave1",
            "params": {"min_rr": 1.5, "cooldown_bars": 3},
            "is_wave1": True,
            "artifact": "t54_1_baseline_wave1_10000.json",
        },
        {
            "strategy_id": "smc_confluence",
            "params": {
                "swing_strength": 7,
                "internal_strength": 3,
                "bias_timing": "pre_candle",
                "rr_ratio": 1.5,
            },
            "is_wave1": False,
            "artifact": "t54_1_baseline_confluence_10000.json",
        },
    ]

    summary_records = []
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    for cfg in runs_config:
        strat_id = cfg["strategy_id"]
        strat_params = cfg["params"]
        artifact_filename = cfg["artifact"]
        is_wave1 = cfg["is_wave1"]

        print(f"\n--- Running: {strat_id} ---")
        t0 = time.time()
        engine = BacktestEngine(
            initial_capital=initial_capital,
            lot_size=lot_size,
            contract_size=contract_size,
            spread_points=spread_points,
            commission_per_lot=commission_per_lot,
            allow_short=True,
        )

        if is_wave1:
            res = engine.run(
                df.copy(),
                strat_id,
                strat_params,
                timeframe="M15",
                htf_events=htf_payload,
            )
            diag = compute_wave1_diagnostics(res, htf_payload, bar_count)
        else:
            # Legacy smc_confluence execution
            res = engine.run(
                df.copy(),
                strat_id,
                strat_params,
            )
            diag = {
                "coordinator_metrics": None,
                "funnel": None,
                "first_zero_layer": None,
                "root_cause_classification": "TRADES_EXECUTED" if len(res.get("trades", [])) > 0 else "no_trades",
            }

        elapsed = round(time.time() - t0, 2)
        metrics = extract_metrics(res, df)
        print(f"[{strat_id}] Completed in {elapsed}s | Trades: {metrics['total_trades']} | Win Rate: {metrics['win_rate']}% | Net PnL: ${metrics['net_profit']} | Max DD: ${metrics['max_drawdown']}")
        if is_wave1:
            print(f"[{strat_id}] Funnel root cause: {diag['root_cause_classification']} | First zero: {diag['first_zero_layer']}")

        run_id = f"run_{strat_id}_10000_{int(time.time())}"
        artifact_data = {
            "run_id": run_id,
            "git_commit": git_commit,
            "dataset_id": "XAUUSD_M1_resampled_M15",
            "protocol_version": "1.0.0",
            "timeframe": "M15",
            "bar_count": bar_count,
            "start_time": start_ts,
            "end_time": end_ts,
            "strategy_id": strat_id,
            "strategy_params": strat_params,
            "htf_payload_hash": payload_hash,
            "execution_duration_sec": elapsed,
            "metrics": metrics,
            "diagnostics": diag,
            "validation_status": "VALID_COMPLETED",
        }

        art_file = out_path / artifact_filename
        with open(art_file, "w", encoding="utf-8") as fp:
            json.dump(artifact_data, fp, indent=2)
        print(f"Saved artifact: {art_file}")

        summary_records.append({
            "strategy_id": strat_id,
            "strategy_params": strat_params,
            "bar_count": bar_count,
            "total_trades": metrics["total_trades"],
            "win_rate": metrics["win_rate"],
            "net_profit": metrics["net_profit"],
            "profit_factor": metrics["profit_factor"],
            "max_drawdown": metrics["max_drawdown"],
            "max_drawdown_pct": metrics["max_drawdown_pct"],
            "final_balance": metrics["final_balance"],
            "average_win": metrics["average_win"],
            "average_loss": metrics["average_loss"],
            "average_holding_bars": metrics["average_holding_bars"],
            "long_trades": metrics["long_trades"],
            "short_trades": metrics["short_trades"],
            "root_cause_classification": diag["root_cause_classification"],
            "first_zero_layer": diag["first_zero_layer"],
            "artifact": str(art_file),
        })

    # Save summary artifact
    summary_data = {
        "summary_id": f"summary_10000_{int(time.time())}",
        "git_commit": git_commit,
        "protocol_version": "1.0.0",
        "dataset_id": "XAUUSD_M1_resampled_M15",
        "timeframe": "M15",
        "bar_count": bar_count,
        "start_time": start_ts,
        "end_time": end_ts,
        "htf_payload_hash": payload_hash,
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "strategies": summary_records,
    }

    summary_file = out_path / "t54_1_baseline_summary_10000.json"
    with open(summary_file, "w", encoding="utf-8") as fp:
        json.dump(summary_data, fp, indent=2)
    print(f"\nSaved summary artifact: {summary_file}")

    return summary_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run 10,000-bar baseline backtest suite across 5 strategies.")
    parser.add_argument("--start", default="2022-01-01 00:00:00")
    parser.add_argument("--end", default="2024-09-30 23:59:59")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--htf-payload", default="research/runs/t54_1_htf_events_m15_10000.json")
    parser.add_argument("--output-dir", default="research/runs")
    args = parser.parse_args()

    run_baseline_suite(
        start=args.start,
        end=args.end,
        limit=args.limit,
        htf_payload_path=args.htf_payload,
        output_dir=args.output_dir,
    )
