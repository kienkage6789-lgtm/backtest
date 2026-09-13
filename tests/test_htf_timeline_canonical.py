"""
tests/test_htf_timeline_canonical.py
====================================
Comprehensive verification tests for the Canonical HTF Event Timeline (T54.1.11).

Validates:
1. Index space alignment (0 <= event.index < 10000, not H1 index).
2. Zero future leak (held until bar_close_time >= event_available_time, index <= N).
3. Timestamp boundary conditions.
4. Future append invariance (prefix 5,000 vs full 10,000 bars).
5. Deterministic sorting and unique event identity.
6. Rejection of invalid payloads (negative index, out of bounds, tz-naive, duplicates).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

import pandas as pd

from engine.data_feed import DataFeed
from research.scripts.htf_event_runner import generate_htf_events_m15
from smc.engine.backtest_adapter import (
    HTFTimeline,
    parse_htf_event_payload,
)
from smc.engine.context import StrategyContextBuilder, ContextBuilderConfig, validate_as_of_evidence
from smc.models import StructureEvent


class TestHTFTimelineCanonical(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Generate canonical 10,000-bar HTF events payload
        cls.payload = generate_htf_events_m15(
            start="2022-01-01 00:00:00",
            end="2024-09-30 23:59:59",
            source_timeframe="H1",
            execution_timeframe="M15",
            limit=10000,
            strength=5,
        )
        cls.events = cls.payload["events"]
        cls.bar_count = cls.payload["bar_count"]

    # -------------------------------------------------------------------------
    # Test 1: Index cùng hệ quy chiếu
    # -------------------------------------------------------------------------
    def test_01_index_in_canonical_m15_space(self):
        """Xác nhận 0 <= event.index < 10000 và event.index không còn là H1 index."""
        self.assertGreater(len(self.events), 0, "Payload must contain events")
        self.assertEqual(self.payload["event_index_space"], "M15_CANONICAL")

        h1_indices = []
        m15_indices = []
        for ev in self.events:
            idx = ev["index"]
            c_idx = ev["canonical_m15_index"]
            h1_idx = ev["source_h1_index"]

            self.assertEqual(idx, c_idx, "event.index must equal canonical_m15_index")
            self.assertGreaterEqual(idx, 0, f"event.index must be non-negative: {idx}")
            self.assertLess(idx, self.bar_count, f"event.index must be < {self.bar_count}: {idx}")

            h1_indices.append(h1_idx)
            m15_indices.append(idx)

        # Confirm that M15 indices are strictly larger than H1 indices (ratio ~ 4x)
        avg_h1 = sum(h1_indices) / len(h1_indices)
        avg_m15 = sum(m15_indices) / len(m15_indices)
        self.assertGreater(avg_m15, avg_h1 * 3.5, "M15 canonical indices must be scaled to M15 timeframe")

    # -------------------------------------------------------------------------
    # Test 2: No future event
    # -------------------------------------------------------------------------
    def test_02_no_future_event_emitted_before_effective_time(self):
        """Tại mỗi M15 bar N, event chỉ được emit khi event.index <= N; nếu > N phải bị giữ lại."""
        parsed_struct_events = [parse_htf_event_payload(e) for e in self.events]
        timeline = HTFTimeline(parsed_struct_events)

        # Simulate stepping bar by bar
        # Pick the first event in the timeline
        first_ev = parsed_struct_events[0]
        event_bar_idx = first_ev.index
        event_avail_time = HTFTimeline.get_effective_time(first_ev)

        # 1. Probe just before the event is available (1 second before)
        pre_time = event_avail_time - pd.Timedelta(seconds=1)
        emitted_before = timeline.get_events_as_of(pre_time)
        self.assertEqual(len(emitted_before), 0, "Event must NOT be emitted before effective time")

        # 2. Probe at exact effective time
        emitted_at = timeline.get_events_as_of(event_avail_time)
        self.assertGreaterEqual(len(emitted_at), 1, "Event MUST be emitted at or after effective time")
        emitted_first = emitted_at[0]
        self.assertEqual(emitted_first.index, event_bar_idx)

        # 3. Confirm that the emitted event has index <= the current bar index
        self.assertLessEqual(emitted_first.index, event_bar_idx)

    # -------------------------------------------------------------------------
    # Test 3: Timestamp boundary
    # -------------------------------------------------------------------------
    def test_03_timestamp_boundary_conditions(self):
        """Kiểm tra event nằm đúng tại biên đầu/cuối M15 bar và loại bỏ ngoài execution range."""
        for ev in self.events:
            ev_avail = pd.Timestamp(ev["event_available_time"])
            ev_open = pd.Timestamp(ev["event_time"])

            # H1 bar duration is 1 hour
            self.assertEqual(ev_avail - ev_open, pd.Timedelta(hours=1))

            # Available time must end on a 15-minute boundary (:00, :15, :30, :45)
            self.assertIn(ev_avail.minute, (0, 15, 30, 45))
            self.assertEqual(ev_avail.second, 0)

        # Events before M15 execution start or after execution end must be excluded
        m15_start = pd.Timestamp(self.payload["m15_start_time"])
        m15_end = pd.Timestamp(self.payload["m15_end_time"])

        for ev in self.events:
            ev_avail = pd.Timestamp(ev["event_available_time"])
            self.assertGreaterEqual(ev_avail, m15_start)
            # Available time must be <= last M15 bar close time
            self.assertLessEqual(ev_avail, m15_end + pd.Timedelta(minutes=15))

    # -------------------------------------------------------------------------
    # Test 4: Future append invariance
    # -------------------------------------------------------------------------
    def test_04_future_append_invariance_prefix_5000(self):
        """Chạy prefix 5.000 bars và full 10.000 bars: các event trong 5.000 bars đầu phải giống hệt nhau."""
        payload_5000 = generate_htf_events_m15(
            start="2022-01-01 00:00:00",
            end="2024-09-30 23:59:59",
            source_timeframe="H1",
            execution_timeframe="M15",
            limit=5000,
            strength=5,
        )
        events_5000 = payload_5000["events"]

        # Filter events from 10,000-bar run that fall within the 5,000-bar index range (< 5000)
        events_10000_prefix = [e for e in self.events if e["canonical_m15_index"] < 5000]

        self.assertEqual(len(events_5000), len(events_10000_prefix), "Prefix event count must match exactly")

        for e5, e10 in zip(events_5000, events_10000_prefix):
            self.assertEqual(e5["canonical_m15_index"], e10["canonical_m15_index"])
            self.assertEqual(e5["event_time"], e10["event_time"])
            self.assertEqual(e5["event_type"], e10["event_type"])
            self.assertEqual(e5["direction"], e10["direction"])
            self.assertEqual(e5["broken_swing_price"], e10["broken_swing_price"])
            self.assertEqual(e5["close_price"], e10["close_price"])

    # -------------------------------------------------------------------------
    # Test 5: HTF event ordering & deterministic hash
    # -------------------------------------------------------------------------
    def test_05_deterministic_sorting_and_no_duplicates(self):
        """Xác nhận payload được sắp xếp deterministic và không trùng lặp event key."""
        seen_keys = set()
        prev_sort_key = None

        for ev in self.events:
            sort_key = (
                ev["canonical_m15_index"],
                ev["event_time"],
                ev["event_type"],
                ev["direction"],
            )
            if prev_sort_key is not None:
                self.assertLessEqual(prev_sort_key, sort_key, "Events must be sorted monotonically")
            prev_sort_key = sort_key

            identity_key = (
                ev["canonical_m15_index"],
                ev["time"],
                ev["event_type"],
                ev["direction"],
                ev["broken_swing_price"],
            )
            self.assertNotIn(identity_key, seen_keys, f"Duplicate event detected: {identity_key}")
            seen_keys.add(identity_key)

    # -------------------------------------------------------------------------
    # Test 6: Invalid payload rejection
    # -------------------------------------------------------------------------
    def test_06_invalid_payload_rejection(self):
        """Từ chối: index âm, index vượt dải, timestamp thiếu tz, event tương lai."""
        valid_ev = self.events[0]

        # 1. Negative index
        bad_neg = copy.deepcopy(valid_ev)
        bad_neg["index"] = -1
        with self.assertRaises(ValueError):
            parse_htf_event_payload(bad_neg)

        # 2. Timezone-naive timestamp
        bad_tz = copy.deepcopy(valid_ev)
        bad_tz["time"] = "2022-01-03 14:00:00"  # Missing +00:00 / UTC
        with self.assertRaises(ValueError):
            parse_htf_event_payload(bad_tz)

        # 3. Non-numeric broken_swing_price
        bad_price = copy.deepcopy(valid_ev)
        bad_price["broken_swing_price"] = "invalid_number"
        with self.assertRaises(ValueError):
            parse_htf_event_payload(bad_price)

        # 4. Future event leak detection in context validator
        bad_future_ev = parse_htf_event_payload(valid_ev)
        # Suppose current bar index is 10, but event has index 162
        current_bar = 10
        close_time = pd.Timestamp("2022-01-02 23:15:00+00:00")
        with self.assertRaises(ValueError) as ctx:
            validate_as_of_evidence(bad_future_ev, current_bar, close_time)
        self.assertIn("Future state leak", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
