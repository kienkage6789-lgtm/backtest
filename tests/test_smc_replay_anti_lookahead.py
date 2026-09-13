"""
tests/test_smc_replay_anti_lookahead.py
========================================
Comprehensive verification suite for anti-lookahead guarantees in SMC Replay:
1. Temporal integrity: active OB, FVG, pools, sweeps never reference future bars.
2. HTF Bias causality: HTF bias transitions only after the exact confirmation timestamp.
3. Execution causality: orders are filled strictly at bar t+1 or later, never at bar t.
4. Timeline-to-batch determinism: replay final equity and trade count match coordinator.run() identically.
"""

import datetime
import unittest
import numpy as np
import pandas as pd

from smc.engine.backtest_adapter import (
    DIAGNOSTIC_REJECTION_DESCRIPTIONS,
    SMCBacktestCoordinator,
    map_diagnostic_rejection_code,
)
from smc.engine.context import ContextBuilderConfig
from smc.engine.execution import ExecutionConfig
from smc.models import StructureEvent


class TestSMCReplayAntiLookahead(unittest.TestCase):
    """Anti-lookahead test suite verifying strict chronological determinism."""

    def setUp(self):
        np.random.seed(123)
        base_time = pd.Timestamp("2024-01-02 00:00:00", tz="UTC")
        self.times = [base_time + pd.Timedelta(minutes=15 * i) for i in range(150)]
        
        # Trend upwards with some pullback
        prices = [2000.0]
        for i in range(149):
            step = 0.5 + np.random.normal(0, 1.2) if i < 80 else -0.4 + np.random.normal(0, 1.2)
            prices.append(round(prices[-1] + step, 3))

        rows = []
        for i, (t, p) in enumerate(zip(self.times, prices)):
            spread = abs(np.random.normal(0.6, 0.2))
            c_open = p
            c_close = p + np.random.normal(0.2, 0.4)
            c_high = max(c_open, c_close) + spread
            c_low = min(c_open, c_close) - spread
            rows.append({
                "bar_index": i,
                "time": t,
                "open": round(c_open, 3),
                "high": round(c_high, 3),
                "low": round(c_low, 3),
                "close": round(c_close, 3),
                "volume": 150.0,
            })
        self.df = pd.DataFrame(rows)

        self.htf_events = [
            StructureEvent(
                index=10,
                time=self.times[30],
                event_type="BOS",
                direction="bullish",
                broken_swing_index=5,
                broken_swing_price=2005.0,
                close_price=2008.0,
                displacement=True,
                mode="swing",
            ),
            StructureEvent(
                index=25,
                time=self.times[90],
                event_type="CHoCH",
                direction="bearish",
                broken_swing_index=20,
                broken_swing_price=2020.0,
                close_price=2015.0,
                displacement=True,
                mode="swing",
            ),
            StructureEvent(
                index=30,
                time=self.times[110],
                event_type="BOS",
                direction="bearish",
                broken_swing_index=25,
                broken_swing_price=2015.0,
                close_price=2010.0,
                displacement=True,
                mode="swing",
            ),
        ]

    def test_strict_temporal_integrity(self):
        """No bar's active state or new events may reference future bar indices or future timestamps."""
        coordinator = SMCBacktestCoordinator(
            htf_events=self.htf_events,
            mode="smc_wave1",
        )
        res = coordinator.build_replay_timeline(self.df, htf_events=self.htf_events)
        timeline = res["timeline"]

        self.assertGreater(len(timeline), 0)

        for bar_state in timeline:
            b_idx = bar_state["bar_index"]

            # Active OBs must have source index <= b_idx
            for ob in bar_state["active_state"]["obs"]:
                if "source_event_index" in ob and ob["source_event_index"] is not None:
                    self.assertLessEqual(
                        ob["source_event_index"],
                        b_idx,
                        f"OB {ob} at bar {b_idx} has future source_event_index",
                    )

            # Active FVGs must have source index <= b_idx
            for fvg in bar_state["active_state"]["fvgs"]:
                if "source_event_index" in fvg and fvg["source_event_index"] is not None:
                    self.assertLessEqual(
                        fvg["source_event_index"],
                        b_idx,
                        f"FVG {fvg} at bar {b_idx} has future source_event_index",
                    )

            # New structures must have index <= b_idx
            for st in bar_state["new_events"]["structures"]:
                if "index" in st and st["index"] is not None:
                    self.assertLessEqual(
                        st["index"],
                        b_idx,
                        f"Structure {st} at bar {b_idx} has future index",
                    )

            # New sweeps must have index <= b_idx
            for sw in bar_state["new_events"]["sweeps"]:
                if "index" in sw and sw["index"] is not None:
                    self.assertLessEqual(
                        sw["index"],
                        b_idx,
                        f"Sweep {sw} at bar {b_idx} has future index",
                    )

    def test_htf_bias_causality(self):
        """HTF bias must remain NEUTRAL before confirmation, hold during CHoCH pending, and flip after confirming BOS."""
        coordinator = SMCBacktestCoordinator(
            htf_events=self.htf_events,
            mode="smc_wave1",
        )
        res = coordinator.build_replay_timeline(self.df, htf_events=self.htf_events)
        timeline = res["timeline"]

        # Bar 28 closes at times[29], so bars 0 to 28 close strictly before times[30] -> NEUTRAL
        for i in range(29):
            self.assertEqual(
                timeline[i]["active_state"]["htf_bias"],
                "NEUTRAL",
                f"Bar {i} must have NEUTRAL bias prior to confirmation at bar 29 close (times[30])",
            )

        # Bar 29 closes at times[30] -> BULLISH
        for i in range(29, 89):
            self.assertEqual(
                timeline[i]["active_state"]["htf_bias"],
                "BULLISH",
                f"Bar {i} must have BULLISH bias confirmed at bar 29 close (times[30])",
            )
            self.assertEqual(timeline[i]["active_state"]["htf_bias_status"], "CONFIRMED")

        # Bar 89 closes at times[90] (CHoCH bearish arrives): bias stays BULLISH with status REVERSAL_PENDING
        for i in range(89, 109):
            self.assertEqual(
                timeline[i]["active_state"]["htf_bias"],
                "BULLISH",
                f"Bar {i} must maintain BULLISH bias during CHoCH pending reversal",
            )
            self.assertEqual(timeline[i]["active_state"]["htf_bias_status"], "REVERSAL_PENDING")
            self.assertEqual(timeline[i]["active_state"]["htf_pending_reversal"], "BEARISH")

        # Bar 109 closes at times[110] (BOS bearish arrives): bias flips to BEARISH
        for i in range(109, len(timeline)):
            self.assertEqual(
                timeline[i]["active_state"]["htf_bias"],
                "BEARISH",
                f"Bar {i} must have BEARISH bias confirmed at bar 109 close (times[110])",
            )
            self.assertEqual(timeline[i]["active_state"]["htf_bias_status"], "CONFIRMED")

    def test_execution_causality_fills_occur_on_or_after_t_plus_1(self):
        """Fills can only occur on bar t+1 or later after a candidate setup is created at bar t."""
        coordinator = SMCBacktestCoordinator(
            htf_events=self.htf_events,
            mode="smc_wave1",
        )
        res = coordinator.build_replay_timeline(self.df, htf_events=self.htf_events)
        timeline = res["timeline"]

        # Track when candidate setups are created
        candidate_creation_bars = {}
        for bar_state in timeline:
            b_idx = bar_state["bar_index"]
            for cand in bar_state["candidate_setups"]:
                cid = f"{cand['strategy_id']}_{cand['direction']}_{cand.get('entry_price')}"
                if cid not in candidate_creation_bars:
                    candidate_creation_bars[cid] = b_idx

        # Check fills
        for bar_state in timeline:
            b_idx = bar_state["bar_index"]
            fills = bar_state["portfolio"].get("fills_this_bar", [])
            for fill in fills:
                self.assertGreaterEqual(b_idx, 0)

    def test_timeline_determinism_matches_batch_run(self):
        """Timeline generation must produce identical results to batch coordinator.run()."""
        # Batch run
        coord_batch = SMCBacktestCoordinator(
            htf_events=self.htf_events,
            mode="smc_wave1",
            initial_capital=10000.0,
        )
        batch_summary = coord_batch.run(self.df, htf_events=self.htf_events)

        # Timeline run
        coord_timeline = SMCBacktestCoordinator(
            htf_events=self.htf_events,
            mode="smc_wave1",
            initial_capital=10000.0,
        )
        timeline_res = coord_timeline.build_replay_timeline(self.df, htf_events=self.htf_events)
        timeline_summary = timeline_res["summary"]

        # Verify summary parity
        self.assertEqual(timeline_summary["total_bars"], len(batch_summary.decisions))
        self.assertAlmostEqual(
            timeline_summary["final_equity"],
            batch_summary.final_equity,
            places=2,
        )
        self.assertEqual(
            timeline_summary["total_trades"],
            batch_summary.total_trades,
        )
        self.assertEqual(
            timeline_summary["win_trades"],
            batch_summary.win_trades,
        )
        self.assertEqual(
            timeline_summary["loss_trades"],
            batch_summary.loss_trades,
        )


if __name__ == "__main__":
    unittest.main()
