import os
import sys
sys.path.insert(0, os.path.abspath("."))
import json
import pandas as pd
from engine.data_feed import DataFeed
from engine.backtest_engine import BacktestEngine
from engine.strategies import StrategyRegistry

def trace_signals():
    feed = DataFeed()
    candles = feed.get_candles("M15", limit=5000)
    df = pd.DataFrame(candles)

    params = {"swing_strength": 5, "internal_strength": 2, "bias_timing": "pre_candle", "rr_ratio": 2.0}
    df_signals = StrategyRegistry.generate_signals(df.copy(), "smc_confluence", params)

    signals = df_signals["signal"].tolist()
    opens = df_signals["open"].tolist()
    sls = df_signals["planned_stop_loss"].tolist()
    tps = df_signals["planned_take_profit"].tolist()

    sig_count = 0
    planned_used = 0
    rejected = 0

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

    # Let's run and capture transitions per bar
    kernel = engine.kernel = engine._run_legacy.__globals__["ExecutionKernel"](
        initial_capital=10000.0,
        lot_size=0.01,
        contract_size=100.0,
        spread_val=engine.spread_val,
        commission_per_side=engine.commission_per_side,
        validation_mode="legacy"
    )

    events = []
    for i in range(len(df_signals)):
        prev_sig = signals[i - 1] if i > 0 else 0
        if i > 0 and prev_sig != 0:
            sig_count += 1
            raw_sl = sls[i - 1]
            raw_tp = tps[i - 1]
            o = opens[i]
            entry_p = o + engine.spread_val if prev_sig == 1 else o
            valid_geo = False
            if prev_sig == 1:
                valid_geo = float(raw_sl) < entry_p < float(raw_tp)
            else:
                valid_geo = float(raw_tp) < entry_p < float(raw_sl)

            if valid_geo:
                planned_used += 1
                instr = engine._run_legacy.__globals__["OpenInstruction"](
                    action="OPEN_OR_REVERSE",
                    direction="BUY" if prev_sig == 1 else "SELL",
                    entry_price=entry_p,
                    sl_price=float(raw_sl),
                    tp_price=float(raw_tp),
                    source="legacy"
                )
                bar = engine._run_legacy.__globals__["ExecutionBar"](
                    bar_index=i,
                    timestamp=int(pd.to_datetime(df_signals['time'].iloc[i]).timestamp()),
                    time_value=str(df_signals['time'].iloc[i]),
                    open=o,
                    high=df_signals['high'].iloc[i],
                    low=df_signals['low'].iloc[i],
                    close=df_signals['close'].iloc[i]
                )
                trans = kernel.process_open(bar, instr)
                events.append({
                    "bar": i,
                    "type": "PLANNED_INSTRUCTION",
                    "direction": instr.direction,
                    "kernel_status": trans.status
                })
            else:
                rejected += 1
                events.append({
                    "bar": i,
                    "type": "REJECTED_GEOMETRY",
                    "direction": "BUY" if prev_sig == 1 else "SELL"
                })

        # Process intrabar
        bar = engine._run_legacy.__globals__["ExecutionBar"](
            bar_index=i,
            timestamp=int(pd.to_datetime(df_signals['time'].iloc[i]).timestamp()),
            time_value=str(df_signals['time'].iloc[i]),
            open=opens[i],
            high=df_signals['high'].iloc[i],
            low=df_signals['low'].iloc[i],
            close=df_signals['close'].iloc[i]
        )
        kernel.process_intrabar(bar)

    print(f"Total signals: {sig_count}")
    print(f"Planned instructions: {planned_used}")
    print(f"Rejected geometry: {rejected}")
    status_counts = {}
    for e in events:
        s = e.get("kernel_status", e["type"])
        status_counts[s] = status_counts.get(s, 0) + 1
    print("Kernel status breakdown:", json.dumps(status_counts, indent=2))
    print(f"Total closed trades: {len(kernel.trades)}")

if __name__ == "__main__":
    trace_signals()
