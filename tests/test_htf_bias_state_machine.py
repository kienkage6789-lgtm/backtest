"""
tests/test_htf_bias_state_machine.py
====================================
Comprehensive tests for the HTF Bias State Machine and S01 HTF Bias integration:
- Initial BOS confirmation (bullish / bearish)
- Continuation BOS (maintaining trend)
- CHoCH sets reversal_pending without flipping bias
- Opposite BOS after CHoCH confirms bias reversal
- Opposite BOS without preceding CHoCH cannot flip bias
- Continuation BOS cancels pending reversal
- CHoCH when neutral remains neutral
- Zero lookahead (future events ignored)
- Conflicting events at same timestamp yield neutral
- S01 rejection when neutral, direction mismatch, pending reversal handling
"""

import datetime
import unittest
import pandas as pd

from smc.models import (
    StructureEvent,
    BiasState,
)
from smc.engine.models import (
    BiasStateSnapshot,
    StrategyContext,
)
from smc.context.htf_bias import (
    get_htf_bias_at,
    map_htf_bias_to_ltf,
    HTFBiasTracker,
)
from smc.engine.strategies.s01_ict_2022 import S01ICT2022Strategy
from tests.test_smc_strategy_s01 import (
    _make_context,
    _make_sweep,
    _make_mss,
    _make_fvg,
    _make_pool,
    _feed,
)


def _make_event(
    idx: int,
    ts: str,
    event_type: str,
    direction: str,
) -> StructureEvent:
    t = pd.Timestamp(ts, tz="UTC")
    return StructureEvent(
        index=idx,
        time=t,
        event_type=event_type,
        direction=direction,
        broken_swing_index=max(0, idx - 2),
        broken_swing_price=2000.0,
        close_price=2005.0 if direction == "bullish" else 1995.0,
        displacement=True,
        mode="swing",
    )


class TestHTFBiasStateMachine(unittest.TestCase):

    def test_bos_bullish_initial(self):
        """1. BOS bullish from neutral -> bullish confirmed."""
        ev = _make_event(1, "2026-01-01 10:00:00", "BOS", "bullish")
        bias = get_htf_bias_at("2026-01-01 11:00:00 UTC", [ev])
        self.assertEqual(bias.bias, "bullish")
        self.assertTrue(bias.confirmed_by_bos)
        self.assertEqual(bias.reason, "initial_bos_confirmed")
        self.assertIsNone(bias.pending_reversal)

    def test_bos_bearish_initial(self):
        """2. BOS bearish from neutral -> bearish confirmed."""
        ev = _make_event(1, "2026-01-01 10:00:00", "BOS", "bearish")
        bias = get_htf_bias_at("2026-01-01 11:00:00 UTC", [ev])
        self.assertEqual(bias.bias, "bearish")
        self.assertTrue(bias.confirmed_by_bos)
        self.assertEqual(bias.reason, "initial_bos_confirmed")
        self.assertIsNone(bias.pending_reversal)

    def test_bos_bullish_continuation(self):
        """3. BOS bullish -> BOS bullish -> remains bullish (continuation_bos)."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "BOS", "bullish")
        ev2 = _make_event(2, "2026-01-01 11:00:00", "BOS", "bullish")
        bias = get_htf_bias_at("2026-01-01 12:00:00 UTC", [ev1, ev2])
        self.assertEqual(bias.bias, "bullish")
        self.assertEqual(bias.reason, "continuation_bos")
        self.assertIsNone(bias.pending_reversal)
        self.assertEqual(bias.source_event_index, 2)

    def test_bos_bearish_continuation(self):
        """4. BOS bearish -> BOS bearish -> remains bearish (continuation_bos)."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "BOS", "bearish")
        ev2 = _make_event(2, "2026-01-01 11:00:00", "BOS", "bearish")
        bias = get_htf_bias_at("2026-01-01 12:00:00 UTC", [ev1, ev2])
        self.assertEqual(bias.bias, "bearish")
        self.assertEqual(bias.reason, "continuation_bos")
        self.assertIsNone(bias.pending_reversal)
        self.assertEqual(bias.source_event_index, 2)

    def test_bullish_then_choch_bearish(self):
        """5. bullish -> CHoCH bearish -> bullish + pending bearish."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "BOS", "bullish")
        ev2 = _make_event(2, "2026-01-01 11:00:00", "CHoCH", "bearish")
        bias = get_htf_bias_at("2026-01-01 12:00:00 UTC", [ev1, ev2])
        self.assertEqual(bias.bias, "bullish")
        self.assertEqual(bias.pending_reversal, "bearish")
        self.assertEqual(bias.pending_reversal_event_type, "CHoCH")
        self.assertEqual(bias.pending_reversal_event_index, 2)
        self.assertEqual(bias.reason, "choch_reversal_pending")

    def test_bullish_then_choch_bearish_then_bos_bullish(self):
        """6. bullish -> CHoCH bearish -> BOS bullish -> bullish + pending cancelled."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "BOS", "bullish")
        ev2 = _make_event(2, "2026-01-01 11:00:00", "CHoCH", "bearish")
        ev3 = _make_event(3, "2026-01-01 12:00:00", "BOS", "bullish")
        bias = get_htf_bias_at("2026-01-01 13:00:00 UTC", [ev1, ev2, ev3])
        self.assertEqual(bias.bias, "bullish")
        self.assertIsNone(bias.pending_reversal)
        self.assertEqual(bias.reason, "reversal_cancelled_by_continuation")

    def test_bullish_then_choch_bearish_then_bos_bearish(self):
        """7. bullish -> CHoCH bearish -> BOS bearish -> bearish confirmed."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "BOS", "bullish")
        ev2 = _make_event(2, "2026-01-01 11:00:00", "CHoCH", "bearish")
        ev3 = _make_event(3, "2026-01-01 12:00:00", "BOS", "bearish")
        bias = get_htf_bias_at("2026-01-01 13:00:00 UTC", [ev1, ev2, ev3])
        self.assertEqual(bias.bias, "bearish")
        self.assertIsNone(bias.pending_reversal)
        self.assertEqual(bias.reason, "reversal_bos_confirmed")
        self.assertTrue(bias.confirmed_by_bos)
        self.assertEqual(bias.source_event_index, 3)

    def test_bearish_then_choch_bullish(self):
        """8. bearish -> CHoCH bullish -> bearish + pending bullish."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "BOS", "bearish")
        ev2 = _make_event(2, "2026-01-01 11:00:00", "CHoCH", "bullish")
        bias = get_htf_bias_at("2026-01-01 12:00:00 UTC", [ev1, ev2])
        self.assertEqual(bias.bias, "bearish")
        self.assertEqual(bias.pending_reversal, "bullish")
        self.assertEqual(bias.pending_reversal_event_type, "CHoCH")
        self.assertEqual(bias.pending_reversal_event_index, 2)
        self.assertEqual(bias.reason, "choch_reversal_pending")

    def test_bearish_then_choch_bullish_then_bos_bearish(self):
        """9. bearish -> CHoCH bullish -> BOS bearish -> bearish + pending cancelled."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "BOS", "bearish")
        ev2 = _make_event(2, "2026-01-01 11:00:00", "CHoCH", "bullish")
        ev3 = _make_event(3, "2026-01-01 12:00:00", "BOS", "bearish")
        bias = get_htf_bias_at("2026-01-01 13:00:00 UTC", [ev1, ev2, ev3])
        self.assertEqual(bias.bias, "bearish")
        self.assertIsNone(bias.pending_reversal)
        self.assertEqual(bias.reason, "reversal_cancelled_by_continuation")

    def test_bearish_then_choch_bullish_then_bos_bullish(self):
        """10. bearish -> CHoCH bullish -> BOS bullish -> bullish confirmed."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "BOS", "bearish")
        ev2 = _make_event(2, "2026-01-01 11:00:00", "CHoCH", "bullish")
        ev3 = _make_event(3, "2026-01-01 12:00:00", "BOS", "bullish")
        bias = get_htf_bias_at("2026-01-01 13:00:00 UTC", [ev1, ev2, ev3])
        self.assertEqual(bias.bias, "bullish")
        self.assertIsNone(bias.pending_reversal)
        self.assertEqual(bias.reason, "reversal_bos_confirmed")
        self.assertTrue(bias.confirmed_by_bos)
        self.assertEqual(bias.source_event_index, 3)

    def test_opposite_bos_without_choch_does_not_flip_bias(self):
        """Rule 3/4: Opposite BOS without preceding CHoCH does NOT flip bias."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "BOS", "bullish")
        ev2 = _make_event(2, "2026-01-01 11:00:00", "BOS", "bearish")  # no preceding CHoCH
        bias = get_htf_bias_at("2026-01-01 12:00:00 UTC", [ev1, ev2])
        self.assertEqual(bias.bias, "bullish")
        self.assertEqual(bias.source_event_index, 1)

    def test_choch_when_neutral_remains_neutral(self):
        """11. CHoCH when bias is neutral -> remains neutral."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "CHoCH", "bullish")
        bias = get_htf_bias_at("2026-01-01 11:00:00 UTC", [ev1])
        self.assertEqual(bias.bias, "neutral")
        self.assertFalse(bias.confirmed_by_bos)
        self.assertEqual(bias.reason, "no_confirmed_bias")

        ev2 = _make_event(2, "2026-01-01 10:00:00", "CHoCH", "bearish")
        bias2 = get_htf_bias_at("2026-01-01 11:00:00 UTC", [ev2])
        self.assertEqual(bias2.bias, "neutral")

    def test_future_events_zero_lookahead(self):
        """12. Future events (effective_time > current bar close) do not affect bias."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "BOS", "bullish")
        ev2 = _make_event(2, "2026-01-01 12:00:00", "BOS", "bearish")
        # Query before ev2 is effective (ev2 effective at 13:00)
        bias = get_htf_bias_at("2026-01-01 12:30:00 UTC", [ev1, ev2])
        self.assertEqual(bias.bias, "bullish")
        self.assertEqual(bias.source_event_index, 1)

    def test_conflicting_events_same_timestamp(self):
        """13. Conflicting events at same timestamp -> neutral / conflicting_events."""
        ev1 = _make_event(1, "2026-01-01 10:00:00", "BOS", "bullish")
        ev2 = _make_event(2, "2026-01-01 10:00:00", "BOS", "bearish")
        bias = get_htf_bias_at("2026-01-01 11:00:00 UTC", [ev1, ev2])
        self.assertEqual(bias.bias, "neutral")
        self.assertEqual(bias.reason, "conflicting_events")

    def test_tracker_streaming_matches_batch(self):
        """Streaming HTFBiasTracker produces identical output to batch evaluation."""
        events = [
            _make_event(1, "2026-01-01 10:00:00", "BOS", "bullish"),
            _make_event(2, "2026-01-01 11:00:00", "CHoCH", "bearish"),
            _make_event(3, "2026-01-01 12:00:00", "BOS", "bearish"),
            _make_event(4, "2026-01-01 13:00:00", "BOS", "bearish"),
        ]
        times = [
            "2026-01-01 10:30:00 UTC",
            "2026-01-01 11:00:00 UTC",
            "2026-01-01 11:30:00 UTC",
            "2026-01-01 12:00:00 UTC",
            "2026-01-01 12:30:00 UTC",
            "2026-01-01 13:00:00 UTC",
            "2026-01-01 13:30:00 UTC",
            "2026-01-01 14:00:00 UTC",
        ]
        tracker = HTFBiasTracker(htf_events=events)
        for t_str in times:
            t = pd.Timestamp(t_str)
            batch_bias = get_htf_bias_at(t, events)
            streaming_bias = tracker.update(current_ltf_time=t)
            self.assertEqual(batch_bias.bias, streaming_bias.bias, f"Mismatch at {t_str}")
            self.assertEqual(batch_bias.pending_reversal, streaming_bias.pending_reversal, f"Pending mismatch at {t_str}")
            self.assertEqual(batch_bias.reason, streaming_bias.reason, f"Reason mismatch at {t_str}")
            self.assertEqual(batch_bias.confirmed_by_bos, streaming_bias.confirmed_by_bos, f"Confirmed mismatch at {t_str}")


class TestS01HTFBiasIntegration(unittest.TestCase):

    def _setup_buy_sequence(self, strat: S01ICT2022Strategy):
        sw = _make_sweep(
            index=10,
            direction="bullish",
            pool_kind="swing_low",
            pool_price=2030.0,
            price_wick=2028.0,
        )
        fvg = _make_fvg(
            index=12,
            direction="bullish",
            top=2040.0,
            bottom=2036.0,
            structure_leg_id="leg_01",
        )
        mss = _make_mss(
            index=15,
            direction="bullish",
            event_type="CHoCH",
            broken_swing_price=2045.0,
            structure_leg_id="leg_01",
        )
        pool = _make_pool(price=2065.0, kind="equal_highs")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(pool,)))
        return sw, fvg, mss, pool

    def _setup_sell_sequence(self, strat: S01ICT2022Strategy):
        sw = _make_sweep(
            index=10,
            direction="bearish",
            pool_kind="swing_high",
            pool_price=2070.0,
            price_wick=2072.0,
        )
        fvg = _make_fvg(
            index=12,
            direction="bearish",
            top=2064.0,
            bottom=2060.0,
            structure_leg_id="leg_02",
        )
        mss = _make_mss(
            index=15,
            direction="bearish",
            event_type="BOS",
            broken_swing_price=2055.0,
            structure_leg_id="leg_02",
        )
        pool = _make_pool(price=2035.0, kind="equal_lows")

        _feed(strat, _make_context(bar_index=10, sweeps=(sw,)))
        _feed(strat, _make_context(bar_index=12, sweeps=(sw,), fvgs=(fvg,)))
        _feed(strat, _make_context(bar_index=15, sweeps=(sw,), structure_events=(mss,), fvgs=(fvg,), pools=(pool,)))
        return sw, fvg, mss, pool

    def test_s01_when_bias_neutral_no_candidate(self):
        """14. S01 when bias neutral -> does not create candidate."""
        strat = S01ICT2022Strategy()
        sw, fvg, mss, pool = self._setup_buy_sequence(strat)
        res = _feed(strat, _make_context(
            bar_index=18,
            low_p=2038.0,
            high_p=2042.0,
            close_p=2040.0,
            sweeps=(sw,),
            structure_events=(mss,),
            fvgs=(fvg,),
            pools=(pool,),
            bias="neutral",
        ))
        self.assertEqual(res, ())

    def test_s01_buy_when_bias_bearish_rejected(self):
        """15. S01 BUY setup when bias is bearish -> rejected."""
        strat = S01ICT2022Strategy()
        sw, fvg, mss, pool = self._setup_buy_sequence(strat)
        res = _feed(strat, _make_context(
            bar_index=18,
            low_p=2038.0,
            high_p=2042.0,
            close_p=2040.0,
            sweeps=(sw,),
            structure_events=(mss,),
            fvgs=(fvg,),
            pools=(pool,),
            bias="bearish",
        ))
        self.assertEqual(res, ())

    def test_s01_sell_when_bias_bullish_rejected(self):
        """16. S01 SELL setup when bias is bullish -> rejected."""
        strat = S01ICT2022Strategy()
        sw, fvg, mss, pool = self._setup_sell_sequence(strat)
        res = _feed(strat, _make_context(
            bar_index=17,
            open_p=2058.0,
            high_p=2062.0,
            low_p=2056.0,
            close_p=2059.0,
            sweeps=(sw,),
            structure_events=(mss,),
            fvgs=(fvg,),
            pools=(pool,),
            bias="bullish",
        ))
        self.assertEqual(res, ())

    def test_s01_buy_after_bullish_with_choch_bearish_pending_permitted(self):
        """17. S01 BUY after bullish bias + bearish CHoCH pending -> still permitted."""
        strat = S01ICT2022Strategy()
        sw, fvg, mss, pool = self._setup_buy_sequence(strat)
        bias_pending = BiasStateSnapshot(
            bias="bullish",
            timestamp=pd.Timestamp("2026-01-15 10:18:00+00:00"),
            pending_reversal="bearish",
            reason="choch_reversal_pending",
        )
        res = _feed(strat, _make_context(
            bar_index=18,
            low_p=2038.0,
            high_p=2042.0,
            close_p=2040.0,
            sweeps=(sw,),
            structure_events=(mss,),
            fvgs=(fvg,),
            pools=(pool,),
            htf_bias=bias_pending,
        ))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].direction, "BUY")

    def test_s01_buy_after_bias_reversed_to_bearish_rejected(self):
        """18. S01 BUY after bias flipped to bearish -> rejected."""
        strat = S01ICT2022Strategy()
        sw, fvg, mss, pool = self._setup_buy_sequence(strat)
        bias_reversed = BiasStateSnapshot(
            bias="bearish",
            timestamp=pd.Timestamp("2026-01-15 10:18:00+00:00"),
            reason="reversal_bos_confirmed",
            confirmed_by_bos=True,
        )
        res = _feed(strat, _make_context(
            bar_index=18,
            low_p=2038.0,
            high_p=2042.0,
            close_p=2040.0,
            sweeps=(sw,),
            structure_events=(mss,),
            fvgs=(fvg,),
            pools=(pool,),
            htf_bias=bias_reversed,
        ))
        self.assertEqual(res, ())

    def test_s01_sell_after_bias_reversed_to_bearish_permitted(self):
        """19. S01 SELL after bias flipped to bearish -> permitted."""
        strat = S01ICT2022Strategy()
        sw, fvg, mss, pool = self._setup_sell_sequence(strat)
        bias_reversed = BiasStateSnapshot(
            bias="bearish",
            timestamp=pd.Timestamp("2026-01-15 10:17:00+00:00"),
            reason="reversal_bos_confirmed",
            confirmed_by_bos=True,
        )
        res = _feed(strat, _make_context(
            bar_index=17,
            open_p=2058.0,
            high_p=2062.0,
            low_p=2056.0,
            close_p=2059.0,
            sweeps=(sw,),
            structure_events=(mss,),
            fvgs=(fvg,),
            pools=(pool,),
            htf_bias=bias_reversed,
        ))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].direction, "SELL")


if __name__ == "__main__":
    unittest.main()
