"""
tests/test_smc_confluence_planned_sltp.py
-----------------------------------------
Unit tests verifying Planned SL/TP wiring from smc_confluence to legacy ExecutionKernel.
Covers the 9 mandatory test scenarios from Plan T54.1.x:
1. Planned SL/TP correctly passed to OpenInstruction
2. RR ratio dynamically changes Take Profit while SL remains stable
3. Geometry check on BUY orders
4. Geometry check on SELL orders
5. Geometry violation at actual fill results in fail-closed rejection
6. Fallback legacy mechanism for strategies without planned levels
7. Zero lookahead audit (signal N executes Open N+1 using levels N)
8. Multi-candidate determinism on the same bar
9. Zero regression on Wave 1 pipeline
"""

import math
from typing import Any, Dict, List
import unittest
import numpy as np
import pandas as pd

from engine.backtest_engine import BacktestEngine
from engine.data_feed import DataFeed
from engine.execution_kernel import ExecutionKernel, ExecutionBar, OpenInstruction
from engine.strategies import StrategyRegistry
from smc.strategy import run_smc_strategy, SMCStrategyConfig, SMCStrategyResult


def make_candle(t_str: str, o: float, h: float, l: float, c: float) -> Dict[str, Any]:
    return {"time": t_str, "open": o, "high": h, "low": l, "close": c, "tick_volume": 100}


class TestSMCConfluencePlannedSLTP(unittest.TestCase):
    # -----------------------------------------------------------------
    # Test 1: Planned SL/TP được truyền đúng qua pipeline
    # -----------------------------------------------------------------
    def test_01_planned_sltp_passed_to_instruction(self):
        """Xác nhận Planned SL/TP từ cột DataFrame được truyền chính xác vào OpenInstruction."""
        candles = [
            make_candle("2026-01-01 10:00:00", 2000.0, 2010.0, 1995.0, 2005.0),
            make_candle("2026-01-01 10:01:00", 2002.0, 2015.0, 2000.0, 2010.0),
            make_candle("2026-01-01 10:02:00", 2010.0, 2025.0, 2008.0, 2020.0),
        ]
        df = pd.DataFrame(candles)

        # Mock StrategyRegistry.generate_signals to emit a BUY signal with planned levels
        original_gen = StrategyRegistry.generate_signals
        try:
            def mock_gen(d, s, p):
                out = d.copy()
                out["signal"] = [1, 0, 0]
                out["planned_entry_price"] = [2005.0, np.nan, np.nan]
                out["planned_stop_loss"] = [1990.0, np.nan, np.nan]
                out["planned_take_profit"] = [2020.0, np.nan, np.nan]
                out["planned_rr"] = [1.5, np.nan, np.nan]
                return out

            StrategyRegistry.generate_signals = mock_gen

            engine = BacktestEngine(spread_points=0.0, commission_per_lot=0.0)
            res = engine.run(df, "smc_confluence", {})

            self.assertEqual(len(res["trades"]), 1)
            trade = res["trades"][0]
            self.assertEqual(trade["type"], "BUY")
            self.assertEqual(trade["entry_price"], 2002.0)  # Open of bar 1
            self.assertEqual(trade["planned_stop_loss"], 1990.0)
            self.assertEqual(trade["planned_take_profit"], 2020.0)
            self.assertEqual(trade["planned_rr"], 1.5)
            self.assertEqual(trade["sl_tp_source"], "strategy_planned")
            self.assertEqual(trade["exit_price"], 2020.0)  # Bar 2 high 2025 >= 2020
            self.assertEqual(trade["exit_reason"], "Take Profit")
            self.assertEqual(res["legacy_telemetry"]["planned_levels_used"], 1)
            self.assertEqual(res["legacy_telemetry"]["fallback_levels_used"], 0)
        finally:
            StrategyRegistry.generate_signals = original_gen

    # -----------------------------------------------------------------
    # Test 2: RR thực sự thay đổi Take Profit
    # -----------------------------------------------------------------
    def test_02_rr_ratio_dynamically_changes_tp(self):
        """Xác nhận các mức rr_ratio khác nhau tạo ra TP khác nhau và SL không đổi."""
        feed = DataFeed("data/XAUUSD.db")
        candles = feed.get_candles("H1", limit=250)
        df = pd.DataFrame(candles)

        tp_levels = {}
        sl_levels = {}

        for rr in [1.0, 2.0, 3.0]:
            cfg = SMCStrategyConfig(
                swing_strength=5,
                internal_strength=2,
                bias_timing="pre_candle",
                rr_ratio=rr
            )
            res = run_smc_strategy(df, cfg)
            sig_indices = df.index[res.signals != 0].tolist()
            self.assertGreater(len(sig_indices), 0, "Phải có ít nhất 1 signal để kiểm tra")
            first_sig_idx = sig_indices[0]

            tp = res.planned_take_profits.iloc[first_sig_idx]
            sl = res.planned_stop_losses.iloc[first_sig_idx]
            tp_levels[rr] = tp
            sl_levels[rr] = sl

        # Verify TP_1 != TP_2 != TP_3
        self.assertNotEqual(tp_levels[1.0], tp_levels[2.0])
        self.assertNotEqual(tp_levels[2.0], tp_levels[3.0])
        self.assertNotEqual(tp_levels[1.0], tp_levels[3.0])

        # Verify SL is constant
        self.assertEqual(sl_levels[1.0], sl_levels[2.0])
        self.assertEqual(sl_levels[2.0], sl_levels[3.0])

    # -----------------------------------------------------------------
    # Test 3: Geometry BUY (planned_sl < actual_entry < planned_tp)
    # -----------------------------------------------------------------
    def test_03_geometry_buy_valid(self):
        """Kiểm tra lệnh BUY hợp lệ được chấp nhận khi planned_sl < actual_entry < planned_tp."""
        candles = [
            make_candle("2026-01-01 10:00:00", 2000.0, 2010.0, 1995.0, 2005.0),
            make_candle("2026-01-01 10:01:00", 2000.0, 2015.0, 1995.0, 2010.0),  # actual entry = 2000 + 0.20 = 2000.20
            make_candle("2026-01-01 10:02:00", 2010.0, 2030.0, 2008.0, 2025.0),
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(
                signal=[1, 0, 0],
                planned_entry_price=[2005.0, np.nan, np.nan],
                planned_stop_loss=[1990.0, np.nan, np.nan],
                planned_take_profit=[2020.0, np.nan, np.nan],
                planned_rr=[2.0, np.nan, np.nan],
            )
            engine = BacktestEngine(spread_points=20.0, commission_per_lot=5.0)
            res = engine.run(df, "smc_confluence", {})

            self.assertEqual(len(res["trades"]), 1)
            tr = res["trades"][0]
            self.assertEqual(tr["type"], "BUY")
            self.assertEqual(tr["sl_tp_source"], "strategy_planned")
            self.assertTrue(tr["planned_stop_loss"] < tr["actual_entry_price"] < tr["planned_take_profit"])
            self.assertEqual(res["legacy_telemetry"]["planned_levels_used"], 1)
            self.assertEqual(res["legacy_telemetry"]["rejected_invalid_geometry"], 0)
        finally:
            StrategyRegistry.generate_signals = original_gen

    # -----------------------------------------------------------------
    # Test 4: Geometry SELL (planned_tp < actual_entry < planned_sl)
    # -----------------------------------------------------------------
    def test_04_geometry_sell_valid(self):
        """Kiểm tra lệnh SELL hợp lệ được chấp nhận khi planned_tp < actual_entry < planned_sl."""
        candles = [
            make_candle("2026-01-01 10:00:00", 2000.0, 2010.0, 1995.0, 2005.0),
            make_candle("2026-01-01 10:01:00", 2000.0, 2005.0, 1980.0, 1985.0),  # actual entry = 2000.0 (Bid)
            make_candle("2026-01-01 10:02:00", 1985.0, 1990.0, 1975.0, 1980.0),
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(
                signal=[-1, 0, 0],
                planned_entry_price=[2005.0, np.nan, np.nan],
                planned_stop_loss=[2015.0, np.nan, np.nan],
                planned_take_profit=[1985.0, np.nan, np.nan],
                planned_rr=[2.0, np.nan, np.nan],
            )
            engine = BacktestEngine(spread_points=20.0, commission_per_lot=5.0)
            res = engine.run(df, "smc_confluence", {})

            self.assertEqual(len(res["trades"]), 1)
            tr = res["trades"][0]
            self.assertEqual(tr["type"], "SELL")
            self.assertEqual(tr["sl_tp_source"], "strategy_planned")
            self.assertTrue(tr["planned_take_profit"] < tr["actual_entry_price"] < tr["planned_stop_loss"])
            self.assertEqual(res["legacy_telemetry"]["planned_levels_used"], 1)
            self.assertEqual(res["legacy_telemetry"]["rejected_invalid_geometry"], 0)
        finally:
            StrategyRegistry.generate_signals = original_gen

    # -----------------------------------------------------------------
    # Test 5: Actual fill làm geometry sai -> Fail-closed
    # -----------------------------------------------------------------
    def test_05_actual_fill_violates_geometry_fail_closed(self):
        """Nếu giá Open nến N+1 nhảy gap vi phạm Planned SL/TP, lệnh bị hủy fail-closed."""
        candles = [
            make_candle("2026-01-01 10:00:00", 2000.0, 2010.0, 1995.0, 2005.0),
            # Gap up vượt qua cả Planned TP (2020) -> Open = 2025.0
            make_candle("2026-01-01 10:01:00", 2025.0, 2030.0, 2020.0, 2028.0),
            make_candle("2026-01-01 10:02:00", 2028.0, 2035.0, 2025.0, 2030.0),
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(
                signal=[1, 0, 0],
                planned_entry_price=[2005.0, np.nan, np.nan],
                planned_stop_loss=[1990.0, np.nan, np.nan],
                planned_take_profit=[2020.0, np.nan, np.nan],
                planned_rr=[1.5, np.nan, np.nan],
            )
            engine = BacktestEngine(spread_points=20.0, commission_per_lot=5.0)
            res = engine.run(df, "smc_confluence", {})

            # Lệnh không được mở!
            self.assertEqual(len(res["trades"]), 0)
            self.assertEqual(res["legacy_telemetry"]["planned_levels_used"], 0)
            self.assertEqual(res["legacy_telemetry"]["rejected_invalid_geometry"], 1)
        finally:
            StrategyRegistry.generate_signals = original_gen

    # -----------------------------------------------------------------
    # Test 6: Fallback legacy mechanism
    # -----------------------------------------------------------------
    def test_06_fallback_legacy_for_strategies_without_planned_levels(self):
        """Chiến lược không có planned SL/TP (ví dụ SMA crossover) vẫn dùng fallback points bình thường."""
        candles = [
            make_candle("2026-01-01 10:00:00", 2000.0, 2005.0, 1995.0, 2000.0),
            make_candle("2026-01-01 10:01:00", 2000.0, 2010.0, 1990.0, 2005.0),
            make_candle("2026-01-01 10:02:00", 2005.0, 2015.0, 1980.0, 1985.0),  # Drops to 1980, hitting SL 1990
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            # 1. Non-SMC strategy: uses fallback points, retains pure legacy schema
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(signal=[1, 0, 0])
            engine = BacktestEngine(
                stop_loss_points=1000.0,   # 10 USD SL -> 2000 - 10 = 1990
                take_profit_points=2000.0, # 20 USD TP
                spread_points=0.0,
                commission_per_lot=0.0
            )
            res = engine.run(df, "sma_crossover", {})

            self.assertEqual(len(res["trades"]), 1)
            trade = res["trades"][0]
            self.assertEqual(trade["exit_price"], 1990.0)
            self.assertEqual(trade["exit_reason"], "Stop Loss")
            self.assertNotIn("sl_tp_source", trade)
            self.assertNotIn("legacy_telemetry", res)  # non-SMC retains exact root keys

            # 2. SMC strategy with missing planned SL/TP: uses fallback points AND records telemetry
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(
                signal=[1, 0, 0],
                planned_stop_loss=[None, None, None],
                planned_take_profit=[None, None, None],
                planned_entry_price=[None, None, None],
                planned_rr=[None, None, None]
            )
            res_smc = engine.run(df, "smc_confluence", {})
            self.assertEqual(len(res_smc["trades"]), 1)
            trade_smc = res_smc["trades"][0]
            self.assertEqual(trade_smc["exit_price"], 1990.0)
            self.assertEqual(trade_smc["exit_reason"], "Stop Loss")
            self.assertEqual(trade_smc["sl_tp_source"], "engine_fallback")
            self.assertEqual(res_smc["legacy_telemetry"]["fallback_levels_used"], 1)
        finally:
            StrategyRegistry.generate_signals = original_gen

    # -----------------------------------------------------------------
    # Test 7: Zero lookahead audit
    # -----------------------------------------------------------------
    def test_07_zero_lookahead_audit(self):
        """Signal tại bar N chỉ đọc Planned SL/TP của bar N và khớp ở Open bar N+1."""
        candles = [
            make_candle("2026-01-01 10:00:00", 2000.0, 2010.0, 1995.0, 2005.0),  # bar 0
            make_candle("2026-01-01 10:01:00", 2005.0, 2012.0, 1998.0, 2008.0),  # bar 1: signal emitted
            make_candle("2026-01-01 10:02:00", 2008.0, 2025.0, 2006.0, 2020.0),  # bar 2: fill at Open 2008
        ]
        df = pd.DataFrame(candles)

        original_gen = StrategyRegistry.generate_signals
        try:
            StrategyRegistry.generate_signals = lambda d, s, p: d.assign(
                signal=[0, 1, 0],
                planned_entry_price=[np.nan, 2008.0, np.nan],
                planned_stop_loss=[np.nan, 1995.0, np.nan],
                planned_take_profit=[np.nan, 2020.0, np.nan],
                planned_rr=[np.nan, 1.5, np.nan],
            )
            engine = BacktestEngine(spread_points=0.0, commission_per_lot=0.0)
            res = engine.run(df, "smc_confluence", {})

            self.assertEqual(len(res["trades"]), 1)
            tr = res["trades"][0]
            # Entry must be at Open of bar 2 (2008.0)
            self.assertEqual(tr["entry_price"], 2008.0)
            self.assertEqual(tr["planned_stop_loss"], 1995.0)
            self.assertEqual(tr["planned_take_profit"], 2020.0)
        finally:
            StrategyRegistry.generate_signals = original_gen

    # -----------------------------------------------------------------
    # Test 8: Multi-candidate cùng bar
    # -----------------------------------------------------------------
    def test_08_multi_candidate_same_bar_integrity(self):
        """Khi có nhiều candidate cùng bar, signal và planned levels phải thuộc về cùng 1 order."""
        feed = DataFeed("data/XAUUSD.db")
        candles = feed.get_candles("H1", limit=500)
        df = pd.DataFrame(candles)

        res = run_smc_strategy(df, SMCStrategyConfig(max_ranked_fvgs=5))
        sig_indices = df.index[res.signals != 0].tolist()

        for idx in sig_indices:
            sig = res.signals.iloc[idx]
            entry = res.planned_entry_prices.iloc[idx]
            sl = res.planned_stop_losses.iloc[idx]
            tp = res.planned_take_profits.iloc[idx]
            rr = res.planned_rrs.iloc[idx]

            # None of them can be NaN or null
            self.assertFalse(math.isnan(entry), f"Planned entry is NaN at index {idx}")
            self.assertFalse(math.isnan(sl), f"Planned SL is NaN at index {idx}")
            self.assertFalse(math.isnan(tp), f"Planned TP is NaN at index {idx}")
            self.assertFalse(math.isnan(rr), f"Planned RR is NaN at index {idx}")

            if sig == 1:
                self.assertLess(sl, entry, f"BUY SL >= Entry at index {idx}")
                self.assertGreater(tp, entry, f"BUY TP <= Entry at index {idx}")
            elif sig == -1:
                self.assertGreater(sl, entry, f"SELL SL <= Entry at index {idx}")
                self.assertLess(tp, entry, f"SELL TP >= Entry at index {idx}")

    # -----------------------------------------------------------------
    # Test 9: Wave 1 regression check
    # -----------------------------------------------------------------
    def test_09_wave1_regression_unaffected(self):
        """Xác nhận luồng Wave 1 (smc_wave1, smc_s01, smc_s05, smc_s09) không bị ảnh hưởng bởi thay đổi legacy."""
        feed = DataFeed("data/XAUUSD.db")
        candles = feed.get_candles("M15", limit=100)
        df = pd.DataFrame(candles)

        engine = BacktestEngine(initial_capital=10000.0, lot_size=0.01)
        for w1_id in ["smc_s01", "smc_s05", "smc_s09", "smc_wave1"]:
            res = engine.run(df, strategy_id=w1_id, strategy_params={"min_rr": 1.5, "cooldown_bars": 3}, timeframe="M15")
            self.assertIn("metrics", res)
            self.assertIn("trades", res)
            self.assertIn("equity_curve", res)
            # Wave 1 results do NOT have legacy_telemetry
            self.assertNotIn("legacy_telemetry", res)


if __name__ == "__main__":
    unittest.main()
