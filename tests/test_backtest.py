import unittest
import pandas as pd
import math
from engine.backtest_engine import BacktestEngine
from engine.strategies import StrategyRegistry
from engine.data_feed import DataFeed

class TestBacktestEngine(unittest.TestCase):

    def test_01_no_lookahead_execution(self):
        """
        Kiểm tra loại bỏ lookahead bias:
        Tín hiệu Buy sinh ở Bar 0 (dựa trên close Bar 0)
        chỉ được khớp ở Bar 1 tại giá Open của Bar 1, không được khớp tại Bar 0.
        """
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2002.0, 'tick_volume': 100},
            {'time': '2026-01-01 10:01:00', 'open': 2004.0, 'high': 2010.0, 'low': 2003.0, 'close': 2008.0, 'tick_volume': 150},
            {'time': '2026-01-01 10:02:00', 'open': 2008.0, 'high': 2012.0, 'low': 2006.0, 'close': 2010.0, 'tick_volume': 120},
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0, 0])
            engine = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                stop_loss_points=0.0,
                take_profit_points=0.0,
                spread_points=20.0, # 0.20 USD
                commission_per_lot=0.0
            )
            result = engine.run(df, "dummy", {})
            trades = result['trades']

            self.assertEqual(len(trades), 1)
            # Entry phải diễn ra ở Bar 1 với Open=2004.0 + Spread=0.20 = 2004.20
            self.assertEqual(trades[0]['entry_time'], '2026-01-01 10:01:00')
            self.assertAlmostEqual(trades[0]['entry_price'], 2004.20)
            print("\n[PASS] No lookahead verified: Order entered at Bar 1 Open, not Bar 0 Close")
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_02_long_with_spread(self):
        """Kiểm tra tính phí spread cho lệnh Long (vào Ask = Open + spread, thoát Bid)."""
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2000.0, 'low': 2000.0, 'close': 2000.0, 'tick_volume': 100},
            {'time': '2026-01-01 10:01:00', 'open': 2000.0, 'high': 2010.0, 'low': 1999.0, 'close': 2010.0, 'tick_volume': 100},
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0])
            engine = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                stop_loss_points=0.0,
                take_profit_points=0.0,
                spread_points=20.0, # 0.20 USD spread
                commission_per_lot=0.0
            )
            result = engine.run(df, "dummy", {})
            trades = result['trades']
            # Entry = Open(2000.0) + Spread(0.20) = 2000.20
            # Exit = Close(2010.0)
            # PnL = (2010.0 - 2000.20) * 0.1 * 100 = 9.80 * 10 = $98.00
            self.assertAlmostEqual(trades[0]['entry_price'], 2000.20)
            self.assertAlmostEqual(trades[0]['pnl'], 98.00)
            print("[PASS] Long spread calculation verified")
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_03_short_with_spread(self):
        """Kiểm tra tính phí spread đối xứng cho lệnh Short (vào Bid = Open, thoát Ask = Exit Bid + spread)."""
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2000.0, 'low': 2000.0, 'close': 2000.0, 'tick_volume': 100},
            {'time': '2026-01-01 10:01:00', 'open': 2000.0, 'high': 2001.0, 'low': 1990.0, 'close': 1990.0, 'tick_volume': 100},
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[-1, 0])
            engine = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                stop_loss_points=0.0,
                take_profit_points=0.0,
                spread_points=20.0, # 0.20 USD spread
                commission_per_lot=0.0,
                allow_short=True
            )
            result = engine.run(df, "dummy", {})
            trades = result['trades']
            # Entry = Open(2000.0) (Bid)
            # Exit = Close(1990.0) + Spread(0.20) = 1990.20 (Ask)
            # PnL = (2000.0 - 1990.20) * 0.1 * 100 = 9.80 * 10 = $98.00
            self.assertAlmostEqual(trades[0]['entry_price'], 2000.00)
            self.assertAlmostEqual(trades[0]['exit_price'], 1990.20)
            self.assertAlmostEqual(trades[0]['pnl'], 98.00)
            print("[PASS] Short spread symmetric calculation verified")
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_04_short_sl_tp_spread_trigger(self):
        """
        Kiểm tra Short SL và TP trigger theo giá Ask (có xét spread):
        Short vào tại 2000.00 (Bid).
        SL = 500 points ($5.00) -> sl_price = 2005.00 (Ask).
        Spread = 20 points ($0.20).
        Khi Bid High = 2004.90 < 2005.00, nhưng Ask High = 2004.90 + 0.20 = 2005.10 >= 2005.00 -> SL phải kích hoạt!
        """
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2000.0, 'low': 2000.0, 'close': 2000.0, 'tick_volume': 100},
            {'time': '2026-01-01 10:01:00', 'open': 2000.0, 'high': 2004.90, 'low': 1999.0, 'close': 2001.0, 'tick_volume': 100},
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[-1, 0])
            engine = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                stop_loss_points=500.0, # $5.00 -> SL = 2005.00 Ask
                take_profit_points=0.0,
                spread_points=20.0,     # $0.20 spread
                commission_per_lot=0.0,
                allow_short=True
            )
            result = engine.run(df, "dummy", {})
            trades = result['trades']
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]['exit_reason'], 'Stop Loss')
            self.assertAlmostEqual(trades[0]['exit_price'], 2005.00)
            self.assertAlmostEqual(trades[0]['pnl'], -50.0) # (2000.0 - 2005.0) * 0.1 * 100 = -$50
            print("[PASS] Short SL triggered via Ask price with spread considered")
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_05_candle_touching_both_sl_and_tp_prioritizes_sl(self):
        """
        Kiểm tra trường hợp nến có biên độ cực lớn chạm đồng thời cả SL và TP:
        Quy ước bắt buộc: ưu tiên Stop Loss trước (bảo thủ).
        """
        # Long vào tại 2000.0 + 0.20 spread = 2000.20
        # SL = 200 points ($2.00) -> 1998.20
        # TP = 200 points ($2.00) -> 2002.20
        # Nến 2 có Low=1990.0 (chạm SL) và High=2015.0 (chạm TP)
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2000.0, 'low': 2000.0, 'close': 2000.0, 'tick_volume': 100},
            {'time': '2026-01-01 10:01:00', 'open': 2000.0, 'high': 2015.0, 'low': 1990.0, 'close': 2005.0, 'tick_volume': 100},
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0])
            engine = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                stop_loss_points=200.0,
                take_profit_points=200.0,
                spread_points=20.0,
                commission_per_lot=0.0
            )
            result = engine.run(df, "dummy", {})
            trades = result['trades']
            self.assertEqual(len(trades), 1)
            # Ưu tiên SL
            self.assertEqual(trades[0]['exit_reason'], 'Stop Loss')
            self.assertAlmostEqual(trades[0]['exit_price'], 1998.20)
            self.assertAlmostEqual(trades[0]['pnl'], -20.0)
            print("[PASS] Simultaneous SL and TP hit correctly prioritizes SL")
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_06_commission_calculation(self):
        """Kiểm tra tính phí hoa hồng commission 2 chiều (entry + exit)."""
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2000.0, 'low': 2000.0, 'close': 2000.0, 'tick_volume': 100},
            {'time': '2026-01-01 10:01:00', 'open': 2000.0, 'high': 2000.0, 'low': 2000.0, 'close': 2000.0, 'tick_volume': 100},
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0])
            # Commission = 5.0 $/lot. Lot size = 0.1 -> 0.50 $/side -> 1.00 $/round-trip
            engine = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                spread_points=0.0,
                commission_per_lot=5.0
            )
            result = engine.run(df, "dummy", {})
            trades = result['trades']
            self.assertAlmostEqual(trades[0]['pnl'], -1.00)
            self.assertAlmostEqual(result['metrics']['final_balance'], 9999.00)
            print("[PASS] Commission round-trip calculation verified")
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_07_forced_close_updates_all_fields(self):
        """Kiểm tra forced close ở nến cuối cập nhật balance, equity_curve, MDD, markers và trade record."""
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2000.0, 'low': 2000.0, 'close': 2000.0, 'tick_volume': 100},
            {'time': '2026-01-01 10:01:00', 'open': 2000.0, 'high': 2000.0, 'low': 2000.0, 'close': 2000.0, 'tick_volume': 100},
            {'time': '2026-01-01 10:02:00', 'open': 2000.0, 'high': 2000.0, 'low': 1800.0, 'close': 1800.0, 'tick_volume': 100},
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0, 0])
            engine = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                spread_points=0.0,
                commission_per_lot=0.0
            )
            result = engine.run(df, "dummy", {})
            trades = result['trades']
            metrics = result['metrics']
            markers = result['markers']
            equity_curve = result['equity_curve']

            # 1. Trade record
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]['exit_reason'], 'End of Backtest')
            self.assertAlmostEqual(trades[0]['pnl'], -2000.00)

            # 2. Balance
            self.assertAlmostEqual(metrics['final_balance'], 8000.00)

            # 3. MDD và MDD pct
            self.assertAlmostEqual(metrics['max_drawdown'], 2000.00)
            self.assertAlmostEqual(metrics['max_drawdown_pct'], 20.00)

            # 4. Equity curve điểm cuối
            self.assertAlmostEqual(equity_curve[-1]['equity'], 8000.00)
            self.assertAlmostEqual(equity_curve[-1]['balance'], 8000.00)

            # 5. Marker
            exit_markers = [m for m in markers if "EXIT" in m['text']]
            self.assertEqual(len(exit_markers), 1)

            print("[PASS] Forced close fully updated balance, trade record, markers, equity curve and MDD")
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_08_invalid_strategy_and_params(self):
        """Kiểm tra từ chối Strategy ID và params không hợp lệ, bao gồm cả NaN và Infinity."""
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2002.0, 'tick_volume': 100},
            {'time': '2026-01-01 10:01:00', 'open': 2004.0, 'high': 2010.0, 'low': 2003.0, 'close': 2008.0, 'tick_volume': 150},
        ]
        df = pd.DataFrame(candles)
        engine = BacktestEngine()

        # Strategy ID không tồn tại
        with self.assertRaises(ValueError):
            engine.run(df, "unknown_strategy", {})

        # SMA: fast >= slow
        with self.assertRaises(ValueError):
            engine.run(df, "sma_crossover", {"fast_period": 50, "slow_period": 20})

        # SMA: period <= 0
        with self.assertRaises(ValueError):
            engine.run(df, "sma_crossover", {"fast_period": -5, "slow_period": 20})

        # SMA: NaN hoặc Inf
        with self.assertRaises(ValueError):
            engine.run(df, "sma_crossover", {"fast_period": float('nan'), "slow_period": 50})

        with self.assertRaises(ValueError):
            engine.run(df, "sma_crossover", {"fast_period": 20, "slow_period": float('inf')})

        # RSI: period <= 0
        with self.assertRaises(ValueError):
            engine.run(df, "rsi_reversal", {"period": 0})

        # RSI: oversold >= overbought
        with self.assertRaises(ValueError):
            engine.run(df, "rsi_reversal", {"oversold": 80, "overbought": 20})

        # RSI: NaN
        with self.assertRaises(ValueError):
            engine.run(df, "rsi_reversal", {"oversold": float('nan'), "overbought": 70})

        # MACD: fast >= slow
        with self.assertRaises(ValueError):
            engine.run(df, "macd_crossover", {"fast": 30, "slow": 10})

        # Donchian: lookback <= 0
        with self.assertRaises(ValueError):
            engine.run(df, "donchian_breakout", {"lookback": -1})

        print("[PASS] Invalid strategy and parameter rejections (including NaN/Inf) verified")

    def test_09_empty_or_invalid_dataframe(self):
        """Kiểm tra từ chối DataFrame rỗng hoặc thiếu cột bắt buộc."""
        engine = BacktestEngine()

        with self.assertRaises(ValueError):
            engine.run(None, "sma_crossover", {})

        with self.assertRaises(ValueError):
            engine.run(pd.DataFrame(), "sma_crossover", {})

        df_missing = pd.DataFrame([{'time': '2026-01-01', 'open': 100, 'high': 105, 'low': 95}])
        with self.assertRaises(ValueError):
            engine.run(df_missing, "sma_crossover", {})

        df_one = pd.DataFrame([{'time': '2026-01-01', 'open': 100, 'high': 105, 'low': 95, 'close': 100}])
        with self.assertRaises(ValueError):
            engine.run(df_one, "sma_crossover", {})

        print("[PASS] Empty and invalid DataFrames rejected cleanly")

    def test_10_real_data_sma_backtest(self):
        """Chạy backtest trên dữ liệu thật H1."""
        feed = DataFeed()
        candles = feed.get_candles('H1', limit=500)
        df = pd.DataFrame(candles)

        engine = BacktestEngine(
            initial_capital=10000.0,
            lot_size=0.1,
            stop_loss_points=300.0,
            take_profit_points=600.0,
            spread_points=20.0,
            commission_per_lot=5.0
        )
        result = engine.run(df, "sma_crossover", {"fast_period": 10, "slow_period": 30})

        metrics = result['metrics']
        self.assertIn('net_profit', metrics)
        self.assertIn('win_rate', metrics)
        self.assertIn('profit_factor', metrics)
        self.assertIn('max_drawdown', metrics)
        self.assertGreater(metrics['total_trades'], 0)
        self.assertGreater(len(result['equity_curve']), 0)
        self.assertGreater(len(result['markers']), 0)

        print(f"[PASS] Real data H1 backtest completed: {metrics['total_trades']} trades, Net PnL: ${metrics['net_profit']:.2f}, MDD: {metrics['max_drawdown_pct']}%")

if __name__ == '__main__':
    unittest.main()
