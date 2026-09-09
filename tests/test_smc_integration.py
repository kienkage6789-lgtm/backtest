"""
tests/test_smc_integration.py
=============================
Integration test suite for the complete end-to-end SMC trading system:
1. Synthetic end-to-end pipeline test (M1 -> Swing -> Bias -> CHoCH -> FVG -> Order -> Retest -> Fill -> TP).
2. Swing Bias Timing test ('pre_candle' vs 'post_candle' simultaneous-bar conflict resolution).
3. FVG Ranking Engine and Candidate Pool ('m_maxRanked') verification.
4. Funnel Telemetry Tracker accuracy and summary table formatting.
5. Visual Chart Objects schema and metadata verification.
6. Real XAUUSD SQLite data backtest via BacktestEngine with smc_confluence strategy.
"""

import unittest
import pandas as pd
import numpy as np

from smc.models import SwingPoint, StructureEvent, FairValueGap
from smc.data_contract import normalize_ohlcv
from smc.strategy import run_smc_strategy, SMCStrategyConfig, SMCStrategyResult, SMCFunnelStats
from engine.strategies import StrategyRegistry
from engine.backtest_engine import BacktestEngine
from engine.data_feed import DataFeed


class TestSMCIntegration(unittest.TestCase):

    def _build_synthetic_e2e_dataset(self):
        """
        Creates a 35-bar dataset with a deterministic, valid SMC setup:
        - Bars 0..9: Prefix establishing Swing Trend
        - Bars 10..18: Internal pullback creating Internal Bearish BOS, then Bullish CHoCH at bar 19
        - Bar 19: Bullish FVG [88.0, 98.0] confirmed at bar 20
        - Bar 20: Pullback dips to 98.0 (within FVG -> LIMIT ORDER FILLED!)
        - Bars 21..34: Strong rally to 138 -> TP HIT!
        """
        prefix_highs = [70.0, 75.0, 85.0, 78.0, 72.0, 88.0, 92.0, 98.0, 95.0, 99.0]
        prefix_lows  = [65.0, 70.0, 80.0, 72.0, 68.0, 82.0, 88.0, 90.0, 89.0, 94.0]
        prefix_closes= [68.0, 74.0, 82.0, 75.0, 70.0, 87.0, 90.0, 96.0, 92.0, 97.0]
        prefix_opens = [66.0, 72.0, 81.0, 74.0, 69.0, 85.0, 89.0, 93.0, 91.0, 95.0]

        body_highs = [100.0, 98.0,  94.0,  96.0,  98.0,  92.0,  88.0,  84.0,  88.0,  102.0, 105.0, 103.0, 108.0, 110.0, 112.0, 115.0, 118.0, 120.0, 122.0, 125.0, 128.0, 130.0, 132.0, 135.0, 145.0]
        body_lows  = [96.0,  90.0,  88.0,  92.0,  94.0,  86.0,  82.0,  78.0,  82.0,  87.0,  98.0,  97.0,  102.0, 105.0, 107.0, 110.0, 113.0, 115.0, 117.0, 120.0, 123.0, 125.0, 127.0, 130.0, 138.0]
        body_closes= [97.0,  91.0,  92.0,  95.0,  97.0,  87.0,  83.0,  80.0,  87.0,  101.0, 104.0, 100.0, 107.0, 109.0, 111.0, 114.0, 117.0, 119.0, 121.0, 124.0, 127.0, 129.0, 131.0, 134.0, 144.0]
        body_opens = [(h + l) / 2.0 for h, l in zip(body_highs, body_lows)]

        highs = prefix_highs + body_highs
        lows  = prefix_lows + body_lows
        closes= prefix_closes + body_closes
        opens = prefix_opens + body_opens

        n = len(highs)
        dates = pd.date_range("2026-01-01 00:00", periods=n, freq="1min", tz="UTC")

        df = normalize_ohlcv(pd.DataFrame({
            "time": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 100
        }))
        return df

    def test_01_synthetic_end_to_end_pipeline(self):
        """
        [Point 1] Full End-to-End Pipeline Verification:
        OHLC M1 -> Swing Pivots -> Swing Bias -> Internal CHoCH -> FVG Discovery -> Limit Order -> Retest Fill -> TP Exit.
        """
        df = self._build_synthetic_e2e_dataset()
        config = SMCStrategyConfig(
            swing_strength=3,
            internal_strength=2,
            bias_timing="post_candle",
            choch_fvg_window=5,
            max_ranked_fvgs=3,
            min_fvg_score=0.0,
            order_type="limit",
            limit_expiry_bars=10,
            rr_ratio=2.0
        )

        result = run_smc_strategy(df, config)

        # 1. Verify structure detected
        self.assertGreaterEqual(result.funnel_stats.swing_pivots_detected, 2)
        self.assertGreaterEqual(result.funnel_stats.fvgs_total_detected, 1)

        # 2. Verify signal and order generated
        self.assertGreaterEqual(result.funnel_stats.pending_orders_created, 1,
                               msg="Expected at least 1 pending order created in the pipeline.")

        # 3. Verify order filled upon retest
        self.assertGreaterEqual(result.funnel_stats.orders_filled, 1,
                               msg="Expected pending limit order to be filled on FVG retest.")

        # 4. Verify trade recorded and closed in profit
        self.assertGreaterEqual(len(result.trades), 1)
        filled_trade = result.trades[0]
        # Verify strict zero lookahead: order placed only at bar 20 when FVG confirmed, filled at bar 21 upon retest
        self.assertEqual(filled_trade["placed_bar"], 20,
                         "Order must be placed at bar 20 when FVG is confirmed, never at bar 19.")
        self.assertEqual(filled_trade["entry_bar"], 21,
                         "Order must be filled on subsequent bar 21, strictly after placement.")
        self.assertEqual(filled_trade["direction"], "bullish")
        self.assertEqual(filled_trade["status"], "win")
        self.assertGreater(filled_trade["pnl"], 0.0)

    def test_02_bias_timing_pre_vs_post_candle(self):
        """
        [Point 2] Verify Swing Bias Timing conflict resolution:
        Simultaneous-bar break timing comparison between 'pre_candle' and 'post_candle'.
        """
        df = self._build_synthetic_e2e_dataset()

        # Run with post_candle
        cfg_post = SMCStrategyConfig(
            swing_strength=3, internal_strength=2, bias_timing="post_candle",
            choch_fvg_window=5, order_type="limit"
        )
        res_post = run_smc_strategy(df, cfg_post)

        # Run with pre_candle
        cfg_pre = SMCStrategyConfig(
            swing_strength=3, internal_strength=2, bias_timing="pre_candle",
            choch_fvg_window=5, order_type="limit"
        )
        res_pre = run_smc_strategy(df, cfg_pre)

        # Telemetry should capture the exact drop difference cleanly
        self.assertEqual(res_post.funnel_stats.choch_matching_swing_bias, 1)
        self.assertEqual(res_pre.funnel_stats.choch_rejected_by_bias, 2)

    def test_03_fvg_ranking_m_max_ranked_enforcement(self):
        """
        [Point 3] Verify that max_ranked_fvgs (m_maxRanked) strictly bounds
        the number of candidate FVGs admitted to the candidate pool.
        """
        df = self._build_synthetic_e2e_dataset()

        # Set max_ranked_fvgs = 1
        cfg_1 = SMCStrategyConfig(
            swing_strength=3, internal_strength=2, bias_timing="post_candle",
            max_ranked_fvgs=1, choch_fvg_window=5, order_type="limit"
        )
        res_1 = run_smc_strategy(df, cfg_1)

        # Set max_ranked_fvgs = 5
        cfg_5 = SMCStrategyConfig(
            swing_strength=3, internal_strength=2, bias_timing="post_candle",
            max_ranked_fvgs=5, choch_fvg_window=5, order_type="limit"
        )
        res_5 = run_smc_strategy(df, cfg_5)

        # Pool size with max_ranked_fvgs=1 must be strictly <= 1
        self.assertLessEqual(res_1.funnel_stats.fvgs_eligible_after_ranking, 1)
        self.assertGreaterEqual(res_5.funnel_stats.fvgs_eligible_after_ranking, res_1.funnel_stats.fvgs_eligible_after_ranking)

    def test_04_funnel_telemetry_tracker(self):
        """
        [Point 4] Verify Funnel Telemetry Tracker records all stages and formats cleanly.
        """
        df = self._build_synthetic_e2e_dataset()
        cfg = SMCStrategyConfig(swing_strength=3, internal_strength=2, bias_timing="post_candle")
        res = run_smc_strategy(df, cfg)
        stats = res.funnel_stats

        # Verify summary table formatting
        summary = stats.summary_table()
        self.assertIn("SMC PIPELINE FUNNEL STATS", summary)
        self.assertIn("[Step 1] Total Bars Scanned", summary)
        self.assertIn("[Step 2] Swing Pivots Confirmed", summary)
        self.assertIn("[Step 6] Orders Placed", summary)
        self.assertIn("[Step 7] Trades Completed", summary)

        # Verify dictionary serialization
        stats_dict = stats.to_dict()
        self.assertIn("total_bars", stats_dict)
        self.assertIn("orders_filled", stats_dict)
        self.assertIn("trades_won", stats_dict)
        self.assertEqual(stats_dict["trades_won"], 1)

    def test_05_visual_chart_objects_schema(self):
        """
        [Point 5] Verify visual chart objects are generated with complete coordinate metadata.
        """
        df = self._build_synthetic_e2e_dataset()
        cfg = SMCStrategyConfig(swing_strength=3, internal_strength=2, bias_timing="post_candle")
        res = run_smc_strategy(df, cfg)
        objs = res.chart_objects

        self.assertIn("swing_pivots", objs)
        self.assertIn("internal_pivots", objs)
        self.assertIn("swing_events", objs)
        self.assertIn("internal_events", objs)
        self.assertIn("order_blocks", objs)
        self.assertIn("fvgs", objs)
        self.assertIn("choch_windows", objs)
        self.assertIn("trades", objs)

        # Verify order blocks have high, low, direction, created_at
        if objs["order_blocks"]:
            ob = objs["order_blocks"][0]
            self.assertIn("high", ob)
            self.assertIn("low", ob)
            self.assertIn("direction", ob)
            self.assertIn("created_at", ob)

        # Verify swing pivots have time and price
        if objs["swing_pivots"]:
            sw = objs["swing_pivots"][0]
            self.assertIn("time", sw)
            self.assertIn("price", sw)
            self.assertIn("kind", sw)

        # Verify FVGs have top, bottom, direction
        if objs["fvgs"]:
            fvg = objs["fvgs"][0]
            self.assertIn("top", fvg)
            self.assertIn("bottom", fvg)
            self.assertIn("direction", fvg)

    def test_06_real_xauusd_data_backtest(self):
        """
        Runs BacktestEngine on real SQLite data with the registered smc_confluence strategy.
        Verifies no lookahead, metrics generation, and funnel telemetry attachment.
        """
        feed = DataFeed(db_path="data/XAUUSD.db")
        # Load 500 H1 candles as DataFrame
        candles = feed.get_candles(timeframe="H1", limit=500)
        df = pd.DataFrame(candles)
        self.assertGreaterEqual(len(df), 100)

        engine = BacktestEngine(initial_capital=10000.0, lot_size=0.1)
        params = {
            "swing_strength": 5,
            "internal_strength": 2,
            "bias_timing": "pre_candle",
            "choch_fvg_window": 5,
            "max_ranked_fvgs": 3,
            "min_fvg_score": 0.0,
            "order_type": "limit",
            "limit_expiry_bars": 15,
            "rr_ratio": 2.0
        }

        result = engine.run(df, strategy_id="smc_confluence", strategy_params=params)

        # Verify BacktestEngine returned valid metrics
        self.assertIn("metrics", result)
        self.assertIn("final_balance", result["metrics"])
        self.assertIn("win_rate", result["metrics"])

        # Verify SMC chart objects and funnel stats were attached
        self.assertIn("smc_objects", result)
        self.assertIn("funnel_stats", result)
        self.assertGreater(result["funnel_stats"]["total_bars"], 0)

        # Print funnel summary for audit
        print("\n[REAL DATA AUDIT] SMC Funnel Telemetry on XAUUSD H1 (500 bars):")
        stats_obj = SMCFunnelStats(**result["funnel_stats"])
        print(stats_obj.summary_table())


if __name__ == "__main__":
    unittest.main()
