"""
tests/test_smc_replay_timeline.py
==================================
Unit tests for the Bar-by-Bar SMC Replay Timeline Engine and Diagnostic Rejection Taxonomy.
Verifies timeline structure, bookmark indexing, 22-code diagnostic mapping, and API endpoint.
"""

import datetime
import unittest
import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from smc.engine.backtest_adapter import (
    DIAGNOSTIC_REJECTION_DESCRIPTIONS,
    SMCBacktestCoordinator,
    map_diagnostic_rejection_code,
)
from smc.models import StructureEvent
from server import app


class TestSMCReplayTimeline(unittest.TestCase):
    """Test suite for SMC Replay Timeline generation and diagnostic taxonomy."""

    def setUp(self):
        # Generate 100 synthetic M15 bars
        np.random.seed(42)
        base_time = pd.Timestamp("2024-01-02 00:00:00", tz="UTC")
        times = [base_time + pd.Timedelta(minutes=15 * i) for i in range(120)]
        prices = [2000.0]
        for _ in range(119):
            delta = np.random.normal(0, 1.5)
            prices.append(round(prices[-1] + delta, 3))

        rows = []
        for i, (t, p) in enumerate(zip(times, prices)):
            spread = abs(np.random.normal(0.5, 0.2))
            c_open = p
            c_close = p + np.random.normal(0, 0.4)
            c_high = max(c_open, c_close) + spread
            c_low = min(c_open, c_close) - spread
            rows.append({
                "bar_index": i,
                "time": t,
                "open": round(c_open, 3),
                "high": round(c_high, 3),
                "low": round(c_low, 3),
                "close": round(c_close, 3),
                "volume": 100.0,
            })
        self.df = pd.DataFrame(rows)

    def test_diagnostic_rejection_taxonomy_complete(self):
        """All 22 diagnostic rejection codes must have bilingual descriptions and valid mappings."""
        expected_codes = [
            "missing_htf_bias",
            "htf_bias_mismatch",
            "invalid_sweep",
            "sweep_not_confirmed",
            "wrong_structure_direction",
            "wrong_regime",
            "mss_before_sweep",
            "missing_displacement",
            "fvg_wrong_structure_leg",
            "fvg_too_far_from_mss",
            "fvg_filled_before_retest",
            "opposite_structure_detected",
            "outside_session",
            "grace_expired",
            "ob_too_old",
            "ob_already_retested",
            "invalid_entry_geometry",
            "rr_below_minimum",
            "selector_rejected",
            "cooldown_active",
            "position_already_open",
            "execution_no_fill",
        ]
        for code in expected_codes:
            self.assertIn(code, DIAGNOSTIC_REJECTION_DESCRIPTIONS, f"Missing code {code}")
            meta = DIAGNOSTIC_REJECTION_DESCRIPTIONS[code]
            self.assertIn("en", meta)
            self.assertIn("vi", meta)
            self.assertTrue(len(meta["en"]) > 0)
            self.assertTrue(len(meta["vi"]) > 0)

    def test_map_diagnostic_rejection_code(self):
        """Mapper converts various raw engine strings to the canonical 22 codes."""
        self.assertEqual(map_diagnostic_rejection_code("missing_required_evidence", context=None), "missing_required_evidence")
        self.assertEqual(map_diagnostic_rejection_code("insufficient_rr"), "rr_below_minimum")
        self.assertEqual(map_diagnostic_rejection_code("geometry_violation_at_fill"), "invalid_entry_geometry")
        self.assertEqual(map_diagnostic_rejection_code("fvg_invalidated_by_close"), "fvg_filled_before_retest")
        self.assertEqual(map_diagnostic_rejection_code("invalid_order_block"), "ob_already_retested")
        self.assertEqual(map_diagnostic_rejection_code("stale_liquidity_sweep"), "invalid_sweep")
        self.assertEqual(map_diagnostic_rejection_code("opposite_structure_shift"), "opposite_structure_detected")
        self.assertEqual(map_diagnostic_rejection_code("cooldown_active"), "cooldown_active")
        self.assertEqual(map_diagnostic_rejection_code("position_already_open_same_direction"), "position_already_open")

    def test_coordinator_build_replay_timeline_wave1(self):
        """Coordinator builds complete replay timeline for smc_wave1 with bookmark indexes."""
        coordinator = SMCBacktestCoordinator(mode="smc_wave1")
        res = coordinator.build_replay_timeline(self.df, start_idx=10, max_bars=50)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["strategy"], "smc_wave1")
        self.assertEqual(res["start_bar_index"], 10)
        self.assertEqual(len(res["timeline"]), 50)
        self.assertIn("bookmarks", res)
        for bk in ("candidates", "rejections", "fills", "events"):
            self.assertIn(bk, res["bookmarks"])
            self.assertIsInstance(res["bookmarks"][bk], list)

        # Inspect first bar in timeline
        b0 = res["timeline"][0]
        self.assertEqual(b0["bar_index"], 10)
        self.assertIn("open", b0)
        self.assertIn("high", b0)
        self.assertIn("low", b0)
        self.assertIn("close", b0)
        self.assertIn("new_events", b0)
        self.assertIn("active_state", b0)
        self.assertIn("candidates", b0)
        self.assertIn("confluence", b0)
        self.assertIn("selector", b0)
        self.assertIn("position", b0)
        self.assertIn("execution_events", b0)
        self.assertIn("equity", b0)
        self.assertIn("balance", b0)

    def test_coordinator_build_replay_timeline_confluence_mode(self):
        """smc_confluence mode is accepted and executes multi-strategy evaluation."""
        coordinator = SMCBacktestCoordinator(mode="smc_confluence")
        res = coordinator.build_replay_timeline(self.df, max_bars=30)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["strategy"], "smc_confluence")
        self.assertEqual(len(res["timeline"]), 30)

    def test_api_replay_timeline_endpoint(self):
        """POST /api/replay/timeline returns valid JSON timeline payload via HTTP."""
        client = TestClient(app)
        payload = {
            "timeframe": "M15",
            "symbol": "XAUUSD",
            "strategy_id": "smc_wave1",
            "limit": 100,
            "start_bar_index": 0,
            "max_bars": 50,
            "strategy_params": {
                "cooldown_bars": 3,
                "min_rr": 1.5,
            },
        }
        response = client.post("/api/replay/timeline", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("timeline", data)
        self.assertIn("bookmarks", data)
        self.assertIn("summary", data)


if __name__ == "__main__":
    unittest.main()
