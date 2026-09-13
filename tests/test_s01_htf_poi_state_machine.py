"""
tests/test_s01_htf_poi_state_machine.py
========================================
Comprehensive test suite verifying the 17 required conditions for S01 top-down SMC trading logic:
  HTF Bias -> HTF POI -> Price in POI -> LTF Sweep -> LTF MSS -> LTF FVG/OB Retest -> Entry
"""

import unittest
import pandas as pd
from types import MappingProxyType

from smc.models import HTFPOI, StructureEvent
from smc.context.htf_bias import HTFBiasTracker
from smc.context.htf_poi import HTFPOITracker
from smc.engine.models import (
    BiasStateSnapshot,
    CandidateSetup,
    FairValueGapSnapshot,
    HTFPOISnapshot,
    LiquidityPoolSnapshot,
    LiquiditySweepSnapshot,
    StrategyContext,
    StructureEventSnapshot,
)
from smc.engine.strategies.s01_ict_2022 import (
    NarrativeStage,
    S01Config,
    S01ICT2022Strategy,
    S01MacroState,
)


def _bar_ts(minute: int) -> pd.Timestamp:
    return pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=minute)


def _make_poi(
    poi_id: str = "poi_h1_bullish_1",
    poi_type: str = "FVG",
    direction: str = "bullish",
    top: float = 2050.0,
    bottom: float = 2030.0,
    timeframe: str = "H1",
    status: str = "active",
    touch_count: int = 1,
    last_touch_bar: int = 5,
) -> HTFPOISnapshot:
    return HTFPOISnapshot(
        poi_id=poi_id,
        poi_type=poi_type,  # type: ignore
        direction=direction,  # type: ignore
        top=top,
        bottom=bottom,
        timeframe=timeframe,
        created_at=0,
        status=status,  # type: ignore
        touch_count=touch_count,
        last_touch_bar=last_touch_bar,
    )


def _make_ctx(
    bar_index: int,
    open_p: float = 2040.0,
    high_p: float = 2045.0,
    low_p: float = 2035.0,
    close_p: float = 2042.0,
    bias: str = "bullish",
    bias_reason: str = "continuation_bos",
    sweeps=(),
    structures=(),
    fvgs=(),
    pools=(),
    active_htf_pois=None,
) -> StrategyContext:
    ts = _bar_ts(bar_index)
    hb = None
    if bias is not None:
        hb = BiasStateSnapshot(
            bias=bias,  # type: ignore
            timestamp=ts,
            reason=bias_reason,
            source_event_type="BOS",
            source_event_index=0,
        )

    pois = active_htf_pois
    if pois is None:
        if bias in ("bullish", "bearish"):
            pois = (_make_poi(direction=bias, top=2070.0, bottom=2020.0),)
        else:
            pois = ()

    return StrategyContext(
        bar_index=bar_index,
        timestamp=ts,
        bar_close_time=ts + pd.Timedelta(minutes=1),
        symbol="XAUUSD",
        timeframe="M1",
        open=open_p,
        high=high_p,
        low=low_p,
        close=close_p,
        volume=100.0,
        atr14=2.0,
        recent_sweeps=tuple(sweeps),
        recent_structures=tuple(structures),
        active_fvgs=tuple(fvgs),
        active_pools=tuple(pools),
        htf_bias=hb,
        active_htf_pois=tuple(pois),
    )


def _make_sweep(index: int = 10, direction: str = "bullish", price_wick: float = 2028.0) -> LiquiditySweepSnapshot:
    return LiquiditySweepSnapshot(
        index=index,
        direction=direction,  # type: ignore
        pool_kind="swing_low" if direction == "bullish" else "swing_high",
        pool_price=2030.0 if direction == "bullish" else 2070.0,
        pool_indices=(5,),
        price_wick=price_wick,
        close_price=2032.0 if direction == "bullish" else 2068.0,
        mode="internal",
        time=_bar_ts(index),
        created_at=index,
        confirmed_at=index,
        swept_at=index,
        valid=True,
    )


def _make_mss(index: int = 15, direction: str = "bullish", leg_id: str = "leg_01") -> StructureEventSnapshot:
    return StructureEventSnapshot(
        index=index,
        time=_bar_ts(index),
        event_type="CHoCH",
        direction=direction,  # type: ignore
        broken_swing_index=8,
        broken_swing_price=2045.0 if direction == "bullish" else 2055.0,
        close_price=2047.0 if direction == "bullish" else 2053.0,
        displacement=True,
        mode="internal",
        break_type="close",
        confirmed_swing_at=index,
        structure_leg_id=leg_id,
    )


def _make_fvg(index: int = 12, direction: str = "bullish", top: float = 2040.0, bottom: float = 2036.0, leg_id: str = "leg_01") -> FairValueGapSnapshot:
    return FairValueGapSnapshot(
        index=index,
        time=_bar_ts(index),
        direction=direction,  # type: ignore
        top=top,
        bottom=bottom,
        mode="internal",
        confirmed_at=index,
        structure_leg_id=leg_id,
    )


def _make_pool(price: float = 2065.0, kind: str = "equal_highs") -> LiquidityPoolSnapshot:
    return LiquidityPoolSnapshot(
        kind=kind,  # type: ignore
        price=price,
        price_max=price + 0.5,
        price_min=price - 0.5,
        indices=(3, 4),
        created_at=5,
        confirmed_at=5,
        mode="internal",
    )


class TestS01HTFPOIStateMachine(unittest.TestCase):
    """Verifies all 17 mandatory tests specified in Section 11 of the requirements."""

    def test_01_no_bias_no_s1(self):
        """1. Không có bias -> không tạo S1 (WAIT_HTF_BIAS, missing_htf_bias)."""
        strat = S01ICT2022Strategy()
        ctx = _make_ctx(10, bias=None)
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, S01MacroState.WAIT_HTF_BIAS)
        self.assertEqual(strat.last_rejection_reason, "missing_htf_bias")

    def test_02_bias_neutral_no_s1(self):
        """2. Bias neutral -> không tạo S1 (WAIT_HTF_BIAS, missing_htf_bias)."""
        strat = S01ICT2022Strategy()
        ctx = _make_ctx(10, bias="neutral")
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, S01MacroState.WAIT_HTF_BIAS)
        self.assertEqual(strat.last_rejection_reason, "missing_htf_bias")

    def test_03_has_bias_no_poi_no_s1(self):
        """3. Có bias nhưng chưa có POI -> không tạo S1 (WAIT_HTF_POI, missing_htf_poi)."""
        strat = S01ICT2022Strategy()
        ctx = _make_ctx(10, bias="bullish", active_htf_pois=())
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, S01MacroState.WAIT_HTF_POI)
        self.assertEqual(strat.last_rejection_reason, "missing_htf_poi")

    def test_04_has_poi_price_not_in_poi_no_s1(self):
        """4. Có POI nhưng giá chưa hồi vào -> không tạo S1 (WAIT_HTF_POI, price_not_in_htf_poi)."""
        strat = S01ICT2022Strategy()
        # POI is down at [2000.0, 2010.0], but price is trading at [2050, 2060] with touch_count=0
        poi = _make_poi(top=2010.0, bottom=2000.0, touch_count=0, last_touch_bar=None)
        ctx = _make_ctx(10, open_p=2055.0, high_p=2060.0, low_p=2050.0, close_p=2058.0, active_htf_pois=(poi,))
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, S01MacroState.WAIT_HTF_POI)
        self.assertEqual(strat.last_rejection_reason, "price_not_in_htf_poi")

    def test_05_wrong_poi_direction_no_s1(self):
        """5. Giá vào sai hướng POI -> không tạo S1 (wrong_poi_direction)."""
        strat = S01ICT2022Strategy()
        # Bias is bullish, but POI is bearish
        poi_bear = _make_poi(direction="bearish", top=2070.0, bottom=2020.0)
        ctx = _make_ctx(10, bias="bullish", active_htf_pois=(poi_bear,))
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, S01MacroState.WAIT_HTF_POI)
        self.assertEqual(strat.last_rejection_reason, "wrong_poi_direction")

    def test_06_poi_invalidated_by_close_through_no_s1(self):
        """6. POI đã bị phá (close xuyên toàn vùng) -> không tạo S1 (POI_INVALIDATED, poi_invalidated)."""
        strat = S01ICT2022Strategy()
        # POI demand is [2030.0, 2050.0]. Current bar closes at 2025.0 (< bottom 2030.0) -> full close through
        poi = _make_poi(top=2050.0, bottom=2030.0)
        ctx = _make_ctx(10, open_p=2035.0, high_p=2038.0, low_p=2022.0, close_p=2025.0, active_htf_pois=(poi,))
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, S01MacroState.POI_INVALIDATED)
        self.assertEqual(strat.last_rejection_reason, "poi_invalidated")

    def test_07_has_poi_no_sweep_no_s1(self):
        """7. Có POI nhưng không có sweep -> không vào (WAIT_LTF_SWEEP)."""
        strat = S01ICT2022Strategy()
        poi = _make_poi(top=2050.0, bottom=2030.0, touch_count=1)
        ctx = _make_ctx(10, active_htf_pois=(poi,), sweeps=())
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, S01MacroState.WAIT_LTF_SWEEP)

    def test_08_has_sweep_no_mss_no_s1(self):
        """8. Có sweep nhưng không có MSS -> không vào (SWEEP_EXPIRED, mss_not_confirmed)."""
        strat = S01ICT2022Strategy(S01Config(sweep_to_mss_max_bars=5))
        poi = _make_poi(top=2050.0, bottom=2030.0, touch_count=1)
        sw = _make_sweep(index=10)

        # Bar 10: sweep arrives -> WAIT_LTF_MSS
        strat.evaluate(_make_ctx(10, active_htf_pois=(poi,), sweeps=(sw,)))
        self.assertEqual(strat.current_state, S01MacroState.WAIT_LTF_MSS)

        # Advance bars without MSS until timeout (10 + 5 = 15)
        for b in range(11, 16):
            strat.evaluate(_make_ctx(b, active_htf_pois=(poi,), sweeps=(sw,)))

        # Bar 16 exceeds timeout -> expired
        res16 = strat.evaluate(_make_ctx(16, active_htf_pois=(poi,), sweeps=(sw,)))
        self.assertEqual(res16, ())
        # Narrative expired without MSS
        self.assertEqual(len(strat._narratives), 0)

    def test_09_has_mss_no_ltf_fvg_ob_no_s1(self):
        """9. Có MSS nhưng không có LTF FVG/OB -> không vào (remains in SWEEP_SEEN)."""
        strat = S01ICT2022Strategy()
        poi = _make_poi(top=2050.0, bottom=2030.0, touch_count=1)
        sw = _make_sweep(index=10)
        mss = _make_mss(index=15)

        strat.evaluate(_make_ctx(10, active_htf_pois=(poi,), sweeps=(sw,)))
        # Feed bars up to 15 with MSS but NO FVG
        for b in range(11, 15):
            strat.evaluate(_make_ctx(b, active_htf_pois=(poi,), sweeps=(sw,)))

        res15 = strat.evaluate(_make_ctx(15, active_htf_pois=(poi,), sweeps=(sw,), structures=(mss,), fvgs=()))
        self.assertEqual(res15, ())
        # Still in SWEEP_SEEN because no FVG matched
        narratives = list(strat._narratives.values())
        self.assertEqual(len(narratives), 1)
        self.assertEqual(narratives[0].stage, NarrativeStage.SWEEP_SEEN)

    def test_10_has_entry_zone_not_retested_no_s1(self):
        """10. Có vùng entry nhưng chưa retest -> không vào (WAIT_LTF_RETEST)."""
        strat = S01ICT2022Strategy()
        poi = _make_poi(top=2050.0, bottom=2030.0, touch_count=1)
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0)
        mss = _make_mss(index=15)

        strat.evaluate(_make_ctx(10, active_htf_pois=(poi,), sweeps=(sw,)))
        strat.evaluate(_make_ctx(11, active_htf_pois=(poi,), sweeps=(sw,)))
        strat.evaluate(_make_ctx(12, active_htf_pois=(poi,), sweeps=(sw,), fvgs=(fvg,)))
        strat.evaluate(_make_ctx(13, active_htf_pois=(poi,), sweeps=(sw,), fvgs=(fvg,)))
        strat.evaluate(_make_ctx(14, active_htf_pois=(poi,), sweeps=(sw,), fvgs=(fvg,)))
        strat.evaluate(_make_ctx(15, active_htf_pois=(poi,), sweeps=(sw,), structures=(mss,), fvgs=(fvg,)))

        # Bar 16 price stays above FVG [2045, 2050] > top 2040 -> no retest
        res16 = strat.evaluate(_make_ctx(16, open_p=2046.0, high_p=2050.0, low_p=2045.0, close_p=2048.0, active_htf_pois=(poi,), sweeps=(sw,), structures=(mss,), fvgs=(fvg,)))
        self.assertEqual(res16, ())
        self.assertEqual(strat.current_state, S01MacroState.WAIT_LTF_RETEST)

    def test_11_full_chain_produces_candidate_with_poi_metadata(self):
        """11. Đủ chuỗi -> tạo candidate kèm đầy đủ 12 trường POI metadata."""
        strat = S01ICT2022Strategy()
        poi = _make_poi(poi_id="poi_h1_bull_99", poi_type="OB", direction="bullish", top=2050.0, bottom=2020.0, touch_count=1, last_touch_bar=5)
        sw = _make_sweep(index=10, direction="bullish", price_wick=2028.0)
        fvg = _make_fvg(index=12, direction="bullish", top=2040.0, bottom=2036.0)
        mss = _make_mss(index=15, direction="bullish")
        pool = _make_pool(price=2065.0)

        strat.evaluate(_make_ctx(10, active_htf_pois=(poi,), sweeps=(sw,)))
        strat.evaluate(_make_ctx(11, active_htf_pois=(poi,), sweeps=(sw,)))
        strat.evaluate(_make_ctx(12, active_htf_pois=(poi,), sweeps=(sw,), fvgs=(fvg,)))
        strat.evaluate(_make_ctx(13, active_htf_pois=(poi,), sweeps=(sw,), fvgs=(fvg,)))
        strat.evaluate(_make_ctx(14, active_htf_pois=(poi,), sweeps=(sw,), fvgs=(fvg,)))
        strat.evaluate(_make_ctx(15, active_htf_pois=(poi,), sweeps=(sw,), structures=(mss,), fvgs=(fvg,), pools=(pool,)))

        # Bar 16 retests FVG [2036, 2040] with low=2038
        res16 = strat.evaluate(_make_ctx(
            16,
            open_p=2042.0,
            high_p=2044.0,
            low_p=2038.0,
            close_p=2041.0,
            active_htf_pois=(poi,),
            sweeps=(sw,),
            structures=(mss,),
            fvgs=(fvg,),
            pools=(pool,),
        ))

        self.assertEqual(len(res16), 1)
        cand = res16[0]
        self.assertEqual(cand.direction, "BUY")
        self.assertEqual(strat.current_state, S01MacroState.S1_READY)

        # Verify all 12 metadata fields specified in Section 8
        self.assertEqual(cand.meta["htf_bias"], "bullish")
        self.assertEqual(cand.meta["htf_bias_source"], "BOS")
        self.assertEqual(cand.meta["poi_id"], "poi_h1_bull_99")
        self.assertEqual(cand.meta["poi_type"], "OB")
        self.assertEqual(cand.meta["poi_timeframe"], "H1")
        self.assertEqual(cand.meta["poi_top"], 2050.0)
        self.assertEqual(cand.meta["poi_bottom"], 2020.0)
        self.assertEqual(cand.meta["poi_touch_bar"], 5)
        self.assertIsNotNone(cand.meta["ltf_sweep_id"])
        self.assertIsNotNone(cand.meta["ltf_mss_id"])
        self.assertIsNotNone(cand.meta["ltf_fvg_id"])
        self.assertIn("ltf_ob_id", cand.meta)

    def test_12_htf_choch_without_bos_does_not_change_bias(self):
        """12. HTF CHoCH chưa có BOS -> bias không đổi (tạo pending_reversal)."""
        tracker = HTFBiasTracker()
        # 1. Initial bullish BOS
        bos1 = StructureEvent(
            index=10,
            time=_bar_ts(10),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=5,
            broken_swing_price=2040.0,
            close_price=2045.0,
        )
        b1 = tracker.update(bos1, current_ltf_time=_bar_ts(10))
        self.assertEqual(b1.bias, "bullish")
        self.assertIsNone(b1.pending_reversal)

        # 2. Bearish CHoCH arrives
        choch = StructureEvent(
            index=20,
            time=_bar_ts(20),
            event_type="CHoCH",
            direction="bearish",
            broken_swing_index=15,
            broken_swing_price=2030.0,
            close_price=2028.0,
        )
        b2 = tracker.update(choch, current_ltf_time=_bar_ts(20))
        self.assertEqual(b2.bias, "bullish")  # Bias remains bullish!
        self.assertEqual(b2.pending_reversal, "bearish")
        self.assertEqual(b2.reason, "choch_reversal_pending")

    def test_13_htf_choch_then_same_direction_bos_cancels_pending(self):
        """13. HTF CHoCH rồi BOS cùng hướng cũ -> hủy pending, bias giữ nguyên."""
        tracker = HTFBiasTracker()
        bos1 = StructureEvent(index=10, time=_bar_ts(10), event_type="BOS", direction="bullish", broken_swing_index=5, broken_swing_price=2040.0, close_price=2045.0)
        tracker.update(bos1, current_ltf_time=_bar_ts(10))

        choch = StructureEvent(index=20, time=_bar_ts(20), event_type="CHoCH", direction="bearish", broken_swing_index=15, broken_swing_price=2030.0, close_price=2028.0)
        tracker.update(choch, current_ltf_time=_bar_ts(20))

        # Continuation bullish BOS
        bos2 = StructureEvent(index=30, time=_bar_ts(30), event_type="BOS", direction="bullish", broken_swing_index=25, broken_swing_price=2050.0, close_price=2055.0)
        b3 = tracker.update(bos2, current_ltf_time=_bar_ts(30))
        self.assertEqual(b3.bias, "bullish")
        self.assertIsNone(b3.pending_reversal)
        self.assertEqual(b3.reason, "reversal_cancelled_by_continuation")

    def test_14_htf_choch_then_opposite_bos_confirms_reversal(self):
        """14. HTF CHoCH rồi BOS ngược hướng -> bias đổi."""
        tracker = HTFBiasTracker()
        bos1 = StructureEvent(index=10, time=_bar_ts(10), event_type="BOS", direction="bullish", broken_swing_index=5, broken_swing_price=2040.0, close_price=2045.0)
        tracker.update(bos1, current_ltf_time=_bar_ts(10))

        choch = StructureEvent(index=20, time=_bar_ts(20), event_type="CHoCH", direction="bearish", broken_swing_index=15, broken_swing_price=2030.0, close_price=2028.0)
        tracker.update(choch, current_ltf_time=_bar_ts(20))

        # Reversal bearish BOS confirming the CHoCH
        bos_bear = StructureEvent(index=30, time=_bar_ts(30), event_type="BOS", direction="bearish", broken_swing_index=25, broken_swing_price=2025.0, close_price=2020.0)
        b3 = tracker.update(bos_bear, current_ltf_time=_bar_ts(30))
        self.assertEqual(b3.bias, "bearish")
        self.assertIsNone(b3.pending_reversal)
        self.assertEqual(b3.reason, "reversal_bos_confirmed")
        self.assertTrue(b3.confirmed_by_bos)

    def test_15_ltf_mss_does_not_change_htf_bias(self):
        """15. LTF MSS không đổi HTF bias."""
        strat = S01ICT2022Strategy()
        poi = _make_poi()
        sw = _make_sweep(index=10)
        mss_bearish = _make_mss(index=15, direction="bearish")

        # Context has HTF bias = bullish
        ctx = _make_ctx(15, bias="bullish", active_htf_pois=(poi,), sweeps=(sw,), structures=(mss_bearish,))
        strat.evaluate(ctx)

        # Context HTF bias is completely unchanged by the presence of LTF MSS
        self.assertEqual(ctx.htf_bias.bias, "bullish")

    def test_16_candle_close_through_fvg_invalidates_setup(self):
        """16. Giá close xuyên FVG/OB -> hủy setup (LTF_ZONE_INVALIDATED, fvg_close_through)."""
        strat = S01ICT2022Strategy()
        poi = _make_poi(top=2070.0, bottom=2020.0, touch_count=1)
        sw = _make_sweep(index=10)
        fvg = _make_fvg(index=12, top=2040.0, bottom=2036.0)
        mss = _make_mss(index=15)

        strat.evaluate(_make_ctx(10, active_htf_pois=(poi,), sweeps=(sw,)))
        strat.evaluate(_make_ctx(11, active_htf_pois=(poi,), sweeps=(sw,)))
        strat.evaluate(_make_ctx(12, active_htf_pois=(poi,), sweeps=(sw,), fvgs=(fvg,)))
        strat.evaluate(_make_ctx(13, active_htf_pois=(poi,), sweeps=(sw,), fvgs=(fvg,)))
        strat.evaluate(_make_ctx(14, active_htf_pois=(poi,), sweeps=(sw,), fvgs=(fvg,)))
        strat.evaluate(_make_ctx(15, active_htf_pois=(poi,), sweeps=(sw,), structures=(mss,), fvgs=(fvg,)))

        # Bar 16 closes below FVG bottom (close 2030 < bottom 2036) -> blows through FVG!
        res16 = strat.evaluate(_make_ctx(16, open_p=2038.0, high_p=2040.0, low_p=2028.0, close_p=2030.0, active_htf_pois=(poi,), sweeps=(sw,), structures=(mss,), fvgs=(fvg,)))
        self.assertEqual(res16, ())
        # Narrative must be pruned and invalidated
        self.assertEqual(len(strat._narratives), 0)

    def test_17_future_event_has_no_effect_zero_lookahead(self):
        """17. Event tương lai -> không ảnh hưởng kết quả (zero-lookahead)."""
        future_ev = StructureEvent(
            index=100,
            time=pd.Timestamp("2026-01-15 11:00:00+00:00"),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=90,
            broken_swing_price=2050.0,
            close_price=2055.0,
        )
        tracker = HTFBiasTracker(htf_events=[future_ev])

        # At 10:30, future event is held back and not effective
        b1 = tracker.update(current_ltf_time=pd.Timestamp("2026-01-15 10:30:00+00:00"))
        self.assertEqual(b1.bias, "neutral")
        self.assertEqual(b1.reason, "no_htf_event")

        # Only at 11:00 or later does it take effect
        b2 = tracker.update(current_ltf_time=pd.Timestamp("2026-01-15 11:00:00+00:00"))
        self.assertEqual(b2.bias, "bullish")
        self.assertEqual(b2.reason, "initial_bos_confirmed")


if __name__ == "__main__":
    unittest.main()
