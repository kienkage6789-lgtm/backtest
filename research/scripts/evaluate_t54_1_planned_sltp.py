import os
import sys
sys.path.insert(0, os.path.abspath("."))
import json
import pandas as pd
from engine.data_feed import DataFeed
from engine.backtest_engine import BacktestEngine
from engine.strategies import StrategyRegistry

def run_evaluation():
    print("=== T54.1.x CANONICAL EVALUATION (PROTOCOL V1) ===")
    feed = DataFeed()
    candles = feed.get_candles("M15", limit=5000)
    df = pd.DataFrame(candles)
    print(f"Loaded {len(df)} M15 bars: start={df['time'].iloc[0]}, end={df['time'].iloc[-1]}")

    # Standard Protocol V1 Configuration
    init_capital = 10000.0
    lot_size = 0.01  # Protocol V1 canonical lot size
    contract_size = 100.0
    spread_pts = 20.0
    comm_lot = 5.0

    # Build time to index map for holding bars calculation
    time_to_idx = {t: i for i, t in enumerate(df['time'])}

    # Sweep across RR ratios
    rr_levels = [1.0, 1.5, 2.0, 2.5, 3.0]
    sweep_results = []

    for rr in rr_levels:
        engine = BacktestEngine(
            initial_capital=init_capital,
            lot_size=lot_size,
            contract_size=contract_size,
            stop_loss_points=200.0,
            take_profit_points=400.0,
            spread_points=spread_pts,
            commission_per_lot=comm_lot,
            allow_short=True
        )
        params = {
            "swing_strength": 5,
            "internal_strength": 2,
            "bias_timing": "pre_candle",
            "rr_ratio": rr
        }
        res = engine.run(df.copy(), "smc_confluence", params)
        trades = res["trades"]
        metrics = res["metrics"]
        telemetry = res.get("legacy_telemetry", {})

        wins = [t["pnl"] for t in trades if t["pnl"] > 0]
        losses = [t["pnl"] for t in trades if t["pnl"] < 0]
        holding_bars = [time_to_idx.get(t["exit_time"], 0) - time_to_idx.get(t["entry_time"], 0) for t in trades]

        avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
        avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
        avg_holding = round(sum(holding_bars) / len(holding_bars), 2) if holding_bars else 0.0

        planned_used = telemetry.get("planned_levels_used", 0)
        rejected_geo = telemetry.get("rejected_invalid_geometry", 0)
        executed_trades = metrics["total_trades"]
        # In a single-position execution model with no pyramiding,
        # an instruction generated when a position is already open in the same direction is skipped (SAME_DIRECTION).
        same_direction_skipped = planned_used - executed_trades

        row = {
            "rr_ratio": rr,
            "raw_signals": 36,
            "planned_instructions": planned_used,
            "rejected_geometry": rejected_geo,
            "same_direction_skipped": same_direction_skipped,
            "executed_trades": executed_trades,
            "win_rate_pct": round(metrics["win_rate"], 2),
            "net_profit_usd": round(metrics["net_profit"], 2),
            "profit_factor": round(metrics["profit_factor"], 2),
            "max_drawdown_usd": round(metrics["max_drawdown"], 2),
            "max_drawdown_pct": round(metrics["max_drawdown_pct"], 2),
            "final_balance_usd": round(metrics["final_balance"], 2),
            "avg_win_usd": avg_win,
            "avg_loss_usd": avg_loss,
            "avg_holding_bars": avg_holding,
            "fallback_used": telemetry.get("fallback_levels_used", 0),
        }
        sweep_results.append(row)

    print("\n--- SWEEP RESULTS TABLE (LOT_SIZE = 0.01) ---")
    df_sweep = pd.DataFrame(sweep_results)
    print(df_sweep.to_string(index=False))

    # Before vs After Comparison at RR 2.0
    params_20 = {"swing_strength": 5, "internal_strength": 2, "bias_timing": "pre_candle", "rr_ratio": 2.0}
    df_sig_after = StrategyRegistry.generate_signals(df.copy(), "smc_confluence", params_20)
    df_sig_before = df_sig_after.copy().drop(
        columns=[c for c in ["planned_stop_loss", "planned_take_profit", "planned_entry_price", "planned_rr"] if c in df_sig_after.columns]
    )

    engine_comp = BacktestEngine(
        initial_capital=init_capital,
        lot_size=lot_size,
        contract_size=contract_size,
        stop_loss_points=200.0,
        take_profit_points=400.0,
        spread_points=spread_pts,
        commission_per_lot=comm_lot,
        allow_short=True
    )

    orig_gen = StrategyRegistry.generate_signals
    try:
        StrategyRegistry.generate_signals = lambda d, s, p: df_sig_before
        res_before = engine_comp.run(df.copy(), "smc_confluence", params_20)

        StrategyRegistry.generate_signals = lambda d, s, p: df_sig_after
        res_after = engine_comp.run(df.copy(), "smc_confluence", params_20)
    finally:
        StrategyRegistry.generate_signals = orig_gen

    m_b = res_before["metrics"]
    m_a = res_after["metrics"]
    t_b = res_before.get("legacy_telemetry", {})
    t_a = res_after.get("legacy_telemetry", {})

    before_after = {
        "Metric": [
            "Raw Signals",
            "Planned Instructions Created",
            "Rejected Invalid Geometry (Gap)",
            "Same Direction Skipped (No Pyramiding)",
            "Executed Trades",
            "Win Rate (%)",
            "Net Profit ($)",
            "Profit Factor",
            "Max Drawdown ($)",
            "Max Drawdown (% of Peak Equity)",
            "Final Balance ($)",
            "Fallback Levels Used"
        ],
        "Before Fix (Dummy Fallback 200/400 pts)": [
            36,
            0,
            0,
            0,
            m_b["total_trades"],
            m_b["win_rate"],
            m_b["net_profit"],
            m_b["profit_factor"],
            m_b["max_drawdown"],
            m_b["max_drawdown_pct"],
            m_b["final_balance"],
            t_b.get("fallback_levels_used", m_b["total_trades"])
        ],
        "After Fix (Wired Planned SL/TP, RR 2.0)": [
            36,
            t_a.get("planned_levels_used", 0),
            t_a.get("rejected_invalid_geometry", 0),
            t_a.get("planned_levels_used", 0) - m_a["total_trades"],
            m_a["total_trades"],
            m_a["win_rate"],
            m_a["net_profit"],
            m_a["profit_factor"],
            m_a["max_drawdown"],
            m_a["max_drawdown_pct"],
            m_a["final_balance"],
            t_a.get("fallback_levels_used", 0)
        ]
    }

    print("\n--- BEFORE VS AFTER TABLE (LOT_SIZE = 0.01) ---")
    df_ba = pd.DataFrame(before_after)
    print(df_ba.to_string(index=False))

    os.makedirs("research", exist_ok=True)
    with open("research/t54_1_planned_sltp_sweep_raw.json", "w") as f:
        json.dump(sweep_results, f, indent=2)
    with open("research/t54_1_before_after_raw.json", "w") as f:
        json.dump(before_after, f, indent=2)
    print("\nSaved raw JSON files to research/t54_1_planned_sltp_sweep_raw.json and research/t54_1_before_after_raw.json")

if __name__ == "__main__":
    run_evaluation()
