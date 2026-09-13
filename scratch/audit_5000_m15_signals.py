import os
import sys
sys.path.insert(0, os.path.abspath("."))
import json
import pandas as pd
from engine.data_feed import DataFeed
from engine.backtest_engine import BacktestEngine
from engine.strategies import StrategyRegistry

def audit_5000():
    feed = DataFeed()
    candles = feed.get_candles("M15", limit=5000)
    df = pd.DataFrame(candles)

    params = {"swing_strength": 5, "internal_strength": 2, "bias_timing": "pre_candle", "rr_ratio": 2.0}

    df_signals = StrategyRegistry.generate_signals(df.copy(), "smc_confluence", params)

    # 1. Inspect signals
    signal_indices = [i for i, s in enumerate(df_signals["signal"]) if s != 0]
    print(f"Total raw signals: {len(signal_indices)}")

    # 2. Run BacktestEngine with lot_size=0.01 (Protocol V1)
    engine = BacktestEngine(
        initial_capital=10000.0,
        lot_size=0.01,
        contract_size=100.0,
        stop_loss_points=200.0,
        take_profit_points=400.0,
        spread_points=20.0,
        commission_per_lot=5.0,
        allow_short=True
    )
    res = engine.run(df.copy(), "smc_confluence", params)

    print("\n--- RESULTS FOR LOT_SIZE=0.01 ---")
    print("Metrics:", json.dumps(res["metrics"], indent=2))
    print("Telemetry:", json.dumps(res.get("legacy_telemetry"), indent=2))
    print("Total Trades:", len(res["trades"]))

    # 3. Also run for lot_size=0.1 to see exact numbers
    engine_01 = BacktestEngine(
        initial_capital=10000.0,
        lot_size=0.1,
        contract_size=100.0,
        stop_loss_points=200.0,
        take_profit_points=400.0,
        spread_points=20.0,
        commission_per_lot=5.0,
        allow_short=True
    )
    res_01 = engine_01.run(df.copy(), "smc_confluence", params)
    print("\n--- RESULTS FOR LOT_SIZE=0.1 ---")
    print("Metrics:", json.dumps(res_01["metrics"], indent=2))
    print("Telemetry:", json.dumps(res_01.get("legacy_telemetry"), indent=2))
    print("Total Trades:", len(res_01["trades"]))

if __name__ == "__main__":
    audit_5000()
