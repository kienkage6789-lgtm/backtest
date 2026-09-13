import pandas as pd
from engine.data_feed import DataFeed
from engine.backtest_engine import BacktestEngine

feed = DataFeed('data/XAUUSD.db')
candles = feed.get_candles('H1', limit=250)
df = pd.DataFrame(candles)

for rr in [1.0, 1.5, 2.0, 3.0]:
    engine = BacktestEngine(initial_capital=10000.0, lot_size=0.1)
    res = engine.run(df, 'smc_confluence', {'swing_strength': 5, 'internal_strength': 2, 'bias_timing': 'pre_candle', 'rr_ratio': rr})
    tr = res['trades'][0]
    print(f"RR={rr:.1f} -> planned_tp={tr['planned_take_profit']}, planned_sl={tr['planned_stop_loss']}, exit={tr['exit_price']}, pnl={tr['pnl']}")
