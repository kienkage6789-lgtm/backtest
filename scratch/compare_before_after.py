import os
import sys
sys.path.insert(0, os.path.abspath("."))
import json
import pandas as pd
from engine.data_feed import DataFeed
from engine.backtest_engine import BacktestEngine
from engine.strategies import StrategyRegistry

def main():
    feed = DataFeed()
    candles = feed.get_candles("M15", limit=5000)
    df = pd.DataFrame(candles)

    params = {"swing_strength": 5, "internal_strength": 2, "bias_timing": "pre_candle", "rr_ratio": 2.0}

    # Generate signals ONCE with planned SL/TP
    df_sig_after = StrategyRegistry.generate_signals(df.copy(), "smc_confluence", params)

    # Before fix: DataFrame has signals but NO planned columns
    df_sig_before = df_sig_after.copy().drop(
        columns=[c for c in ["planned_stop_loss", "planned_take_profit", "planned_entry_price", "planned_rr"] if c in df_sig_after.columns]
    )

    engine = BacktestEngine(
        initial_capital=10000.0,
        lot_size=0.1,
        contract_size=100.0,
        stop_loss_points=200.0,
        take_profit_points=400.0,
        spread_points=20.0,
        commission_per_lot=5.0,
        allow_short=True
    )

    original_generate = StrategyRegistry.generate_signals
    try:
        # Run BEFORE: mock generate_signals to return df_sig_before
        StrategyRegistry.generate_signals = lambda d, s, p: df_sig_before
        res_before = engine.run(df.copy(), "smc_confluence", params)

        # Run AFTER: mock generate_signals to return df_sig_after
        StrategyRegistry.generate_signals = lambda d, s, p: df_sig_after
        res_after = engine.run(df.copy(), "smc_confluence", params)
    finally:
        StrategyRegistry.generate_signals = original_generate

    m_b = res_before["metrics"]
    m_a = res_after["metrics"]

    comparison = {
        "Metric": [
            "Total Trades",
            "Win Rate (%)",
            "Net Profit ($)",
            "Profit Factor",
            "Max Drawdown ($)",
            "Max Drawdown (%)",
            "Final Balance ($)",
            "Planned Levels Used",
            "Fallback Levels Used",
            "Rejected Invalid Geometry"
        ],
        "Before Fix (Broken Fallback 200/400 pts)": [
            m_b["total_trades"],
            m_b["win_rate"],
            m_b["net_profit"],
            m_b["profit_factor"],
            m_b["max_drawdown"],
            m_b["max_drawdown_pct"],
            m_b["final_balance"],
            res_before.get("legacy_telemetry", {}).get("planned_levels_used", 0),
            res_before.get("legacy_telemetry", {}).get("fallback_levels_used", 0),
            res_before.get("legacy_telemetry", {}).get("rejected_invalid_geometry", 0),
        ],
        "After Fix (Wired Planned SL/TP, RR=2.0)": [
            m_a["total_trades"],
            m_a["win_rate"],
            m_a["net_profit"],
            m_a["profit_factor"],
            m_a["max_drawdown"],
            m_a["max_drawdown_pct"],
            m_a["final_balance"],
            res_after.get("legacy_telemetry", {}).get("planned_levels_used", 0),
            res_after.get("legacy_telemetry", {}).get("fallback_levels_used", 0),
            res_after.get("legacy_telemetry", {}).get("rejected_invalid_geometry", 0),
        ]
    }

    df_comp = pd.DataFrame(comparison)
    print(df_comp.to_string(index=False))

    with open("scratch/before_after_5000_m15.json", "w") as f:
        json.dump(comparison, f, indent=2)

if __name__ == "__main__":
    main()
