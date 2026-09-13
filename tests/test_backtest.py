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

    def test_11_two_way_reversal_in_open(self):
        """Kiểm tra reversal hai chiều BUY->SELL và SELL->BUY tại Open."""
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
            {'time': '2026-01-01 10:01:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
            {'time': '2026-01-01 10:02:00', 'open': 2010.0, 'high': 2015.0, 'low': 2005.0, 'close': 2010.0},
            {'time': '2026-01-01 10:03:00', 'open': 2005.0, 'high': 2010.0, 'low': 2000.0, 'close': 2005.0},
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            # Bar 0: BUY signal (1) -> Bar 1: opens BUY at 2000.20
            # Bar 1: SELL signal (-1) -> Bar 2: closes BUY at 2010.00, opens SELL at 2010.00
            # Bar 2: BUY signal (1) -> Bar 3: closes SELL at 2005.20 (Ask), opens BUY at 2005.20
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, -1, 1, 0])
            engine = BacktestEngine(spread_points=20.0, commission_per_lot=0.0)
            res = engine.run(df, "dummy", {})
            trades = res['trades']

            self.assertEqual(len(trades), 3) # 2 reversals + 1 forced close
            self.assertEqual(trades[0]['type'], 'BUY')
            self.assertEqual(trades[0]['exit_reason'], 'Signal Reversal')
            self.assertEqual(trades[1]['type'], 'SELL')
            self.assertEqual(trades[1]['exit_reason'], 'Signal Reversal')
            self.assertEqual(trades[2]['type'], 'BUY')
            self.assertEqual(trades[2]['exit_reason'], 'End of Backtest')
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_12_repeated_same_direction_no_pyramiding(self):
        """Kiểm tra lặp lại tín hiệu cùng chiều không mở thêm lệnh (không pyramiding)."""
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
            {'time': '2026-01-01 10:01:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
            {'time': '2026-01-01 10:02:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
            {'time': '2026-01-01 10:03:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 1, 1, 0])
            engine = BacktestEngine(spread_points=20.0, commission_per_lot=0.0)
            res = engine.run(df, "dummy", {})
            # Chỉ có duy nhất 1 trade mở tại bar 1 và đóng tại bar 3 forced close
            self.assertEqual(len(res['trades']), 1)
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_13_sell_when_short_disabled_closes_long_no_short(self):
        """Kiểm tra SELL khi allow_short=False đóng LONG theo semantics legacy nhưng không mở SHORT."""
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
            {'time': '2026-01-01 10:01:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
            {'time': '2026-01-01 10:02:00', 'open': 2010.0, 'high': 2015.0, 'low': 2005.0, 'close': 2010.0},
            {'time': '2026-01-01 10:03:00', 'open': 2005.0, 'high': 2010.0, 'low': 2000.0, 'close': 2005.0},
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            # Bar 0: BUY (1) -> Bar 1: opens BUY
            # Bar 1: SELL (-1) -> Bar 2: closes BUY, does NOT open SHORT
            # Bar 2: SELL (-1) -> Bar 3: flat, does nothing
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, -1, -1, 0])
            engine = BacktestEngine(allow_short=False, spread_points=20.0, commission_per_lot=0.0)
            res = engine.run(df, "dummy", {})

            trades = res['trades']
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]['type'], 'BUY')
            self.assertEqual(trades[0]['exit_reason'], 'Signal Reversal')
            # Đảm bảo sau đó flat, không có trade SHORT nào
            self.assertIsNone(engine.kernel.position)
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_14_dynamic_position_trigger_without_global_guard(self):
        """Chứng minh dynamic SL/TP trên PositionState kích hoạt khi global fixed levels bằng 0."""
        from engine.execution_kernel import ExecutionBar, OpenInstruction
        engine = BacktestEngine(stop_loss_points=0.0, take_profit_points=0.0)
        # Directly process a dynamic instruction with SL/TP through the engine's kernel
        bar1 = ExecutionBar(bar_index=1, timestamp=1000, time_value="t1", open=2000.0, high=2005.0, low=1995.0, close=2000.0)
        inst = OpenInstruction(
            action="OPEN_OR_REVERSE",
            direction="BUY",
            entry_price=2000.20,
            sl_price=1995.0,
            tp_price=2015.0,
            source="wave1"
        )
        engine.kernel.process_open(bar1, inst)
        self.assertIsNotNone(engine.kernel.position)

        bar2 = ExecutionBar(bar_index=2, timestamp=2000, time_value="t2", open=1998.0, high=2002.0, low=1994.0, close=1996.0)
        trans = engine.kernel.process_intrabar(bar2)
        # Must trigger SL dynamically even though engine.stop_loss_val == 0.0
        self.assertEqual(trans.status, "STOPPED")
        self.assertEqual(len(engine.kernel.trades), 1)
        self.assertEqual(engine.kernel.trades[0]['exit_reason'], "Stop Loss")

    def test_15_result_exact_key_schema(self):
        """Kiểm tra schema khóa chính xác của kết quả backtest."""
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
            {'time': '2026-01-01 10:01:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
        ]
        df = pd.DataFrame(candles)
        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[0, 0])
            engine = BacktestEngine()
            res = engine.run(df, "dummy", {})
            self.assertEqual(set(res.keys()), {"metrics", "trades", "equity_curve", "markers"})
            required_metrics = {
                "initial_capital", "final_balance", "net_profit", "return_pct",
                "total_trades", "winning_trades", "losing_trades", "win_rate",
                "profit_factor", "max_drawdown", "max_drawdown_pct", "gross_profit", "gross_loss"
            }
            self.assertEqual(set(res["metrics"].keys()), required_metrics)
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_16_multiple_runs_on_same_engine_no_leak(self):
        """Kiểm tra chạy 2 lần trên cùng một instance BacktestEngine không bị rò rỉ state."""
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
            {'time': '2026-01-01 10:01:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
            {'time': '2026-01-01 10:02:00', 'open': 2000.0, 'high': 2005.0, 'low': 1995.0, 'close': 2000.0},
        ]
        df = pd.DataFrame(candles)
        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0, 0])
            engine = BacktestEngine()
            res1 = engine.run(df, "dummy", {})
            res2 = engine.run(df, "dummy", {})

            self.assertEqual(res1['metrics']['final_balance'], res2['metrics']['final_balance'])
            self.assertEqual(len(res1['trades']), len(res2['trades']))
            self.assertEqual(res2['trades'][0]['trade_id'], 1)
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_17_legacy_price_around_one_negative_sl_tp(self):
        """
        Kiểm tra ranh giới legacy: giá tài sản quanh 1.0 với fixed SL/TP lớn
        khiến SL hoặc TP tính ra số âm. BacktestEngine.run() không được ném lỗi
        và phải forced close ở nến cuối khớp hành vi legacy.
        """
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 1.0, 'high': 1.2, 'low': 0.8, 'close': 1.0},
            {'time': '2026-01-01 10:01:00', 'open': 1.0, 'high': 1.2, 'low': 0.8, 'close': 1.1},
            {'time': '2026-01-01 10:02:00', 'open': 1.1, 'high': 1.3, 'low': 0.9, 'close': 1.2},
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            # 1. BUY với stop_loss_points = 200.0 -> SL = 1.0 - 2.0 = -1.0
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0, 0])
            engine_buy = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                spread_points=0.0,
                stop_loss_points=200.0,
                take_profit_points=0.0,
                commission_per_lot=0.0,
            )
            res_buy = engine_buy.run(df, "dummy", {})
            trades_buy = res_buy['trades']
            self.assertEqual(len(trades_buy), 1)
            self.assertEqual(trades_buy[0]['type'], 'BUY')
            self.assertEqual(trades_buy[0]['exit_reason'], 'End of Backtest')
            self.assertAlmostEqual(trades_buy[0]['entry_price'], 1.0)
            self.assertAlmostEqual(trades_buy[0]['exit_price'], 1.2)
            self.assertAlmostEqual(trades_buy[0]['pnl'], 2.0)
            self.assertAlmostEqual(res_buy['metrics']['final_balance'], 10002.0)

            # 2. SELL với take_profit_points = 200.0 -> TP = 1.0 - 2.0 = -1.0
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[-1, 0, 0])
            engine_sell = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                spread_points=0.0,
                stop_loss_points=0.0,
                take_profit_points=200.0,
                commission_per_lot=0.0,
                allow_short=True,
            )
            res_sell = engine_sell.run(df, "dummy", {})
            trades_sell = res_sell['trades']
            self.assertEqual(len(trades_sell), 1)
            self.assertEqual(trades_sell[0]['type'], 'SELL')
            self.assertEqual(trades_sell[0]['exit_reason'], 'End of Backtest')
            self.assertAlmostEqual(trades_sell[0]['entry_price'], 1.0)
            self.assertAlmostEqual(trades_sell[0]['exit_price'], 1.2)
            # Short entry 1.0, exit 1.2 -> (1.0 - 1.2) * 10 = -2.0
            self.assertAlmostEqual(trades_sell[0]['pnl'], -2.0)
            self.assertAlmostEqual(res_sell['metrics']['final_balance'], 9998.0)
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_18_legacy_constructor_boundary_parity(self):
        """
        Kiểm tra ranh giới constructor facade legacy:
        Trước refactor, BacktestEngine chấp nhận initial_capital=0, lot_size=0,
        contract_size=0, spread_points < 0, commission_per_lot < 0 mà không bị lỗi sớm.
        Đảm bảo không phát sinh lỗi sớm tại constructor.
        """
        engine = BacktestEngine(
            initial_capital=0.0,
            lot_size=0.0,
            contract_size=0.0,
            spread_points=-20.0,
            commission_per_lot=-5.0,
            allow_short=True,
        )
        self.assertEqual(engine.initial_capital, 0.0)
        self.assertEqual(engine.lot_size, 0.0)
        self.assertEqual(engine.contract_size, 0.0)
        self.assertEqual(engine.spread_val, -0.20)
        self.assertEqual(engine.commission_per_side, 0.0)
        self.assertIsNotNone(engine.kernel)

    def test_19_legacy_coerced_zero_and_negative_open(self):
        """
        Kiểm tra khả năng tương thích của facade legacy khi dữ liệu open chứa:
        1. Giá trị không phải số ('bad') bị coerce thành 0.0
        2. Mức giá finite âm (ví dụ -10.0)
        Cả hai trường hợp phải chạy xuyên suốt tới forced close mà không bị từ chối sớm.
        """
        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0, 0])

            # 1. Open tại nến vào lệnh bị coerce thành 0.0
            candles_coerced = [
                {'time': '2026-01-01 10:00:00', 'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 100.0},
                {'time': '2026-01-01 10:01:00', 'open': 'bad', 'high': 5.0, 'low': -1.0, 'close': 2.0},
                {'time': '2026-01-01 10:02:00', 'open': 2.0, 'high': 5.0, 'low': 0.0, 'close': 3.0},
            ]
            df_coerced = pd.DataFrame(candles_coerced)
            engine1 = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                spread_points=0.0,
                stop_loss_points=200.0,  # SL = 0.0 - 2.0 = -2.0 (không chạm vì low = -1.0)
                take_profit_points=0.0,
                commission_per_lot=0.0,
            )
            res1 = engine1.run(df_coerced, "dummy", {})
            self.assertEqual(len(res1['trades']), 1)
            self.assertEqual(res1['trades'][0]['entry_price'], 0.0)
            self.assertEqual(res1['trades'][0]['exit_price'], 3.0)
            self.assertEqual(res1['trades'][0]['exit_reason'], 'End of Backtest')
            self.assertAlmostEqual(res1['trades'][0]['pnl'], 30.0)

            # 2. Open và các mức giá âm finite
            candles_neg = [
                {'time': '2026-01-01 10:00:00', 'open': -10.0, 'high': -5.0, 'low': -15.0, 'close': -10.0},
                {'time': '2026-01-01 10:01:00', 'open': -10.0, 'high': -5.0, 'low': -15.0, 'close': -8.0},
                {'time': '2026-01-01 10:02:00', 'open': -8.0, 'high': -5.0, 'low': -12.0, 'close': -7.0},
            ]
            df_neg = pd.DataFrame(candles_neg)
            engine2 = BacktestEngine(
                initial_capital=10000.0,
                lot_size=0.1,
                contract_size=100.0,
                spread_points=0.0,
                stop_loss_points=0.0,
                take_profit_points=0.0,
                commission_per_lot=0.0,
            )
            res2 = engine2.run(df_neg, "dummy", {})
            self.assertEqual(len(res2['trades']), 1)
            self.assertEqual(res2['trades'][0]['entry_price'], -10.0)
            self.assertEqual(res2['trades'][0]['exit_reason'], 'End of Backtest')
            # Entry -10.0, exit -7.0 -> PnL = (-7.0 - (-10.0)) * 10 = +30.0
            self.assertAlmostEqual(res2['trades'][0]['pnl'], 30.0)
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_20_negative_initial_capital_return_pct(self):
        """
        Kiểm tra công thức return_pct khi initial_capital là số âm trong legacy mode:
        1. initial_capital = -100.0, net_profit = +1.0 -> return_pct = -1.0
        2. initial_capital = -100.0, net_profit = -1.0 -> return_pct = +1.0
        3. initial_capital = 0.0 -> return_pct = 0.0 (không chia cho 0)
        """
        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0, 0])
            candles = [
                {'time': '2026-01-01 10:00:00', 'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 100.0},
                {'time': '2026-01-01 10:01:00', 'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 100.0},
                {'time': '2026-01-01 10:02:00', 'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 100.10},
            ]
            df = pd.DataFrame(candles)

            # Case 1: capital = -100.0, net_profit = +1.0
            engine_neg = BacktestEngine(
                initial_capital=-100.0,
                lot_size=0.1,
                contract_size=100.0,
                spread_points=0.0,
                commission_per_lot=0.0,
            )
            res1 = engine_neg.run(df, "dummy", {})
            self.assertEqual(len(res1["trades"]), 1)
            self.assertAlmostEqual(res1["trades"][0]["pnl"], 1.0)
            self.assertEqual(res1["trades"][0]["return_pct"], -1.0)
            self.assertAlmostEqual(res1["metrics"]["net_profit"], 1.0)
            self.assertEqual(res1["metrics"]["return_pct"], -1.0)

            # Case 2: capital = -100.0, net_profit = -1.0
            candles_loss = [
                {'time': '2026-01-01 10:00:00', 'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 100.0},
                {'time': '2026-01-01 10:01:00', 'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 100.0},
                {'time': '2026-01-01 10:02:00', 'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 99.90},
            ]
            df_loss = pd.DataFrame(candles_loss)
            engine_neg2 = BacktestEngine(
                initial_capital=-100.0,
                lot_size=0.1,
                contract_size=100.0,
                spread_points=0.0,
                commission_per_lot=0.0,
            )
            res2 = engine_neg2.run(df_loss, "dummy", {})
            self.assertEqual(len(res2["trades"]), 1)
            self.assertAlmostEqual(res2["trades"][0]["pnl"], -1.0)
            self.assertEqual(res2["trades"][0]["return_pct"], 1.0)
            self.assertAlmostEqual(res2["metrics"]["net_profit"], -1.0)
            self.assertEqual(res2["metrics"]["return_pct"], 1.0)

            # Case 3: capital = 0.0 -> return_pct == 0.0
            engine_zero = BacktestEngine(
                initial_capital=0.0,
                lot_size=0.1,
                contract_size=100.0,
                spread_points=0.0,
                commission_per_lot=0.0,
            )
            res3 = engine_zero.run(df, "dummy", {})
            self.assertEqual(res3["metrics"]["return_pct"], 0.0)
            self.assertEqual(res3["trades"][0]["return_pct"], 0.0)
        finally:
            StrategyRegistry.generate_signals = original_gen

    def test_21_arithmetic_overflow_rejected_cleanly(self):
        """Kiểm tra BacktestEngine từ chối overflow số học mà không commit state hỏng."""
        candles = [
            {'time': '2026-01-01 10:00:00', 'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 100.0},
            {'time': '2026-01-01 10:01:00', 'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 100.0},
            {'time': '2026-01-01 10:02:00', 'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 110.0},
        ]
        df = pd.DataFrame(candles)
        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0, 0])
            engine_overflow = BacktestEngine(
                initial_capital=1000.0,
                lot_size=1e308,
                contract_size=1.0,
                spread_points=0.0,
                commission_per_lot=0.0,
            )
            with self.assertRaises(ValueError) as ctx:
                engine_overflow.run(df, "dummy", {})
            self.assertIn("must be finite", str(ctx.exception))
        finally:
            StrategyRegistry.generate_signals = original_gen


if __name__ == '__main__':
    unittest.main()
