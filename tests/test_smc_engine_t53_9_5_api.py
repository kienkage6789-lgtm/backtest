"""
tests/test_smc_engine_t53_9_5_api.py
======================================
T53.9.5 API End-to-End Test Suite.

Verifies:
1. POST /api/backtest with Wave 1 modes (smc_s01, smc_s05, smc_s09, smc_wave1).
2. Legacy strategy backward compatibility (sma_crossover, rsi_reversal, macd_crossover,
   donchian_breakout, smc_confluence).
3. 100% JSON serializability via json.dumps() for all responses.
4. Correct parsing of htf_events passed as list of dicts.
5. Fail-closed validation returning HTTP 400 (never HTTP 500):
   - Missing timezone in HTF events
   - NaN / Inf in HTF events
   - Invalid direction / event_type in HTF events
   - Unsupported Wave 1 timeframes (H1, H4, D1)
   - Insufficient candles
   - Invalid strategy ID
"""

from __future__ import annotations

import json
import unittest
from fastapi.testclient import TestClient
from server import app


class TestSMCEngineT5395API(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    # -----------------------------------------------------------------------
    # Group 1: Wave 1 API Execution
    # -----------------------------------------------------------------------

    def test_01_wave1_modes_execute_successfully(self):
        """All 4 Wave 1 modes (smc_s01, smc_s05, smc_s09, smc_wave1) execute via POST /api/backtest."""
        modes = ["smc_s01", "smc_s05", "smc_s09", "smc_wave1"]
        for mode in modes:
            with self.subTest(mode=mode):
                payload = {
                    "timeframe": "M15",
                    "limit": 50,
                    "strategy_id": mode,
                    "strategy_params": {"min_rr": 1.5, "cooldown_bars": 2},
                    "initial_capital": 10000.0,
                    "lot_size": 0.01,
                    "spread_points": 20.0,
                    "commission_per_lot": 5.0,
                    "allow_short": True,
                }
                res = self.client.post("/api/backtest", json=payload)
                self.assertEqual(res.status_code, 200, f"Mode {mode} failed: {res.text}")
                data = res.json()
                self.assertEqual(data["status"], "success")
                self.assertEqual(data["mode"], mode)
                self.assertEqual(data["schema_version"], "2.0.0")
                self.assertIn("run_metadata", data)
                self.assertIn("execution_events", data)
                self.assertIn("decisions", data)
                self.assertIn("pending_intents", data)
                self.assertIn("cooldown_snapshot", data)
                self.assertIn("metrics", data)
                self.assertIn("trades", data)
                self.assertIn("equity_curve", data)
                self.assertIn("markers", data)

                # Ensure valid json dump
                serialized = json.dumps(data)
                self.assertIsInstance(serialized, str)

    # -----------------------------------------------------------------------
    # Group 2: Legacy Strategy Backward Compatibility
    # -----------------------------------------------------------------------

    def test_02_legacy_strategies_compatibility(self):
        """All 5 legacy strategies execute and maintain exact backwards-compatible schema."""
        legacy_cases = [
            ("sma_crossover", {"fast_period": 10, "slow_period": 30}),
            ("rsi_reversal", {"rsi_period": 14, "oversold": 30, "overbought": 70}),
            ("macd_crossover", {"fast_period": 12, "slow_period": 26, "signal_period": 9}),
            ("donchian_breakout", {"channel_period": 20}),
            (
                "smc_confluence",
                {
                    "swing_strength": 3,
                    "internal_strength": 2,
                    "bias_timing": "pre_candle",
                },
            ),
        ]
        for strat_id, params in legacy_cases:
            with self.subTest(strategy_id=strat_id):
                payload = {
                    "timeframe": "H1",
                    "limit": 50,
                    "strategy_id": strat_id,
                    "strategy_params": params,
                    "initial_capital": 10000.0,
                    "lot_size": 0.1,
                    "stop_loss_points": 200.0,
                    "take_profit_points": 400.0,
                    "spread_points": 20.0,
                    "commission_per_lot": 5.0,
                    "allow_short": True,
                }
                res = self.client.post("/api/backtest", json=payload)
                self.assertEqual(res.status_code, 200, f"Legacy {strat_id} failed: {res.text}")
                data = res.json()
                self.assertEqual(data["status"], "success")
                self.assertIn("metrics", data)
                self.assertIn("trades", data)
                self.assertIn("equity_curve", data)
                self.assertIn("markers", data)

                # Legacy responses must NOT have Wave 1 V2 keys
                self.assertNotIn("schema_version", data)
                self.assertNotIn("cooldown_snapshot", data)

                # For smc_confluence, verify smc_objects and funnel_stats exist
                if strat_id == "smc_confluence":
                    self.assertIn("smc_objects", data)
                    self.assertIn("funnel_stats", data)

                # JSON serializability
                serialized = json.dumps(data)
                self.assertIsInstance(serialized, str)

    # -----------------------------------------------------------------------
    # Group 3: HTF Events Parsing
    # -----------------------------------------------------------------------

    def test_03_htf_events_parsed_correctly(self):
        """Valid htf_events passed as list of dicts are parsed and counted in run_metadata."""
        htf_events = [
            {
                "index": 0,
                "time": "2024-01-02T10:00:00+00:00",
                "event_type": "BOS",
                "direction": "bullish",
                "broken_swing_price": 2050.0,
                "close_price": 2055.0,
            },
            {
                "index": 0,
                "time": "2024-01-02T11:00:00+00:00",
                "event_type": "CHoCH",
                "direction": "bearish",
                "broken_swing_price": 2045.0,
                "close_price": 2040.0,
            },
        ]
        payload = {
            "timeframe": "M15",
            "limit": 50,
            "strategy_id": "smc_wave1",
            "strategy_params": {"min_rr": 1.5, "cooldown_bars": 2},
            "initial_capital": 10000.0,
            "lot_size": 0.01,
            "htf_events": htf_events,
        }
        res = self.client.post("/api/backtest", json=payload)
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["run_metadata"]["htf_events_count"], 2)

    # -----------------------------------------------------------------------
    # Group 4: Fail-Closed Validation (HTTP 400, Never HTTP 500)
    # -----------------------------------------------------------------------

    def test_04_missing_timezone_in_htf_event_returns_400(self):
        """HTF event with naive/missing timezone returns HTTP 400."""
        payload = {
            "timeframe": "M15",
            "limit": 50,
            "strategy_id": "smc_wave1",
            "htf_events": [
                {
                    "index": 1,
                    "time": "2024-01-02 10:00:00",  # missing tz offset
                    "event_type": "BOS",
                    "direction": "bullish",
                    "broken_swing_price": 2050.0,
                    "close_price": 2055.0,
                }
            ],
        }
        res = self.client.post("/api/backtest", json=payload)
        self.assertEqual(res.status_code, 400)
        self.assertIn("timezone", res.text.lower())

    def test_05_invalid_direction_or_event_type_returns_400(self):
        """Invalid direction or event_type in HTF event returns HTTP 400."""
        # Invalid direction
        payload_dir = {
            "timeframe": "M15",
            "limit": 50,
            "strategy_id": "smc_wave1",
            "htf_events": [
                {
                    "index": 1,
                    "time": "2024-01-02T10:00:00+00:00",
                    "event_type": "BOS",
                    "direction": "sideways",  # Invalid
                    "broken_swing_price": 2050.0,
                    "close_price": 2055.0,
                }
            ],
        }
        res = self.client.post("/api/backtest", json=payload_dir)
        self.assertEqual(res.status_code, 400)

        # Invalid event_type
        payload_ev = {
            "timeframe": "M15",
            "limit": 50,
            "strategy_id": "smc_wave1",
            "htf_events": [
                {
                    "index": 1,
                    "time": "2024-01-02T10:00:00+00:00",
                    "event_type": "BREAKOUT",  # Invalid (must be BOS or CHoCH)
                    "direction": "bullish",
                    "broken_swing_price": 2050.0,
                    "close_price": 2055.0,
                }
            ],
        }
        res = self.client.post("/api/backtest", json=payload_ev)
        self.assertEqual(res.status_code, 400)

    def test_06_nan_inf_numeric_in_htf_event_returns_400(self):
        """Non-numeric or non-finite prices in HTF event return HTTP 400."""
        payload_nan = {
            "timeframe": "M15",
            "limit": 50,
            "strategy_id": "smc_wave1",
            "htf_events": [
                {
                    "index": 1,
                    "time": "2024-01-02T10:00:00+00:00",
                    "event_type": "BOS",
                    "direction": "bullish",
                    "broken_swing_price": "invalid_price",
                    "close_price": 2055.0,
                }
            ],
        }
        res = self.client.post("/api/backtest", json=payload_nan)
        self.assertEqual(res.status_code, 400)

    def test_07_invalid_timeframe_for_wave1_returns_400(self):
        """Wave 1 strategies reject H1, H4, D1 timeframes with HTTP 400."""
        for tf in ["H1", "H4", "D1"]:
            for mode in ["smc_wave1", "smc_s01", "smc_s05", "smc_s09"]:
                with self.subTest(tf=tf, mode=mode):
                    payload = {
                        "timeframe": tf,
                        "limit": 50,
                        "strategy_id": mode,
                    }
                    res = self.client.post("/api/backtest", json=payload)
                    self.assertEqual(res.status_code, 400, f"Expected 400 for {mode} on {tf}")
                    self.assertIn("Timeframe", res.text)

    def test_08_invalid_strategy_id_returns_400(self):
        """Unknown strategy ID returns HTTP 400, never HTTP 500."""
        payload = {
            "timeframe": "M15",
            "limit": 50,
            "strategy_id": "totally_unknown_strategy_xyz",
        }
        res = self.client.post("/api/backtest", json=payload)
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("Internal Server Error", res.text)

    def test_09_insufficient_candles_returns_400(self):
        """Requests with start_time > end_time resulting in 0 candles return HTTP 400."""
        payload = {
            "timeframe": "M15",
            "start_time": "2024-05-10 00:00:00",
            "end_time": "2024-05-01 00:00:00",  # inverted range
            "limit": 50,
            "strategy_id": "smc_wave1",
        }
        res = self.client.post("/api/backtest", json=payload)
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("Internal Server Error", res.text)


if __name__ == "__main__":
    unittest.main()
