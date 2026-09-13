import os
import sys
sys.path.insert(0, os.path.abspath("."))
import json
import pandas as pd
from engine.backtest_engine import BacktestEngine
from engine.data_feed import DataFeed

def run_evaluation():
    feed = DataFeed()
    candles = feed.get_candles("M15", limit=5000)
    df = pd.DataFrame(candles)
    print(f"Loaded {len(df)} candles from {df['time'].iloc[0]} to {df['time'].iloc[-1]}")

    rr_levels = [1.0, 1.5, 2.0, 2.5, 3.0]
    results = []

    time_to_idx = {t: i for i, t in enumerate(df['time'])}

    for rr in rr_levels:
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

        row = {
            "rr_ratio": rr,
            "total_trades": metrics["total_trades"],
            "win_rate": round(metrics["win_rate"], 2),
            "net_profit": round(metrics["net_profit"], 2),
            "profit_factor": round(metrics["profit_factor"], 2),
            "max_drawdown": round(metrics["max_drawdown"], 2),
            "final_balance": round(metrics["final_balance"], 2),
            "average_win": avg_win,
            "average_loss": avg_loss,
            "average_holding_bars": avg_holding,
            "planned_levels_used": telemetry.get("planned_levels_used", 0),
            "fallback_levels_used": telemetry.get("fallback_levels_used", 0),
            "rejected_invalid_geometry": telemetry.get("rejected_invalid_geometry", 0),
        }
        results.append(row)

    print("\n--- RESULTS TABLE ---")
    headers = list(results[0].keys())
    print(" | ".join(headers))
    for r in results:
        print(" | ".join(str(r[h]) for h in headers))

    with open("scratch/smc_rr_sweep_5000_m15.json", "w") as f:
        json.dump(results, f, indent=2)

if __name__ == "__main__":
    run_evaluation()
