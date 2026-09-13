"""
tests/test_backtest_legacy_compat.py
------------------------------------
Golden parity tests for the 5 legacy strategies to verify that
the shared execution kernel refactor preserves byte-for-byte exact
behavior against the pre-refactor baseline.
"""

import json
import os
import unittest
import pandas as pd
from engine.backtest_engine import BacktestEngine
from engine.data_feed import DataFeed

BASELINE_FILE = os.path.join(os.path.dirname(__file__), "fixtures", "t53_9_2_legacy_baseline.json")


class TestBacktestLegacyCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(BASELINE_FILE, "r", encoding="utf-8") as f:
            cls.baseline = json.load(f)

        feed = DataFeed()
        candles = feed.get_candles("H1", limit=250)
        cls.df = pd.DataFrame(candles)

    def _canonical_json(self, data):
        return json.dumps(data, sort_keys=True, default=str)

    def _run_and_compare(self, name, strat_id, params, allow_short):
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
        res = engine.run(self.df, strat_id, params)

        clean_res = {
            "metrics": res["metrics"],
            "trades": res["trades"],
            "equity_curve": res["equity_curve"],
            "markers": res["markers"],
        }
        if "smc_objects" in res:
            clean_res["smc_objects"] = res["smc_objects"]
        if "funnel_stats" in res:
            clean_res["funnel_stats"] = res["funnel_stats"]

        expected_json = self._canonical_json(self.baseline[name])
        actual_json = self._canonical_json(clean_res)

        self.assertEqual(actual_json, expected_json, f"Legacy parity mismatch for {name}")

    def test_01_sma_crossover_parity(self):
        self._run_and_compare(
            "sma_crossover", "sma_crossover",
            {"fast_period": 10, "slow_period": 30},
            allow_short=True
        )

    def test_02_sma_crossover_long_only_parity(self):
        self._run_and_compare(
            "sma_crossover_long_only", "sma_crossover",
            {"fast_period": 10, "slow_period": 30},
            allow_short=False
        )

    def test_03_rsi_reversal_parity(self):
        self._run_and_compare(
            "rsi_reversal", "rsi_reversal",
            {"period": 14, "oversold": 30.0, "overbought": 70.0},
            allow_short=True
        )

    def test_04_macd_crossover_parity(self):
        self._run_and_compare(
            "macd_crossover", "macd_crossover",
            {"fast": 12, "slow": 26, "signal": 9},
            allow_short=True
        )

    def test_05_donchian_breakout_parity(self):
        self._run_and_compare(
            "donchian_breakout", "donchian_breakout",
            {"lookback": 20},
            allow_short=True
        )

    def test_06_smc_confluence_parity(self):
        self._run_and_compare(
            "smc_confluence", "smc_confluence",
            {"swing_strength": 5, "internal_strength": 2, "bias_timing": "pre_candle"},
            allow_short=True
        )


if __name__ == "__main__":
    unittest.main()
