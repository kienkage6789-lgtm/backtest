import os
import json
import pandas as pd
from engine.backtest_engine import BacktestEngine
from engine.data_feed import DataFeed

os.makedirs('tests/fixtures', exist_ok=True)

feed = DataFeed()
candles = feed.get_candles('H1', limit=250)
df = pd.DataFrame(candles)

strategies = [
    ('sma_crossover', {'fast_period': 10, 'slow_period': 30}, True),
    ('sma_crossover_long_only', {'fast_period': 10, 'slow_period': 30}, False),
    ('rsi_reversal', {'period': 14, 'oversold': 30.0, 'overbought': 70.0}, True),
    ('macd_crossover', {'fast': 12, 'slow': 26, 'signal': 9}, True),
    ('donchian_breakout', {'lookback': 20}, True),
    ('smc_confluence', {'swing_strength': 5, 'internal_strength': 2, 'bias_timing': 'pre_candle'}, True),
]

baseline = {}

for name, params, allow_short in strategies:
    strat_id = 'sma_crossover' if name == 'sma_crossover_long_only' else name
    engine = BacktestEngine(
        initial_capital=10000.0,
        lot_size=0.1,
        contract_size=100.0,
        stop_loss_points=200.0,
        take_profit_points=400.0,
        spread_points=20.0,
        commission_per_lot=5.0,
        allow_short=allow_short
    )
    res = engine.run(df, strat_id, params)

    clean_res = {
        'metrics': res['metrics'],
        'trades': res['trades'],
        'equity_curve': res['equity_curve'],
        'markers': res['markers'],
    }
    if 'smc_objects' in res:
        clean_res['smc_objects'] = res['smc_objects']
    if 'funnel_stats' in res:
        clean_res['funnel_stats'] = res['funnel_stats']

    baseline[name] = clean_res
    print(f"Generated baseline for {name}: {len(clean_res['trades'])} trades")

with open('tests/fixtures/t53_9_2_legacy_baseline.json', 'w', encoding='utf-8') as f:
    json.dump(baseline, f, indent=2, sort_keys=True, default=str)

print("Baseline saved successfully.")
