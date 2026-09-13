"""
tests/test_smc_engine_t53_9_4_integration.py
=============================================
T53.9.4 Integration Test Suite — API/Data Integration & Mode Exposure.

Tests:
 01. /api/strategies returns all 9 IDs, no duplicates, legacy metadata intact.
 02. Dispatcher routes 5 legacy strategies unaffected by new code.
 03. Dispatcher routes all 4 Wave1 modes to SMCBacktestCoordinator.
 04. Timeframe validation: H1/D1 rejected with ValueError; M15 accepted.
 05. HTF events accepted as list-of-dicts and recorded in run_metadata.
 06. HTF future event withheld — no lookahead into past bars.
 07. HTF exact boundary event (effective_time == bar_close_time) admitted.
 08. Conflicting duplicate HTF events (same key, different payload) rejected.
 09. Invalid inputs (NaN, Inf, missing tz) rejected with ValueError/TypeError.
 10. Wave1 response has V2 fields and is 100% JSON-serialisable.
"""

from __future__ import annotations

import json
import math
import unittest
from datetime import timezone
from typing import Any

import pandas as pd

from engine.backtest_engine import BacktestEngine, WAVE1_ALLOWED_TIMEFRAMES
from engine.strategies import StrategyRegistry


# ---------------------------------------------------------------------------
# Helper: minimal synthetic OHLCV dataframe
# ---------------------------------------------------------------------------

def _make_df(n: int = 30, timeframe_minutes: int = 15) -> pd.DataFrame:
    """Create a minimal DataFrame with tz-NAIVE timestamps (for legacy backtest engine tests)."""
    base = pd.Timestamp("2024-01-02 10:00:00")  # tz-naive
    freq = pd.Timedelta(minutes=timeframe_minutes)
    rows = []
    price = 2000.0
    for i in range(n):
        ts = base + freq * i
        rows.append({
            "bar_index": i,
            "time": ts,
            "open": round(price, 2),
            "high": round(price + 1.5, 2),
            "low": round(price - 1.5, 2),
            "close": round(price + 0.5, 2),
            "volume": 100.0,
        })
        price += 0.1
    return pd.DataFrame(rows)


def _make_df_utc(n: int = 30, timeframe_minutes: int = 15) -> pd.DataFrame:
    """Create a minimal DataFrame with UTC-aware timestamps (for Wave1 coordinator tests)."""
    base = pd.Timestamp("2024-01-02 10:00:00", tz="UTC")
    freq = pd.Timedelta(minutes=timeframe_minutes)
    rows = []
    price = 2000.0
    for i in range(n):
        ts = base + freq * i
        rows.append({
            "bar_index": i,
            "time": ts,
            "open": round(price, 2),
            "high": round(price + 1.5, 2),
            "low": round(price - 1.5, 2),
            "close": round(price + 0.5, 2),
            "volume": 100.0,
        })
        price += 0.1
    return pd.DataFrame(rows)


def _make_engine(**kw) -> BacktestEngine:
    defaults = dict(
        initial_capital=10000.0,
        lot_size=0.01,
        spread_points=20.0,
        commission_per_lot=5.0,
        allow_short=True,
    )
    defaults.update(kw)
    return BacktestEngine(**defaults)


def _make_htf_event_dict(
    bar_idx: int = 0,
    time_str: str = "2024-01-02T09:00:00+00:00",
    event_type: str = "BOS",
    direction: str = "bullish",
    bsp: float = 1998.0,
    cp: float = 1999.0,
) -> dict[str, Any]:
    return {
        "index": bar_idx,
        "time": time_str,
        "event_type": event_type,
        "direction": direction,
        "broken_swing_price": bsp,
        "close_price": cp,
    }


# ===========================================================================
# Test Suite
# ===========================================================================

class TestT5394StrategyRegistry(unittest.TestCase):
    """test_01: /api/strategies catalog has exactly 9 IDs, no duplicates, legacy intact."""

    def test_01_api_strategies_contains_all_9_ids_no_duplicates(self):
        legacy = StrategyRegistry.get_available_strategies()
        wave1 = StrategyRegistry.get_wave1_strategies()
        all_strats = legacy + wave1

        ids = [s["id"] for s in all_strats]

        # 9 total
        self.assertEqual(len(ids), 9, f"Expected 9 strategies, got {len(ids)}: {ids}")

        # No duplicates
        self.assertEqual(len(ids), len(set(ids)), f"Duplicate IDs found: {ids}")

        # Legacy IDs present and unchanged
        legacy_ids = {s["id"] for s in legacy}
        self.assertEqual(
            legacy_ids,
            StrategyRegistry.LEGACY_STRATEGIES,
            f"Legacy IDs mismatch: {legacy_ids}",
        )

        # Wave1 IDs present
        wave1_ids = {s["id"] for s in wave1}
        self.assertEqual(
            wave1_ids,
            StrategyRegistry.WAVE1_STRATEGIES,
            f"Wave1 IDs mismatch: {wave1_ids}",
        )

        # All Wave1 entries declare allowed_timeframes
        for entry in wave1:
            self.assertIn("allowed_timeframes", entry, f"wave1 entry missing allowed_timeframes: {entry['id']}")
            self.assertIn("M15", entry["allowed_timeframes"])

        # Legacy metadata for sma_crossover is intact (spot-check)
        sma = next(s for s in legacy if s["id"] == "sma_crossover")
        self.assertEqual(sma["name"], "SMA Crossover (Giao cắt MA)")

    def test_01b_supported_strategies_set_has_9_ids(self):
        self.assertEqual(len(StrategyRegistry.SUPPORTED_STRATEGIES), 9)
        self.assertEqual(
            StrategyRegistry.SUPPORTED_STRATEGIES,
            StrategyRegistry.LEGACY_STRATEGIES | StrategyRegistry.WAVE1_STRATEGIES,
        )


class TestT5394LegacyDispatcher(unittest.TestCase):
    """test_02: 5 legacy strategies route through dispatcher unchanged."""

    def _run_legacy(self, strategy_id: str, params: dict = None) -> dict:
        df = _make_df(30)  # tz-naive for legacy engine
        engine = _make_engine()
        result = engine.run(df, strategy_id, params or {}, timeframe="H1")
        return result

    def test_02_sma_crossover_legacy_path(self):
        res = self._run_legacy("sma_crossover", {"fast_period": 5, "slow_period": 10})
        self.assertIn("metrics", res)
        self.assertIn("trades", res)
        self.assertIn("equity_curve", res)
        self.assertIn("markers", res)
        # No V2 fields in legacy response
        self.assertNotIn("mode", res)
        self.assertNotIn("schema_version", res)

    def test_02_rsi_reversal_legacy_path(self):
        res = self._run_legacy("rsi_reversal", {"period": 5})
        self.assertIn("metrics", res)
        self.assertNotIn("schema_version", res)

    def test_02_macd_crossover_legacy_path(self):
        res = self._run_legacy("macd_crossover", {"fast": 5, "slow": 10, "signal": 3})
        self.assertIn("metrics", res)

    def test_02_donchian_breakout_legacy_path(self):
        res = self._run_legacy("donchian_breakout", {"lookback": 10})
        self.assertIn("metrics", res)

    def test_02_smc_confluence_legacy_path(self):
        res = self._run_legacy(
            "smc_confluence",
            {
                "swing_strength": 3,
                "internal_strength": 2,
                "bias_timing": "pre_candle",
                "choch_fvg_window": 3,
                "max_ranked_fvgs": 2,
                "min_fvg_score": 0.0,
                "require_ob": False,
                "ob_lookback": 10,
                "sl_anchor": "ob",
                "order_type": "market",
                "limit_expiry_bars": 10,
                "rr_ratio": 2.0,
            },
        )
        self.assertIn("metrics", res)

    def test_02_invalid_strategy_raises_valueerror(self):
        df = _make_df(10)
        engine = _make_engine()
        with self.assertRaises(ValueError) as ctx:
            engine.run(df, "unknown_strategy_xyz", {})
        self.assertIn("unknown_strategy_xyz", str(ctx.exception))


class TestT5394Wave1Dispatcher(unittest.TestCase):
    """test_03: 4 Wave1 modes are dispatched to SMCBacktestCoordinator."""

    def _run_wave1(self, mode: str) -> dict:
        df = _make_df_utc(20, timeframe_minutes=15)
        engine = _make_engine()
        return engine.run(df, mode, {}, timeframe="M15")

    def _assert_v2_fields(self, result: dict, mode: str) -> None:
        self.assertEqual(result.get("mode"), mode)
        self.assertEqual(result.get("schema_version"), "2.0.0")
        self.assertIn("execution_events", result)
        self.assertIn("decisions", result)
        self.assertIn("pending_intents", result)
        self.assertIn("cooldown_snapshot", result)
        self.assertIn("run_metadata", result)
        self.assertIn("metrics", result)
        self.assertIn("trades", result)
        self.assertIn("equity_curve", result)
        self.assertIn("markers", result)

    def test_03_wave1_all_modes_dispatched(self):
        for mode in sorted(StrategyRegistry.WAVE1_STRATEGIES):
            with self.subTest(mode=mode):
                result = self._run_wave1(mode)
                self._assert_v2_fields(result, mode)

    def test_03_wave1_run_metadata_has_timeframe(self):
        result = self._run_wave1("smc_wave1")
        meta = result.get("run_metadata", {})
        self.assertEqual(meta.get("timeframe"), "M15")
        self.assertIn("bars_analyzed", meta)
        self.assertEqual(meta["bars_analyzed"], 20)


class TestT5394TimeframeValidation(unittest.TestCase):
    """test_04: Wave1 rejects H1/D1 with ValueError; M1/M5/M15 accepted."""

    def _run(self, mode: str, timeframe: str) -> dict:
        df = _make_df_utc(20, timeframe_minutes=15)
        engine = _make_engine()
        return engine.run(df, mode, {}, timeframe=timeframe)

    def test_04_h1_rejected_for_all_wave1_modes(self):
        for mode in sorted(StrategyRegistry.WAVE1_STRATEGIES):
            with self.subTest(mode=mode, tf="H1"):
                with self.assertRaises(ValueError) as ctx:
                    self._run(mode, "H1")
                self.assertIn("H1", str(ctx.exception))
                self.assertIn("M1", str(ctx.exception))

    def test_04_d1_rejected(self):
        with self.assertRaises(ValueError):
            self._run("smc_wave1", "D1")

    def test_04_m30_rejected(self):
        with self.assertRaises(ValueError):
            self._run("smc_s01", "M30")

    def test_04_m1_accepted(self):
        result = self._run("smc_s01", "M1")
        self.assertEqual(result.get("mode"), "smc_s01")

    def test_04_m5_accepted(self):
        result = self._run("smc_s05", "M5")
        self.assertEqual(result.get("mode"), "smc_s05")

    def test_04_m15_accepted(self):
        result = self._run("smc_s09", "M15")
        self.assertEqual(result.get("mode"), "smc_s09")

    def test_04_wave1_allowed_timeframes_constant(self):
        self.assertEqual(WAVE1_ALLOWED_TIMEFRAMES, frozenset({"M1", "M5", "M15"}))


class TestT5394HTFEventsInput(unittest.TestCase):
    """test_05: HTF events from API request as JSON dicts are parsed and recorded."""

    def _run_with_htf(self, htf_events) -> dict:
        df = _make_df_utc(20, timeframe_minutes=15)
        engine = _make_engine()
        return engine.run(df, "smc_wave1", {}, timeframe="M15", htf_events=htf_events)

    def test_05_htf_events_accepted_as_dicts(self):
        # An event that happened BEFORE the first bar (so it should be effective)
        ev = _make_htf_event_dict(
            bar_idx=0,
            time_str="2024-01-02T09:00:00+00:00",
        )
        result = self._run_with_htf([ev])
        self.assertEqual(result.get("schema_version"), "2.0.0")
        meta = result.get("run_metadata", {})
        self.assertEqual(meta.get("htf_events_count"), 1)

    def test_05_no_htf_events_is_fine(self):
        result = self._run_with_htf(None)
        meta = result.get("run_metadata", {})
        self.assertEqual(meta.get("htf_events_count"), 0)

    def test_05_empty_htf_events_list_is_fine(self):
        result = self._run_with_htf([])
        meta = result.get("run_metadata", {})
        self.assertEqual(meta.get("htf_events_count"), 0)


class TestT5394HTFNoLookahead(unittest.TestCase):
    """test_06: HTF future event withheld — does not influence bars before effective time."""

    def test_06_future_event_not_visible_before_effective_time(self):
        # Create df with 10 M15 bars from 10:00 UTC
        base = pd.Timestamp("2024-01-02 10:00:00", tz="UTC")
        freq = pd.Timedelta(minutes=15)
        n = 10
        rows = []
        price = 2000.0
        for i in range(n):
            ts = base + freq * i
            rows.append({
                "bar_index": i,
                "time": ts,
                "open": round(price, 2),
                "high": round(price + 2.0, 2),
                "low": round(price - 2.0, 2),
                "close": round(price + 0.5, 2),
                "volume": 100.0,
            })
            price += 0.2
        df = pd.DataFrame(rows)

        # A far-future event (beyond all bars)
        far_future = "2030-01-01T00:00:00+00:00"
        htf_future = [_make_htf_event_dict(bar_idx=99999, time_str=far_future)]

        engine_without = _make_engine()
        engine_with = _make_engine()

        result_without = engine_without.run(df, "smc_wave1", {}, timeframe="M15", htf_events=None)
        result_with = engine_with.run(df, "smc_wave1", {}, timeframe="M15", htf_events=htf_future)

        # The future event must not change any output
        self.assertEqual(
            result_without["metrics"]["total_trades"],
            result_with["metrics"]["total_trades"],
            "Future HTF event must not influence trade count",
        )
        self.assertEqual(
            len(result_without["decisions"]),
            len(result_with["decisions"]),
            "Future HTF event must not change decision count",
        )


class TestT5394HTFBoundaryEvent(unittest.TestCase):
    """test_07: HTF event with effective_time == bar_close_time is admitted."""

    def test_07_boundary_event_admitted(self):
        # Bar 0 opens at 10:00 UTC and closes at 10:15 UTC (M15)
        # Event effective at exactly 10:15 UTC should be admitted at bar 0
        bar_open = pd.Timestamp("2024-01-02 10:00:00", tz="UTC")
        bar_close = bar_open + pd.Timedelta(minutes=15)  # 10:15

        rows = []
        price = 2000.0
        for i in range(10):
            ts = bar_open + pd.Timedelta(minutes=15) * i
            rows.append({
                "bar_index": i,
                "time": ts,
                "open": price,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price + 0.3,
                "volume": 100.0,
            })
        df = pd.DataFrame(rows)

        engine = _make_engine()
        boundary_ev = _make_htf_event_dict(
            bar_idx=0,
            time_str=bar_close.isoformat(),  # exactly at bar_close_time
        )
        # Should not raise; boundary is accepted
        result = engine.run(df, "smc_wave1", {}, timeframe="M15", htf_events=[boundary_ev])
        self.assertEqual(result.get("schema_version"), "2.0.0")
        meta = result.get("run_metadata", {})
        self.assertEqual(meta.get("htf_events_count"), 1)


class TestT5394ConflictingHTFEvents(unittest.TestCase):
    """test_08: Conflicting duplicate HTF events (same identity key, different payload) raise ValueError."""

    def test_08_conflicting_duplicate_rejected(self):
        engine = _make_engine()
        df = _make_df_utc(10)
        # Two events with same (index, time, event_type, direction) but different broken_swing_price
        ev1 = _make_htf_event_dict(bar_idx=0, bsp=1990.0, cp=1995.0)
        ev2 = _make_htf_event_dict(bar_idx=0, bsp=1985.0, cp=1995.0)  # different bsp

        with self.assertRaises(ValueError) as ctx:
            engine.run(df, "smc_wave1", {}, timeframe="M15", htf_events=[ev1, ev2])
        err = str(ctx.exception)
        # Should mention conflict
        self.assertTrue(
            "Conflicting" in err or "conflicting" in err or "conflict" in err.lower(),
            f"Expected conflict message, got: {err}",
        )

    def test_08_identical_duplicate_accepted_deduplicated(self):
        """Exact duplicate (same payload) should be silently deduplicated inside HTFTimeline.
        htf_events_count reflects the number of *parsed* (pre-dedup) events passed in."""
        engine = _make_engine()
        df = _make_df_utc(10)
        ev = _make_htf_event_dict()
        result = engine.run(df, "smc_wave1", {}, timeframe="M15", htf_events=[ev, ev])
        meta = result.get("run_metadata", {})
        # 2 identical dicts are passed → 2 parsed events (dedup happens inside HTFTimeline)
        self.assertEqual(meta.get("htf_events_count"), 2)


class TestT5394InvalidInputs(unittest.TestCase):
    """test_09: NaN, Inf, missing timezone, bad event_type are all rejected with ValueError/TypeError."""

    def _run(self, htf_events) -> dict:
        df = _make_df_utc(10)
        engine = _make_engine()
        return engine.run(df, "smc_wave1", {}, timeframe="M15", htf_events=htf_events)

    def test_09_nan_broken_swing_price_rejected(self):
        ev = _make_htf_event_dict(bsp=float("nan"))
        with self.assertRaises(ValueError):
            self._run([ev])

    def test_09_inf_close_price_rejected(self):
        ev = _make_htf_event_dict(cp=float("inf"))
        with self.assertRaises(ValueError):
            self._run([ev])

    def test_09_missing_timezone_rejected(self):
        ev = _make_htf_event_dict(time_str="2024-01-02 10:00:00")  # no tz
        with self.assertRaises((ValueError, TypeError)):
            self._run([ev])

    def test_09_invalid_event_type_rejected(self):
        ev = dict(_make_htf_event_dict())
        ev["event_type"] = "UNKNOWN"
        with self.assertRaises(ValueError):
            self._run([ev])

    def test_09_invalid_direction_rejected(self):
        ev = dict(_make_htf_event_dict())
        ev["direction"] = "sideways"
        with self.assertRaises(ValueError):
            self._run([ev])

    def test_09_missing_required_field_rejected(self):
        ev = {"index": 0, "time": "2024-01-02T10:00:00+00:00", "event_type": "BOS"}
        # Missing direction, broken_swing_price, close_price
        with self.assertRaises((ValueError, KeyError)):
            self._run([ev])

    def test_09_non_dict_htf_event_rejected(self):
        with self.assertRaises((ValueError, TypeError)):
            self._run(["not_a_dict"])

    def test_09_nan_min_rr_rejected(self):
        df = _make_df_utc(10)
        engine = _make_engine()
        with self.assertRaises(ValueError):
            engine.run(df, "smc_wave1", {"min_rr": float("nan")}, timeframe="M15")

    def test_09_negative_cooldown_bars_rejected(self):
        df = _make_df_utc(10)
        engine = _make_engine()
        with self.assertRaises(ValueError):
            engine.run(df, "smc_wave1", {"cooldown_bars": -1}, timeframe="M15")


class TestT5394ResponseSchema(unittest.TestCase):
    """test_10: Wave1 response is fully JSON-serialisable with all V2 fields."""

    def test_10_wave1_response_schema_and_json_serializable(self):
        df = _make_df_utc(25, timeframe_minutes=15)
        engine = _make_engine()
        htf_ev = _make_htf_event_dict(
            bar_idx=0,
            time_str="2024-01-02T09:00:00+00:00",
        )
        result = engine.run(df, "smc_wave1", {}, timeframe="M15", htf_events=[htf_ev])

        # V2 fields present
        self.assertIn("mode", result)
        self.assertIn("schema_version", result)
        self.assertIn("execution_events", result)
        self.assertIn("decisions", result)
        self.assertIn("pending_intents", result)
        self.assertIn("cooldown_snapshot", result)
        self.assertIn("run_metadata", result)

        # Standard fields present
        self.assertIn("metrics", result)
        self.assertIn("trades", result)
        self.assertIn("equity_curve", result)
        self.assertIn("markers", result)

        # Metrics have all required keys
        metrics = result["metrics"]
        for key in (
            "initial_capital", "final_balance", "net_profit", "return_pct",
            "total_trades", "winning_trades", "losing_trades",
            "win_rate", "profit_factor", "max_drawdown", "max_drawdown_pct",
        ):
            self.assertIn(key, metrics, f"metrics missing key: {key}")

        # 100% JSON-serialisable (no Timestamp, numpy types, etc.)
        try:
            serialised = json.dumps(result)
        except (TypeError, ValueError) as e:
            self.fail(f"Wave1 response is not JSON-serialisable: {e}")

        # Roundtrip sanity check
        loaded = json.loads(serialised)
        self.assertEqual(loaded["schema_version"], "2.0.0")
        self.assertEqual(loaded["mode"], "smc_wave1")

    def test_10_all_wave1_modes_json_safe(self):
        df = _make_df_utc(20, timeframe_minutes=15)
        engine = _make_engine()
        for mode in sorted(StrategyRegistry.WAVE1_STRATEGIES):
            with self.subTest(mode=mode):
                result = engine.run(df, mode, {}, timeframe="M15")
                try:
                    json.dumps(result)
                except (TypeError, ValueError) as e:
                    self.fail(f"Mode {mode!r} response is not JSON-serialisable: {e}")

    def test_10_legacy_response_still_json_safe(self):
        df = _make_df(30)  # tz-naive for legacy
        engine = _make_engine()
        result = engine.run(df, "sma_crossover", {"fast_period": 5, "slow_period": 10})
        try:
            json.dumps(result)
        except (TypeError, ValueError) as e:
            self.fail(f"Legacy response is not JSON-serialisable: {e}")


class TestWave1StructureModeWiring(unittest.TestCase):
    """Regression test suite verifying Wave 1 structure_mode == 'internal' wiring."""

    def test_wave1_engine_context_builder_receives_internal_mode(self):
        """Wave 1 backtest adapter constructs ContextBuilderConfig with structure_mode='internal'."""
        from unittest.mock import patch
        from smc.engine.backtest_adapter import SMCBacktestCoordinator

        df = _make_df_utc(10, timeframe_minutes=15)
        engine = _make_engine()

        orig_init = SMCBacktestCoordinator.__init__
        captured_configs = []

        def captured_init(self_coord, *args, **kwargs):
            captured_configs.append(kwargs.get("context_config"))
            orig_init(self_coord, *args, **kwargs)

        with patch.object(SMCBacktestCoordinator, "__init__", side_effect=captured_init, autospec=True):
            engine.run(df, "smc_wave1", {}, timeframe="M15")

        self.assertEqual(len(captured_configs), 1)
        ctx_cfg = captured_configs[0]
        self.assertIsNotNone(ctx_cfg)
        self.assertEqual(ctx_cfg.structure_mode, "internal")

    def test_wave1_strategies_and_evidence_mode_alignment(self):
        """smc_s01, smc_s05, smc_s09 configs require 'internal' mode matching context_config."""
        from smc.engine.context import ContextBuilderConfig, StrategyContextBuilder
        from smc.engine.strategies.s01_ict_2022 import S01ICT2022Strategy
        from smc.engine.strategies.s05_bos_ob_retest import S05BOSOBRetestStrategy
        from smc.engine.strategies.s09_ict_silver_bullet import S09ICTSilverBulletStrategy

        s01 = S01ICT2022Strategy()
        s05 = S05BOSOBRetestStrategy()
        s09 = S09ICTSilverBulletStrategy()

        self.assertEqual(s01.config.mode, "internal")
        self.assertEqual(s05.config.mode, "internal")
        self.assertEqual(s09.config.mode, "internal")

        # Context builder with internal mode produces internal evidence
        builder = StrategyContextBuilder(ContextBuilderConfig(timeframe="M15", structure_mode="internal"))
        self.assertEqual(builder._config.structure_mode, "internal")

    def test_context_builder_default_swing_unmodified(self):
        """ContextBuilder outside Wave1 preserves default structure_mode='swing'."""
        from smc.engine.context import ContextBuilderConfig
        cfg = ContextBuilderConfig()
        self.assertEqual(cfg.structure_mode, "swing")
        cfg_m15 = ContextBuilderConfig(timeframe="M15")
        self.assertEqual(cfg_m15.structure_mode, "swing")

    def test_smc_confluence_legacy_isolation(self):
        """Legacy smc_confluence remains unaffected by Wave1 internal mode wiring."""
        df = _make_df(30)
        engine = _make_engine()
        result = engine.run(
            df,
            "smc_confluence",
            {"swing_strength": 7, "internal_strength": 3, "bias_timing": "pre_candle", "rr_ratio": 1.5},
        )
        self.assertIn("trades", result)
        self.assertIn("metrics", result)
        self.assertNotIn("schema_version", result)  # Legacy schema, not Wave1 V2


if __name__ == "__main__":
    unittest.main(verbosity=2)

