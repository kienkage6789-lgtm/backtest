"""
scratch/eval_s05_comparisons.py
===============================
Comparative evaluation of S05 Legacy vs New LTF Confirmation Variants on 10,000 M15 bars.
"""

import sys
from pathlib import Path
import json
import math
import copy
import pandas as pd
import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from engine.data_feed import DataFeed
from engine.backtest_engine import BacktestEngine
from smc.engine.strategies.s05_bos_ob_retest import S05BOSOBRetestStrategy, S05MacroState


def run_comparisons():
    feed = DataFeed()
    candles = feed.get_candles("M15", start_time="2022-01-01 00:00:00", end_time="2024-09-30 23:59:59", limit=10000)
    df = pd.DataFrame(candles)
    print(f"Loaded {len(df)} candles")

    htf_payload_path = ROOT_DIR / "research" / "runs" / "t54_1_htf_events_m15_10000.json"
    with open(htf_payload_path, "r", encoding="utf-8") as fp:
        htf_payload = json.load(fp)

    configs_to_test = [
        # (name, params)
        ("1. S05 Legacy (Disp=True)", {
            "s05_require_ltf_confirmation": False,
            "s05_require_displacement": True,
        }),
        ("1b. S05 Legacy (Disp=False)", {
            "s05_require_ltf_confirmation": False,
            "s05_require_displacement": False,
        }),
        ("2a. CHoCH + FVG (Disp=True)", {
            "s05_require_ltf_confirmation": True,
            "s05_ltf_confirmation_event_types": ("CHoCH",),
            "s05_ltf_entry_zone": "fvg",
            "s05_require_displacement": True,
        }),
        ("2b. CHoCH + FVG (Disp=False)", {
            "s05_require_ltf_confirmation": True,
            "s05_ltf_confirmation_event_types": ("CHoCH",),
            "s05_ltf_entry_zone": "fvg",
            "s05_require_displacement": False,
        }),
        ("3a. BOS + FVG (Disp=True)", {
            "s05_require_ltf_confirmation": True,
            "s05_ltf_confirmation_event_types": ("BOS",),
            "s05_ltf_entry_zone": "fvg",
            "s05_require_displacement": True,
        }),
        ("3b. BOS + FVG (Disp=False)", {
            "s05_require_ltf_confirmation": True,
            "s05_ltf_confirmation_event_types": ("BOS",),
            "s05_ltf_entry_zone": "fvg",
            "s05_require_displacement": False,
        }),
        ("4a. CHoCH/BOS + OB (Disp=True)", {
            "s05_require_ltf_confirmation": True,
            "s05_ltf_confirmation_event_types": ("CHoCH", "BOS"),
            "s05_ltf_entry_zone": "ob",
            "s05_require_displacement": True,
        }),
        ("4b. CHoCH/BOS + OB (Disp=False)", {
            "s05_require_ltf_confirmation": True,
            "s05_ltf_confirmation_event_types": ("CHoCH", "BOS"),
            "s05_ltf_entry_zone": "ob",
            "s05_require_displacement": False,
        }),
        ("5a. CHoCH/BOS + Either (Disp=True)", {
            "s05_require_ltf_confirmation": True,
            "s05_ltf_confirmation_event_types": ("CHoCH", "BOS"),
            "s05_ltf_entry_zone": "either",
            "s05_require_displacement": True,
        }),
        ("5b. CHoCH/BOS + Either (Disp=False)", {
            "s05_require_ltf_confirmation": True,
            "s05_ltf_confirmation_event_types": ("CHoCH", "BOS"),
            "s05_ltf_entry_zone": "either",
            "s05_require_displacement": False,
        }),
        ("6a. CHoCH/BOS + Confluence (Disp=True)", {
            "s05_require_ltf_confirmation": True,
            "s05_ltf_confirmation_event_types": ("CHoCH", "BOS"),
            "s05_ltf_entry_zone": "confluence",
            "s05_require_displacement": True,
        }),
        ("6b. CHoCH/BOS + Confluence (Disp=False)", {
            "s05_require_ltf_confirmation": True,
            "s05_ltf_confirmation_event_types": ("CHoCH", "BOS"),
            "s05_ltf_entry_zone": "confluence",
            "s05_require_displacement": False,
        }),
    ]

    all_results = []

    for name, params in configs_to_test:
        print(f"\n==================================================")
        print(f"Running Backtest for: {name}")
        print(f"Params: {params}")

        # Funnel trackers
        funnel = {
            "htf_bias_bars": 0,
            "htf_ob_created": set(),
            "htf_ob_touched": set(),
            "ltf_confirmations": set(),
            "ltf_zones_created": set(),
            "ltf_zones_retested": set(),
            "candidates_emitted": 0,
            "eligible_candidates": 0,
            "orders_selected": 0,
            "orders_filled": 0,
            "completed_trades": 0,
            "invalidated_before_retest": 0,
        }

        orig_eval = S05BOSOBRetestStrategy.evaluate

        def hooked_eval(self, context):
            if context.htf_bias is not None and context.htf_bias.bias in ("bullish", "bearish"):
                funnel["htf_bias_bars"] += 1

            for p in getattr(context, "active_htf_pois", ()):
                if getattr(p, "poi_type", "") in ("OB", "order_block"):
                    pid = getattr(p, "poi_id", f"{p.direction}_{p.created_at}")
                    funnel["htf_ob_created"].add(pid)
                    if context.low <= p.top and context.high >= p.bottom:
                        funnel["htf_ob_touched"].add(pid)

            for ob in getattr(context, "active_obs", ()):
                orig_t = getattr(ob, "origin_type", getattr(ob, "source_event_type", ""))
                if orig_t == "BOS":
                    pid = f"ob_{ob.direction}_{ob.index}"
                    funnel["htf_ob_created"].add(pid)
                    if context.low <= ob.high and context.high >= ob.low:
                        funnel["htf_ob_touched"].add(pid)

            cands = orig_eval(self, context)
            if cands:
                funnel["candidates_emitted"] += len(cands)

            # Track narrative stages if in confirmation mode
            if hasattr(self, "_confirmation_narratives"):
                for n in self._confirmation_narratives.values():
                    if n.confirmation_id:
                        funnel["ltf_confirmations"].add(n.confirmation_id)
                    if n.entry_zone_id:
                        funnel["ltf_zones_created"].add(n.entry_zone_id)
                    if n.retest_bar is not None:
                        funnel["ltf_zones_retested"].add(n.entry_zone_id)
                    if n.macro_state in (
                        S05MacroState.HTF_OB_INVALIDATED,
                        S05MacroState.LTF_CONFIRMATION_EXPIRED,
                        S05MacroState.LTF_ZONE_INVALIDATED,
                        S05MacroState.ENTRY_EXPIRED,
                        S05MacroState.OPPOSITE_STRUCTURE_SHIFT,
                        S05MacroState.RR_INVALID,
                    ):
                        funnel["invalidated_before_retest"] += 1

            return cands

        S05BOSOBRetestStrategy.evaluate = hooked_eval

        try:
            engine = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.01,
                contract_size=100.0,
                spread_points=20.0,
                commission_per_lot=5.0,
                allow_short=True,
            )

            res = engine.run(
                df.copy(),
                "smc_s05",
                {
                    "min_rr": 1.5,
                    "cooldown_bars": 3,
                    **params,
                },
                timeframe="M15",
                htf_events=htf_payload,
            )
        finally:
            S05BOSOBRetestStrategy.evaluate = orig_eval

        trades = res.get("trades", [])
        events = res.get("execution_events", [])
        decisions = res.get("decisions", [])

        funnel["orders_filled"] = len([e for e in events if e.get("event_type") == "ORDER_FILLED"])
        funnel["orders_selected"] = len([d for d in decisions if d.get("action") == "SELECT"])
        funnel["eligible_candidates"] = len([d for d in decisions if d.get("status") == "ELIGIBLE"])
        funnel["completed_trades"] = len(trades)

        total_trades = len(trades)
        winning_trades = [t for t in trades if t.get("pnl", 0.0) > 0]
        losing_trades = [t for t in trades if t.get("pnl", 0.0) < 0]
        win_rate = (len(winning_trades) / total_trades * 100.0) if total_trades > 0 else 0.0
        gross_profit = sum(t.get("pnl", 0.0) for t in winning_trades)
        gross_loss = abs(sum(t.get("pnl", 0.0) for t in losing_trades))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        net_pnl = sum(t.get("pnl", 0.0) for t in trades)

        # Max drawdown
        peak = 10000.0
        balance = 10000.0
        max_dd_dollars = 0.0
        max_dd_pct = 0.0
        for t in trades:
            balance += t.get("pnl", 0.0)
            if balance > peak:
                peak = balance
            dd = peak - balance
            dd_pct = (dd / peak * 100.0) if peak > 0 else 0.0
            if dd > max_dd_dollars:
                max_dd_dollars = dd
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

        # Average R
        r_multiples = [t.get("pnl_r", 0.0) for t in trades if "pnl_r" in t]
        avg_r = (sum(r_multiples) / len(r_multiples)) if r_multiples else (
            (net_pnl / (len(trades) * 20.0)) if trades else 0.0
        )

        long_trades = [t for t in trades if t.get("direction", "").upper() == "BUY"]
        short_trades = [t for t in trades if t.get("direction", "").upper() == "SELL"]

        metrics = {
            "name": name,
            "total_trades": total_trades,
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "net_pnl": round(net_pnl, 2),
            "max_dd_dollars": round(max_dd_dollars, 2),
            "max_dd_pct": round(max_dd_pct, 2),
            "avg_r": round(avg_r, 2),
            "long_trades": len(long_trades),
            "short_trades": len(short_trades),
            "funnel": {
                "htf_bias_bars": funnel["htf_bias_bars"],
                "htf_ob_created": len(funnel["htf_ob_created"]),
                "htf_ob_touched": len(funnel["htf_ob_touched"]),
                "ltf_confirmations": len(funnel["ltf_confirmations"]),
                "ltf_zones_created": len(funnel["ltf_zones_created"]),
                "ltf_zones_retested": len(funnel["ltf_zones_retested"]),
                "candidates_emitted": funnel["candidates_emitted"],
                "eligible_candidates": funnel["eligible_candidates"],
                "orders_selected": funnel["orders_selected"],
                "orders_filled": funnel["orders_filled"],
                "completed_trades": funnel["completed_trades"],
                "invalidated_before_retest": funnel["invalidated_before_retest"],
            }
        }
        all_results.append(metrics)

        print(f"Results for {name}:")
        print(f"  Trades: {total_trades} (W: {len(winning_trades)}, L: {len(losing_trades)})")
        print(f"  Win Rate: {win_rate:.2f}%, PF: {profit_factor:.2f}, Net PnL: ${net_pnl:.2f}, MaxDD: ${max_dd_dollars:.2f} ({max_dd_pct:.2f}%)")
        print(f"  Funnel: OB Created={len(funnel['htf_ob_created'])}, Touched={len(funnel['htf_ob_touched'])}, Conf={len(funnel['ltf_confirmations'])}, Retest={len(funnel['ltf_zones_retested'])}, Cand={funnel['candidates_emitted']}, Fill={funnel['orders_filled']}")

    # Output comparison table
    print("\n" + "="*95)
    print("COMPARATIVE SUMMARY TABLE (10,000 M15 Candles: 2022-01-01 to 2024-09-30)")
    print("="*95)
    header = f"{'Configuration':<38} | {'Trades':<6} | {'WinRate':<7} | {'PF':<5} | {'Net PnL':<9} | {'MaxDD ($)':<9} | {'Long/Short':<10}"
    print(header)
    print("-" * len(header))
    for r in all_results:
        ls_str = f"{r['long_trades']}/{r['short_trades']}"
        print(f"{r['name']:<38} | {r['total_trades']:<6} | {r['win_rate']:<6.1f}% | {r['profit_factor']:<5.2f} | ${r['net_pnl']:<8.2f} | ${r['max_dd_dollars']:<8.2f} | {ls_str:<10}")

    out_file = ROOT_DIR / "scratch" / "s05_comparison_results.json"
    with open(out_file, "w", encoding="utf-8") as fp:
        json.dump(all_results, fp, indent=2)
    print(f"\nSaved detailed results to {out_file}")


if __name__ == "__main__":
    run_comparisons()
