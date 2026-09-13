"""
tests/test_smc_context.py
=========================
Comprehensive Test Suite for T52 — Context:
- KillZone/SessionFilter (Boundary, Timezone, DST, Overnight, Precedence, Immutability)
- HTF Bias Adapter (Zero-lookahead, As-of mapping, Conflict, Future Queueing, Late Event, Gaps)
- Batch and Incremental Parity 100%
- Performance Benchmark (10,000 LTF bars)
"""

import datetime
import json
import time
import unittest
from types import MappingProxyType
import numpy as np
import pandas as pd

from smc.models import (
    SessionWindow,
    SessionDecision,
    Signal,
    BiasState,
    StructureEvent,
)
from smc.context.session import (
    LONDON_KILLZONE,
    NEWYORK_KILLZONE,
    ASIAN_RANGE,
    DEFAULT_SESSIONS,
    evaluate_session_window,
    evaluate_sessions,
    evaluate_sessions_batch,
    check_killzone_signal,
    SessionFilter,
)
from smc.context.htf_bias import (
    get_htf_bias_at,
    map_htf_bias_to_ltf,
    HTFBiasTracker,
)


class TestSMCContext(unittest.TestCase):

    # =========================================================================
    # 1. KILLZONE & SESSION FILTER TESTS
    # =========================================================================

    def test_session_start_inclusive_end_exclusive(self):
        """QC: boundary rule: start <= local_time < end."""
        window = LONDON_KILLZONE  # 07:00 - 10:00 UTC

        # 1 minute before start
        d_before = evaluate_session_window("2026-01-15 06:59:00 UTC", window, candle_closed=True)
        self.assertFalse(d_before.in_session)
        self.assertEqual(d_before.reason, "before_session_start")

        # Exact start
        d_start = evaluate_session_window("2026-01-15 07:00:00 UTC", window, candle_closed=True)
        self.assertTrue(d_start.in_session)
        self.assertEqual(d_start.reason, "inside_session")

        # Middle of session
        d_mid = evaluate_session_window("2026-01-15 08:30:00 UTC", window, candle_closed=True)
        self.assertTrue(d_mid.in_session)
        self.assertEqual(d_mid.reason, "inside_session")

        # Exact end
        d_end = evaluate_session_window("2026-01-15 10:00:00 UTC", window, candle_closed=True)
        self.assertFalse(d_end.in_session)
        self.assertEqual(d_end.reason, "at_session_end")

        # 1 minute after end
        d_after = evaluate_session_window("2026-01-15 10:01:00 UTC", window, candle_closed=True)
        self.assertFalse(d_after.in_session)
        self.assertEqual(d_after.reason, "outside_session")

    def test_session_dst_transition_london_and_newyork(self):
        """QC: DST transitions adjust correctly for exchange timezones but leave UTC sessions fixed."""
        london_local_window = SessionWindow(
            name="london_local",
            start=datetime.time(7, 0),
            end=datetime.time(10, 0),
            timezone="Europe/London",
        )
        ny_local_window = SessionWindow(
            name="ny_local",
            start=datetime.time(9, 30),
            end=datetime.time(16, 0),
            timezone="America/New_York",
        )

        # Winter London (GMT = UTC+0) on 2026-01-15
        d_lon_w = evaluate_session_window("2026-01-15 07:00:00 UTC", london_local_window, candle_closed=True)
        self.assertTrue(d_lon_w.in_session)
        d_lon_w_early = evaluate_session_window("2026-01-15 06:00:00 UTC", london_local_window, candle_closed=True)
        self.assertFalse(d_lon_w_early.in_session)

        # Summer London (BST = UTC+1) on 2026-07-15
        # 07:00 BST = 06:00 UTC -> should be in session!
        d_lon_s = evaluate_session_window("2026-07-15 06:00:00 UTC", london_local_window, candle_closed=True)
        self.assertTrue(d_lon_s.in_session)
        self.assertEqual(d_lon_s.meta["local_time"], "07:00:00")
        d_lon_s_early = evaluate_session_window("2026-07-15 05:59:00 UTC", london_local_window, candle_closed=True)
        self.assertFalse(d_lon_s_early.in_session)

        # Winter New York (EST = UTC-5) on 2026-01-15: 09:30 EST = 14:30 UTC
        d_ny_w = evaluate_session_window("2026-01-15 14:30:00 UTC", ny_local_window, candle_closed=True)
        self.assertTrue(d_ny_w.in_session)
        d_ny_w_early = evaluate_session_window("2026-01-15 14:29:00 UTC", ny_local_window, candle_closed=True)
        self.assertFalse(d_ny_w_early.in_session)

        # Summer New York (EDT = UTC-4) on 2026-07-15: 09:30 EDT = 13:30 UTC
        d_ny_s = evaluate_session_window("2026-07-15 13:30:00 UTC", ny_local_window, candle_closed=True)
        self.assertTrue(d_ny_s.in_session)
        self.assertEqual(d_ny_s.meta["local_time"], "09:30:00")
        d_ny_s_early = evaluate_session_window("2026-07-15 13:29:00 UTC", ny_local_window, candle_closed=True)
        self.assertFalse(d_ny_s_early.in_session)

        # UTC session is completely invariant across summer and winter
        d_utc_w = evaluate_session_window("2026-01-15 07:00:00 UTC", LONDON_KILLZONE, candle_closed=True)
        d_utc_s = evaluate_session_window("2026-07-15 07:00:00 UTC", LONDON_KILLZONE, candle_closed=True)
        self.assertTrue(d_utc_w.in_session)
        self.assertTrue(d_utc_s.in_session)

    def test_session_midnight_crossing_and_weekday_attribution(self):
        """QC: overnight sessions (start > end) attribute early morning hours to the session start day."""
        overnight_window = SessionWindow(
            name="overnight_monday_only",
            start=datetime.time(22, 0),
            end=datetime.time(2, 0),
            timezone="UTC",
            days=frozenset({0}),  # Monday only (0=Monday)
        )

        # Monday 2026-01-12 21:59 UTC: before start
        d1 = evaluate_session_window("2026-01-12 21:59:00 UTC", overnight_window, candle_closed=True)
        self.assertFalse(d1.in_session)
        self.assertEqual(d1.reason, "outside_session")

        # Monday 2026-01-12 22:00 UTC: in session
        d2 = evaluate_session_window("2026-01-12 22:00:00 UTC", overnight_window, candle_closed=True)
        self.assertTrue(d2.in_session)
        self.assertEqual(d2.reason, "inside_session")

        # Tuesday 2026-01-13 01:30 UTC: calendar day is Tuesday, but session cycle belongs to Monday (0)
        d3 = evaluate_session_window("2026-01-13 01:30:00 UTC", overnight_window, candle_closed=True)
        self.assertTrue(d3.in_session)
        self.assertEqual(d3.reason, "inside_session")
        self.assertEqual(d3.meta["eval_weekday"], 0)

        # Tuesday 2026-01-13 02:00 UTC: exact session end
        d4 = evaluate_session_window("2026-01-13 02:00:00 UTC", overnight_window, candle_closed=True)
        self.assertFalse(d4.in_session)
        self.assertEqual(d4.reason, "at_session_end")

        # Tuesday 2026-01-13 22:00 UTC: Tuesday start (weekday 1) not in days {0}
        d5 = evaluate_session_window("2026-01-13 22:00:00 UTC", overnight_window, candle_closed=True)
        self.assertFalse(d5.in_session)
        self.assertEqual(d5.reason, "wrong_weekday")

        # Wednesday 2026-01-14 01:30 UTC: Started Tuesday (weekday 1) not in days {0}
        d6 = evaluate_session_window("2026-01-14 01:30:00 UTC", overnight_window, candle_closed=True)
        self.assertFalse(d6.in_session)
        self.assertEqual(d6.reason, "wrong_weekday")

    def test_session_naive_timestamp_and_timezone_validation(self):
        """QC: naive timestamp rejected without default_timezone; localized when default_timezone provided."""
        window = LONDON_KILLZONE

        # Naive timestamp without default_timezone
        d_naive = evaluate_session_window("2026-01-15 07:30:00", window, candle_closed=True)
        self.assertFalse(d_naive.in_session)
        self.assertEqual(d_naive.reason, "naive_timestamp")

        # Naive timestamp with default_timezone="UTC"
        d_local = evaluate_session_window("2026-01-15 07:30:00", window, candle_closed=True, default_timezone="UTC")
        self.assertTrue(d_local.in_session)
        self.assertEqual(d_local.reason, "inside_session")

        # Invalid timezone string
        bad_window = SessionWindow(
            name="bad_tz",
            start=datetime.time(7, 0),
            end=datetime.time(10, 0),
            timezone="Mars/Phobos",
        )
        d_bad_tz = evaluate_session_window("2026-01-15 07:30:00 UTC", bad_window, candle_closed=True)
        self.assertFalse(d_bad_tz.in_session)
        self.assertEqual(d_bad_tz.reason, "invalid_timezone")

    def test_session_candle_closed_precedence_and_conflicts(self):
        """QC: strict deterministic precedence for closed, is_closed, and candle_closed parameter."""
        window = LONDON_KILLZONE

        # Precedence Rule 1: Both closed and is_closed present -> raise ValueError
        df_both = pd.DataFrame({
            "time": [pd.Timestamp("2026-01-15 08:00:00", tz="UTC")],
            "closed": [True],
            "is_closed": [True],
        })
        with self.assertRaises(ValueError):
            evaluate_sessions_batch(df_both, [window])

        # Precedence Rule 2: Single column present, used properly
        df_single = pd.DataFrame({
            "time": [pd.Timestamp("2026-01-15 08:00:00", tz="UTC")],
            "is_closed": [False],
        })
        res = evaluate_sessions_batch(df_single, [window])
        self.assertFalse(res[0].in_session)
        self.assertEqual(res[0].reason, "partial_candle")

        # Precedence Rule 2b: Single column present but candle_closed conflicts -> raise ValueError
        with self.assertRaises(ValueError):
            evaluate_sessions_batch(df_single, [window], candle_closed=True)

        # Precedence Rule 3: No column present, candle_closed parameter supplied
        df_no_col = pd.DataFrame({
            "time": [pd.Timestamp("2026-01-15 08:00:00", tz="UTC")],
        })
        res_param = evaluate_sessions_batch(df_no_col, [window], candle_closed=True)
        self.assertTrue(res_param[0].in_session)
        self.assertEqual(res_param[0].reason, "inside_session")

        # Precedence Rule 4: Neither column present nor candle_closed parameter -> raise ValueError
        with self.assertRaises(ValueError):
            evaluate_sessions_batch(df_no_col, [window], candle_closed=None)

    def test_session_strict_boolean_parsing_and_type_validation(self):
        """QC: Strict closed-state parser handles strings, ints, floats, and rejects NaN or invalid types."""
        window = LONDON_KILLZONE  # 07:00-10:00 UTC
        ts = "2026-01-15 08:00:00 UTC"

        # 1. String booleans: "false" MUST NOT be treated as True (anti-lookahead)
        d_str_false = evaluate_sessions(ts, sessions=[window], candle_closed="false")
        self.assertFalse(d_str_false.in_session)
        self.assertEqual(d_str_false.reason, "partial_candle")

        d_str_true = evaluate_sessions(ts, sessions=[window], candle_closed="true")
        self.assertTrue(d_str_true.in_session)
        self.assertEqual(d_str_true.reason, "inside_session")

        # 2. String 0 and 1
        d_str_0 = evaluate_sessions(ts, sessions=[window], candle_closed="0")
        self.assertFalse(d_str_0.in_session)
        self.assertEqual(d_str_0.reason, "partial_candle")

        d_str_1 = evaluate_sessions(ts, sessions=[window], candle_closed="1")
        self.assertTrue(d_str_1.in_session)

        # 3. Integers 0 and 1
        d_int_0 = evaluate_sessions(ts, sessions=[window], candle_closed=0)
        self.assertFalse(d_int_0.in_session)
        self.assertEqual(d_int_0.reason, "partial_candle")

        d_int_1 = evaluate_sessions(ts, sessions=[window], candle_closed=1)
        self.assertTrue(d_int_1.in_session)

        # 4. Floats 0.0 and 1.0
        d_flt_0 = evaluate_sessions(ts, sessions=[window], candle_closed=0.0)
        self.assertFalse(d_flt_0.in_session)
        self.assertEqual(d_flt_0.reason, "partial_candle")

        d_flt_1 = evaluate_sessions(ts, sessions=[window], candle_closed=1.0)
        self.assertTrue(d_flt_1.in_session)

        # 5. Invalid values: NaN, invalid strings, numbers other than 0/1 MUST raise ValueError
        with self.assertRaises(ValueError):
            evaluate_sessions(ts, sessions=[window], candle_closed=float("nan"))

        with self.assertRaises(ValueError):
            evaluate_sessions(ts, sessions=[window], candle_closed=np.nan)

        with self.assertRaises(ValueError):
            evaluate_sessions(ts, sessions=[window], candle_closed="invalid_bool")

        with self.assertRaises(ValueError):
            evaluate_sessions(ts, sessions=[window], candle_closed=2)

        with self.assertRaises(ValueError):
            evaluate_sessions(ts, sessions=[window], candle_closed=1.5)

        # 6. In DataFrame column: string booleans evaluated correctly without mistaking "false" for True
        df_batch_str = pd.DataFrame({
            "time": [pd.Timestamp("2026-01-15 08:00:00", tz="UTC"), pd.Timestamp("2026-01-15 08:15:00", tz="UTC")],
            "closed": ["false", "true"],
        })
        res = evaluate_sessions_batch(df_batch_str, sessions=[window])
        self.assertFalse(res[0].in_session)
        self.assertEqual(res[0].reason, "partial_candle")
        self.assertTrue(res[1].in_session)
        self.assertEqual(res[1].reason, "inside_session")

        # 7. In DataFrame column: NaN in closed column raises ValueError
        df_nan = pd.DataFrame({
            "time": [pd.Timestamp("2026-01-15 08:00:00", tz="UTC")],
            "closed": [np.nan],
        })
        with self.assertRaises(ValueError):
            evaluate_sessions_batch(df_nan, sessions=[window])

    def test_session_multi_session_aggregation_and_ordering(self):
        """QC: multi-session returns 1 decision per candle, deterministic order, matched_sessions tuple."""
        sessions = [
            SessionWindow("session_a", datetime.time(7, 0), datetime.time(10, 0), "UTC"),
            SessionWindow("session_b", datetime.time(9, 0), datetime.time(12, 0), "UTC"),
            SessionWindow("session_c", datetime.time(12, 0), datetime.time(15, 0), "UTC"),
        ]

        # 08:00 -> matches only session_a
        d_08 = evaluate_sessions("2026-01-15 08:00:00 UTC", sessions=sessions, candle_closed=True)
        self.assertTrue(d_08.in_session)
        self.assertEqual(d_08.session_name, "session_a")
        self.assertEqual(d_08.meta["matched_sessions"], ("session_a",))

        # 09:30 -> overlaps session_a and session_b
        d_0930 = evaluate_sessions("2026-01-15 09:30:00 UTC", sessions=sessions, candle_closed=True)
        self.assertTrue(d_0930.in_session)
        self.assertEqual(d_0930.session_name, "session_a")  # first configured
        self.assertEqual(d_0930.meta["matched_sessions"], ("session_a", "session_b"))

        # 11:00 -> matches only session_b
        d_11 = evaluate_sessions("2026-01-15 11:00:00 UTC", sessions=sessions, candle_closed=True)
        self.assertTrue(d_11.in_session)
        self.assertEqual(d_11.session_name, "session_b")
        self.assertEqual(d_11.meta["matched_sessions"], ("session_b",))

        # 16:00 -> matches none
        d_16 = evaluate_sessions("2026-01-15 16:00:00 UTC", sessions=sessions, candle_closed=True)
        self.assertFalse(d_16.in_session)
        self.assertIsNone(d_16.session_name)
        self.assertEqual(d_16.meta["matched_sessions"], ())

    def test_session_decision_and_signal_immutability_and_json(self):
        """QC: SessionDecision.meta is immutable Mapping and serializes to JSON cleanly."""
        window = LONDON_KILLZONE
        d = evaluate_session_window("2026-01-15 08:00:00 UTC", window, candle_closed=True)

        # Check immutability
        with self.assertRaises((TypeError, AttributeError)):
            d.meta["mutated_key"] = "hacked"

        # Check to_dict() and JSON serialization
        d_dict = d.to_dict()
        self.assertIsInstance(d_dict, dict)
        json_str = json.dumps(d_dict)
        self.assertIn("inside_session", json_str)

        # Check Signal API
        sig = check_killzone_signal("2026-01-15 08:00:00 UTC", sessions=[window], candle_closed=True)
        self.assertIsInstance(sig, Signal)
        self.assertEqual(sig.name, "in_killzone")
        self.assertTrue(sig.value)
        self.assertIn("session_name", sig.meta)
        self.assertIn("utc_timestamp", sig.meta)
        self.assertIn("local_time", sig.meta)
        self.assertIn("timezone", sig.meta)
        self.assertIn("reason", sig.meta)
        self.assertIn("candle_closed", sig.meta)

        sig_json = json.dumps(sig.to_dict())
        self.assertIn("in_killzone", sig_json)

    # =========================================================================
    # 2. HTF BIAS ADAPTER TESTS
    # =========================================================================

    def test_htf_future_event_not_visible(self):
        """QC: zero-lookahead: an HTF event confirmed at 10:00 is invisible at LTF bars < 10:00."""
        ev = StructureEvent(
            index=20,
            time=pd.Timestamp("2026-01-15 10:00:00", tz="UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=15,
            broken_swing_price=100.0,
            close_price=102.0,
        )

        ltf_bars = [
            pd.Timestamp("2026-01-15 09:00:00", tz="UTC"),
            pd.Timestamp("2026-01-15 09:15:00", tz="UTC"),
            pd.Timestamp("2026-01-15 09:30:00", tz="UTC"),
            pd.Timestamp("2026-01-15 09:45:00", tz="UTC"),
            pd.Timestamp("2026-01-15 10:00:00", tz="UTC"),
            pd.Timestamp("2026-01-15 10:15:00", tz="UTC"),
        ]

        mapped = map_htf_bias_to_ltf(ltf_bars, [ev])
        self.assertEqual(len(mapped), 6)

        # Bars before 10:00 must be neutral
        for i in range(4):
            self.assertEqual(mapped[i].bias, "neutral")
            self.assertEqual(mapped[i].reason, "no_htf_event")
            self.assertIsNone(mapped[i].source_event_index)

        # Bar at 10:00 and after must be bullish
        self.assertEqual(mapped[4].bias, "bullish")
        self.assertEqual(mapped[4].source_event_index, 20)
        self.assertEqual(mapped[4].reason, "initial_bos_confirmed")

        self.assertEqual(mapped[5].bias, "bullish")
        self.assertEqual(mapped[5].source_event_index, 20)

    def test_htf_future_event_queued_until_effective_time(self):
        """QC: in incremental tracker, future events passed ahead of time are queued and take effect only when t_ltf >= effective_time."""
        tracker = HTFBiasTracker()

        ev = StructureEvent(
            index=20,
            time=pd.Timestamp("2026-01-15 10:00:00", tz="UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=15,
            broken_swing_price=100.0,
            close_price=102.0,
        )

        # Ingest event with current_ltf_time=None -> returns None, event queued
        ret = tracker.update(ev, current_ltf_time=None)
        self.assertIsNone(ret)

        # Evaluate LTF candle at 09:00 -> future event ignored, returns neutral
        b1 = tracker.update(current_ltf_time="2026-01-15 09:00:00 UTC")
        self.assertIsNotNone(b1)
        self.assertEqual(b1.bias, "neutral")
        self.assertEqual(b1.reason, "no_htf_event")

        # Evaluate at 09:45 -> still neutral
        b2 = tracker.update(current_ltf_time="2026-01-15 09:45:00 UTC")
        self.assertEqual(b2.bias, "neutral")

        # Evaluate at 10:00 -> event is now effective -> returns bullish
        b3 = tracker.update(current_ltf_time="2026-01-15 10:00:00 UTC")
        self.assertEqual(b3.bias, "bullish")
        self.assertEqual(b3.source_event_index, 20)

        # Past emitted states in history remain untouched
        history = tracker.get_history()
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0].bias, "neutral")
        self.assertEqual(history[1].bias, "neutral")
        self.assertEqual(history[2].bias, "bullish")

    def test_htf_event_at_exact_cutoff(self):
        """QC: cutoff_time is inclusive; event exactly at cutoff is accepted, event after cutoff excluded."""
        cutoff = "2026-01-15 10:00:00 UTC"
        ev_cutoff = StructureEvent(
            index=20,
            time=pd.Timestamp("2026-01-15 10:00:00", tz="UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=15,
            broken_swing_price=100.0,
            close_price=102.0,
        )
        ev_after = StructureEvent(
            index=21,
            time=pd.Timestamp("2026-01-15 10:05:00", tz="UTC"),
            event_type="CHoCH",
            direction="bearish",
            broken_swing_index=16,
            broken_swing_price=102.0,
            close_price=99.0,
        )

        # At 10:00: ev_cutoff is accepted
        bias_10 = get_htf_bias_at("2026-01-15 10:00:00 UTC", [ev_cutoff, ev_after], cutoff_time=cutoff)
        self.assertEqual(bias_10.bias, "bullish")
        self.assertEqual(bias_10.source_event_index, 20)

        # At 11:00: ev_after is excluded by cutoff_time, so bias remains bullish from ev_cutoff
        bias_11 = get_htf_bias_at("2026-01-15 11:00:00 UTC", [ev_cutoff, ev_after], cutoff_time=cutoff)
        self.assertEqual(bias_11.bias, "bullish")
        self.assertEqual(bias_11.source_event_index, 20)

    def test_htf_ltf_gap_asof_mapping(self):
        """QC: as-of mapping handles weekend gaps and missing bars without index-based assumptions."""
        ev_fri = StructureEvent(
            index=10,
            time=pd.Timestamp("2026-01-09 21:00:00", tz="UTC"),  # Friday close
            event_type="BOS",
            direction="bullish",
            broken_swing_index=8,
            broken_swing_price=100.0,
            close_price=105.0,
        )
        ev_mon = StructureEvent(
            index=15,
            time=pd.Timestamp("2026-01-12 08:00:00", tz="UTC"),  # Monday morning
            event_type="CHoCH",
            direction="bearish",
            broken_swing_index=12,
            broken_swing_price=105.0,
            close_price=98.0,
        )
        ev_mon_bos = StructureEvent(
            index=16,
            time=pd.Timestamp("2026-01-12 10:00:00", tz="UTC"),  # Monday BOS confirming reversal
            event_type="BOS",
            direction="bearish",
            broken_swing_index=14,
            broken_swing_price=98.0,
            close_price=95.0,
        )

        ltf_bars = [
            pd.Timestamp("2026-01-09 20:59:00", tz="UTC"),  # before Friday event
            pd.Timestamp("2026-01-09 21:00:00", tz="UTC"),  # exact Friday event
            pd.Timestamp("2026-01-10 12:00:00", tz="UTC"),  # weekend gap
            pd.Timestamp("2026-01-12 07:45:00", tz="UTC"),  # Monday before CHoCH
            pd.Timestamp("2026-01-12 08:00:00", tz="UTC"),  # Monday at CHoCH -> remains bullish, pending bearish
            pd.Timestamp("2026-01-12 09:00:00", tz="UTC"),  # Monday after CHoCH -> still bullish, pending bearish
            pd.Timestamp("2026-01-12 10:00:00", tz="UTC"),  # Monday at BOS -> confirmed bearish
        ]

        mapped = map_htf_bias_to_ltf(ltf_bars, [ev_fri, ev_mon, ev_mon_bos])
        self.assertEqual(mapped[0].bias, "neutral")
        self.assertEqual(mapped[1].bias, "bullish")
        self.assertEqual(mapped[2].bias, "bullish")
        self.assertEqual(mapped[3].bias, "bullish")
        self.assertEqual(mapped[4].bias, "bullish")
        self.assertEqual(mapped[4].pending_reversal, "bearish")
        self.assertEqual(mapped[5].bias, "bullish")
        self.assertEqual(mapped[5].pending_reversal, "bearish")
        self.assertEqual(mapped[6].bias, "bearish")
        self.assertIsNone(mapped[6].pending_reversal)

    def test_htf_conflict_same_timestamp_neutral(self):
        """QC: conflicting bullish and bearish events at the exact same timestamp yield neutral bias."""
        ev_bull = StructureEvent(
            index=20,
            time=pd.Timestamp("2026-01-15 10:00:00", tz="UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=15,
            broken_swing_price=100.0,
            close_price=102.0,
        )
        ev_bear = StructureEvent(
            index=21,
            time=pd.Timestamp("2026-01-15 10:00:00", tz="UTC"),
            event_type="CHoCH",
            direction="bearish",
            broken_swing_index=16,
            broken_swing_price=101.0,
            close_price=99.0,
        )

        bias = get_htf_bias_at("2026-01-15 10:00:00 UTC", [ev_bull, ev_bear])
        self.assertEqual(bias.bias, "neutral")
        self.assertEqual(bias.reason, "conflicting_events")
        self.assertEqual(bias.meta["conflicting_count"], 2)

    def test_htf_validation_and_unsorted_duplicate_events(self):
        """QC: input validation, invalid conflict policy rejection, deterministic sorting and deduplication."""
        ev1 = StructureEvent(
            index=10,
            time=pd.Timestamp("2026-01-15 08:00:00", tz="UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=5,
            broken_swing_price=100.0,
            close_price=101.0,
        )
        ev2 = StructureEvent(
            index=12,
            time=pd.Timestamp("2026-01-15 09:00:00", tz="UTC"),
            event_type="CHoCH",
            direction="bearish",
            broken_swing_index=7,
            broken_swing_price=102.0,
            close_price=99.0,
        )
        ev3 = StructureEvent(
            index=14,
            time=pd.Timestamp("2026-01-15 10:00:00", tz="UTC"),
            event_type="BOS",
            direction="bearish",
            broken_swing_index=9,
            broken_swing_price=98.0,
            close_price=95.0,
        )

        # Test invalid conflict_policy rejection
        with self.assertRaises(ValueError):
            get_htf_bias_at("2026-01-15 09:00:00 UTC", [ev1], conflict_policy="skip")

        with self.assertRaises(ValueError):
            HTFBiasTracker(conflict_policy="arbitrary")

        # Test naive event timestamp rejection
        naive_ev = StructureEvent(
            index=10,
            time=pd.Timestamp("2026-01-15 08:00:00"),  # naive
            event_type="BOS",
            direction="bullish",
            broken_swing_index=5,
            broken_swing_price=100.0,
            close_price=101.0,
        )
        with self.assertRaises(ValueError):
            get_htf_bias_at("2026-01-15 09:00:00 UTC", [naive_ev])

        # Test unsorted and duplicate events: ev1 (BOS) -> ev2 (CHoCH) -> bias remains bullish, pending bearish
        unsorted_with_dups = [ev2, ev1, ev2, ev1]
        bias = get_htf_bias_at("2026-01-15 09:30:00 UTC", unsorted_with_dups)
        self.assertEqual(bias.bias, "bullish")
        self.assertEqual(bias.source_event_index, 10)
        self.assertEqual(bias.pending_reversal, "bearish")
        self.assertEqual(bias.pending_reversal_event_index, 12)

        # After subsequent BOS bearish -> flips to bearish
        unsorted_with_dups_3 = [ev3, ev2, ev1, ev3, ev1]
        bias_rev = get_htf_bias_at("2026-01-15 10:30:00 UTC", unsorted_with_dups_3)
        self.assertEqual(bias_rev.bias, "bearish")
        self.assertEqual(bias_rev.source_event_index, 14)

    def test_late_htf_event_policy(self):
        """QC: late-arriving HTF event does not mutate previously emitted states, but updates current/future state with late_event=True."""
        tracker = HTFBiasTracker()

        # Step 1: LTF candles up to 10:00 with no HTF events
        for m in ("09:00", "09:30", "10:00"):
            tracker.update(current_ltf_time=f"2026-01-15 {m}:00 UTC")

        self.assertEqual(len(tracker.get_history()), 3)
        for h in tracker.get_history():
            self.assertEqual(h.bias, "neutral")

        # Step 2: Late HTF event with time=09:15 arrives at 10:15
        late_ev = StructureEvent(
            index=5,
            time=pd.Timestamp("2026-01-15 09:15:00", tz="UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=2,
            broken_swing_price=100.0,
            close_price=103.0,
        )

        b_1015 = tracker.update(htf_event=late_ev, current_ltf_time="2026-01-15 10:15:00 UTC")
        self.assertIsNotNone(b_1015)
        self.assertEqual(b_1015.bias, "bullish")
        self.assertEqual(b_1015.source_event_index, 5)
        self.assertTrue(b_1015.meta.get("late_event", False))

        # Check history immutability: first 3 remain neutral!
        history = tracker.get_history()
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0].bias, "neutral")
        self.assertEqual(history[1].bias, "neutral")
        self.assertEqual(history[2].bias, "neutral")
        self.assertEqual(history[3].bias, "bullish")

        # Step 3: Second late event arrives at 10:30 with SAME timestamp 09:15 but lower index (non-representative)
        late_ev_bear = StructureEvent(
            index=2,
            time=pd.Timestamp("2026-01-15 09:15:00", tz="UTC"),
            event_type="CHoCH",
            direction="bearish",
            broken_swing_index=1,
            broken_swing_price=99.0,
            close_price=98.0,
        )
        b_1030 = tracker.update(htf_event=late_ev_bear, current_ltf_time="2026-01-15 10:30:00 UTC")
        self.assertIsNotNone(b_1030)
        # Cluster now has index 2 (bearish) and index 5 (bullish) at 09:15 -> conflict!
        self.assertEqual(b_1030.bias, "neutral")
        self.assertEqual(b_1030.reason, "conflicting_events")
        # MUST have late_event=True even though late_ev_bear was not cluster[-1]
        self.assertTrue(b_1030.meta.get("late_event", False))

    def test_htf_and_session_tracker_rejects_non_monotonic_timestamp(self):
        """QC: HTFBiasTracker and SessionFilter reject non-monotonic backward timestamps, supporting reset()."""
        # 1. HTFBiasTracker: reset() retains _known_events, clear_events() wipes them
        ev_bull = StructureEvent(
            index=1,
            time=pd.Timestamp("2026-01-15 08:00:00", tz="UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=0,
            broken_swing_price=100.0,
            close_price=102.0,
        )
        bias_tracker = HTFBiasTracker(htf_events=[ev_bull])
        b_initial = bias_tracker.update(current_ltf_time="2026-01-15 10:30:00 UTC")
        self.assertEqual(b_initial.bias, "bullish")

        # Non-monotonic backward step: 10:30 -> 10:20 MUST raise ValueError
        with self.assertRaises(ValueError) as ctx:
            bias_tracker.update(current_ltf_time="2026-01-15 10:20:00 UTC")
        self.assertIn("Non-monotonic LTF timestamp", str(ctx.exception))

        # Monotonic equal or forward step is allowed
        bias_tracker.update(current_ltf_time="2026-01-15 10:30:00 UTC")
        bias_tracker.update(current_ltf_time="2026-01-15 10:45:00 UTC")

        # reset() retains _known_events! Bias should still be bullish at 09:00
        bias_tracker.reset()
        self.assertIsNone(bias_tracker._last_ltf_time)
        b_rewind = bias_tracker.update(current_ltf_time="2026-01-15 09:00:00 UTC")
        self.assertIsNotNone(b_rewind)
        self.assertEqual(b_rewind.bias, "bullish")  # HTF event is retained!

        # clear_events() wipes HTF events
        bias_tracker.clear_events()
        b_empty = bias_tracker.update(current_ltf_time="2026-01-15 09:00:00 UTC")
        self.assertEqual(b_empty.bias, "neutral")
        self.assertEqual(b_empty.reason, "no_htf_event")

        # 2. SessionFilter: safe naive timestamp handling (no TypeError) & reset()
        sess_filter = SessionFilter(sessions=[LONDON_KILLZONE])
        sess_filter.update("2026-01-15 10:30:00 UTC", candle_closed=True)

        # Naive timestamp arriving after aware timestamp MUST NOT throw TypeError
        d_naive = sess_filter.update("2026-01-15 09:00:00", candle_closed=True)
        self.assertFalse(d_naive.in_session)
        self.assertEqual(d_naive.reason, "naive_timestamp")

        # Backward aware timestamp raises ValueError
        with self.assertRaises(ValueError) as ctx2:
            sess_filter.update("2026-01-15 10:20:00 UTC", candle_closed=True)
        self.assertIn("Non-monotonic timestamp in SessionFilter", str(ctx2.exception))

        sess_filter.reset()
        d_fresh = sess_filter.update("2026-01-15 08:00:00 UTC", candle_closed=True)
        self.assertTrue(d_fresh.in_session)

    def test_batch_incremental_context_parity(self):
        """QC: 100% parity between Batch and Incremental evaluation across all fields for Session and HTF Bias."""
        # 1. Session Filter Parity
        dates = pd.date_range("2026-01-15 05:00", periods=60, freq="15min", tz="UTC")
        df_candles = pd.DataFrame({
            "time": dates,
            "closed": [True] * 60,
        })
        sessions = [LONDON_KILLZONE, NEWYORK_KILLZONE]

        batch_sessions = evaluate_sessions_batch(df_candles, sessions=sessions)

        tracker = SessionFilter(sessions=sessions)
        inc_sessions = [tracker.update(df_candles.iloc[i]) for i in range(len(df_candles))]

        self.assertEqual(len(batch_sessions), len(inc_sessions))
        for b, inc in zip(batch_sessions, inc_sessions):
            self.assertEqual(b.in_session, inc.in_session)
            self.assertEqual(b.session_name, inc.session_name)
            self.assertEqual(b.timestamp, inc.timestamp)
            self.assertEqual(b.reason, inc.reason)
            self.assertEqual(b.meta["matched_sessions"], inc.meta["matched_sessions"])
            self.assertEqual(b.to_dict(), inc.to_dict())

        # 2. HTF Bias Parity
        ev1 = StructureEvent(
            index=2,
            time=pd.Timestamp("2026-01-15 07:00:00", tz="UTC"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=1,
            broken_swing_price=100.0,
            close_price=105.0,
        )
        ev2 = StructureEvent(
            index=6,
            time=pd.Timestamp("2026-01-15 13:00:00", tz="UTC"),
            event_type="CHoCH",
            direction="bearish",
            broken_swing_index=4,
            broken_swing_price=105.0,
            close_price=98.0,
        )
        htf_events = [ev1, ev2]

        batch_biases = map_htf_bias_to_ltf(df_candles, htf_events)

        bias_tracker = HTFBiasTracker(htf_events=htf_events)
        inc_biases = [bias_tracker.update(current_ltf_time=dates[i]) for i in range(len(dates))]

        self.assertEqual(len(batch_biases), len(inc_biases))
        for b, inc in zip(batch_biases, inc_biases):
            self.assertEqual(b.bias, inc.bias)
            self.assertEqual(b.timestamp, inc.timestamp)
            self.assertEqual(b.source_event_index, inc.source_event_index)
            self.assertEqual(b.source_event_time, inc.source_event_time)
            self.assertEqual(b.source_event_type, inc.source_event_type)
            self.assertEqual(b.source_event_direction, inc.source_event_direction)
            self.assertEqual(b.reason, inc.reason)
            self.assertEqual(b.to_dict(), inc.to_dict())

    def test_context_performance_benchmark(self):
        """QC: Benchmark 10,000 LTF bars with realistic HTF events, measuring sort/prepare, execution time, and memory bound."""
        n_bars = 10000
        dates = pd.date_range("2026-01-01 00:00", periods=n_bars, freq="1min", tz="UTC")
        df_ltf = pd.DataFrame({
            "time": dates,
            "closed": [True] * n_bars,
        })

        # Generate 100 realistic HTF structure events
        htf_events = []
        for k in range(100):
            bar_t = dates[k * 95]
            htf_events.append(StructureEvent(
                index=k,
                time=bar_t,
                event_type="BOS" if k % 3 != 0 else "CHoCH",
                direction="bullish" if k % 2 == 0 else "bearish",
                broken_swing_index=max(0, k - 2),
                broken_swing_price=100.0 + (k % 10),
                close_price=101.0 + (k % 10),
            ))

        sessions = [LONDON_KILLZONE, NEWYORK_KILLZONE, ASIAN_RANGE]

        # Benchmark 1: Batch Session Filter on 10k bars
        t0_sess = time.perf_counter()
        sess_results = evaluate_sessions_batch(df_ltf, sessions=sessions)
        t_sess = time.perf_counter() - t0_sess

        self.assertEqual(len(sess_results), n_bars)
        self.assertLess(t_sess, 1.5)

        # Benchmark 2: Batch HTF Bias mapping on 10k bars
        t0_bias = time.perf_counter()
        bias_results = map_htf_bias_to_ltf(df_ltf, htf_events)
        t_bias = time.perf_counter() - t0_bias

        self.assertEqual(len(bias_results), n_bars)
        self.assertLess(t_bias, 1.0)

        # Benchmark 3: Incremental streaming simulation over 10k bars
        tracker_sess = SessionFilter(sessions=sessions)
        tracker_bias = HTFBiasTracker(htf_events=htf_events)

        t0_inc = time.perf_counter()
        for i in range(n_bars):
            t_bar = dates[i]
            tracker_sess.update(t_bar, candle_closed=True)
            tracker_bias.update(current_ltf_time=t_bar)
        t_inc = time.perf_counter() - t0_inc

        self.assertLess(t_inc, 2.5)

        print(
            f"\n[CONTEXT BENCHMARK] 10,000 LTF bars, {len(htf_events)} HTF events, {len(sessions)} sessions:\n"
            f"  - Batch SessionFilter: {t_sess*1000:.2f}ms (threshold < 1500ms)\n"
            f"  - Batch HTFBias:       {t_bias*1000:.2f}ms (threshold < 1000ms)\n"
            f"  - Incremental Dual:    {t_inc*1000:.2f}ms ({t_inc/n_bars*1e6:.1f} µs/bar, threshold < 2500ms)"
        )


if __name__ == "__main__":
    unittest.main()
