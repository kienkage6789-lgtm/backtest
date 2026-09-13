"""
tests/test_s05_ltf_confirmation_state_machine.py
================================================
Comprehensive 22-test suite verifying the SMC top-down logic for S05:
  HTF Bias -> HTF OB same direction -> Price touches HTF OB ->
  LTF MSS/CHoCH same direction -> Identify LTF FVG/OB ->
  Price retests LTF FVG/OB -> Candidate Setup -> Fill at next candle Open (N+1).
"""

import unittest
import copy
import pandas as pd
from typing import Optional, Sequence

from smc.engine.errors import StrategyStateError, StrategyValidationError
from smc.engine.models import (
    BiasStateSnapshot,
    CandidateSetup,
    FairValueGapSnapshot,
    HTFPOISnapshot,
    LiquidityPoolSnapshot,
    OrderBlockSnapshot,
    StrategyContext,
    StructureEventSnapshot,
)
from smc.engine.strategies.s05_bos_ob_retest import (
    S05Config,
    S05BOSOBRetestStrategy,
    S05MacroState,
)


def _bar_ts(minute: int) -> pd.Timestamp:
    return pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=minute)


def _make_htf_ob(
    poi_id: str = "htf_ob_bullish_1",
    direction: str = "bullish",
    top: float = 2050.0,
    bottom: float = 2040.0,
    created_at: int = 0,
    status: str = "active",
    touch_count: int = 1,
    last_touch_bar: int = 5,
    source_event_index: int = 0,
) -> HTFPOISnapshot:
    return HTFPOISnapshot(
        poi_id=poi_id,
        poi_type="OB",
        direction=direction,  # type: ignore
        top=top,
        bottom=bottom,
        timeframe="H1",
        created_at=created_at,
        status=status,  # type: ignore
        touch_count=touch_count,
        last_touch_bar=last_touch_bar,
        source_event={"event_type": "BOS", "index": source_event_index},  # type: ignore
    )


def _make_structure_event(
    index: int = 10,
    event_type: str = "CHoCH",
    direction: str = "bullish",
    broken_swing_index: int = 5,
    broken_swing_price: float = 2045.0,
    close_price: float = 2047.0,
    break_type: str = "close",
    displacement: bool = True,
    leg_id: str = "leg_01",
    confirmed_swing_at: Optional[int] = None,
) -> StructureEventSnapshot:
    return StructureEventSnapshot(
        index=index,
        time=_bar_ts(index),
        event_type=event_type,  # type: ignore
        direction=direction,  # type: ignore
        broken_swing_index=broken_swing_index,
        broken_swing_price=broken_swing_price,
        close_price=close_price,
        displacement=displacement,
        mode="internal",
        break_type=break_type,  # type: ignore
        confirmed_swing_at=confirmed_swing_at if confirmed_swing_at is not None else index,
        structure_leg_id=leg_id,
    )


def _make_fvg(
    index: int = 11,
    direction: str = "bullish",
    top: float = 2046.0,
    bottom: float = 2042.0,
    leg_id: str = "leg_01",
) -> FairValueGapSnapshot:
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


def _make_ob(
    index: int = 9,
    direction: str = "bullish",
    high: float = 2044.0,
    low: float = 2041.0,
    open_p: float = 2042.0,
    close_p: float = 2043.0,
    leg_id: str = "leg_01",
    source_event_index: int = 10,
    origin_type: str = "BOS",
) -> OrderBlockSnapshot:
    return OrderBlockSnapshot(
        index=index,
        time=_bar_ts(index),
        direction=direction,  # type: ignore
        high=high,
        low=low,
        open=open_p,
        close=close_p,
        mode="internal",
        origin_type=origin_type,  # type: ignore
        source_event_type=origin_type,  # type: ignore
        source_event_index=source_event_index,
        quality="base",  # type: ignore
        valid=True,
        created_at=index,
        structure_leg_id=leg_id,
    )


def _make_pool(
    price: float = 2060.0,
    kind: str = "swing_high",
    confirmed_at: int = 4,
    indices: Sequence[int] = (4,),
) -> LiquidityPoolSnapshot:
    return LiquidityPoolSnapshot(
        mode="internal",  # type: ignore
        kind=kind,  # type: ignore
        price=price,
        indices=tuple(indices),
        confirmed_at=confirmed_at,
        valid=True,
    )


def _make_context(
    bar_index: int,
    open_p: Optional[float] = None,
    high_p: float = 2048.0,
    low_p: float = 2043.0,
    close_p: Optional[float] = None,
    bias: Optional[str] = "bullish",
    structures=(),
    fvgs=(),
    obs=(),
    pools=(),
    active_htf_pois=None,
) -> StrategyContext:
    if high_p < low_p:
        high_p, low_p = low_p, high_p
    if open_p is None:
        open_p = (low_p + high_p) / 2.0
    if close_p is None:
        close_p = (low_p + high_p) / 2.0
    open_p = max(low_p, min(high_p, open_p))
    close_p = max(low_p, min(high_p, close_p))

    ts = _bar_ts(bar_index)
    hb = None
    if bias is not None:
        hb = BiasStateSnapshot(
            bias=bias,  # type: ignore
            timestamp=ts,
            reason="htf_trend",
            source_event_type="BOS",
            source_event_index=0,
        )

    htf_pois = active_htf_pois
    if htf_pois is None and bias in ("bullish", "bearish"):
        htf_pois = (_make_htf_ob(direction=bias),)
    elif htf_pois is None:
        htf_pois = ()

    return StrategyContext(
        bar_index=bar_index,
        timestamp=ts,
        bar_close_time=ts + pd.Timedelta(minutes=1),
        symbol="XAUUSD",
        timeframe="M15",
        open=open_p,
        high=high_p,
        low=low_p,
        close=close_p,
        volume=100.0,
        atr14=2.0,
        recent_structures=tuple(structures),
        active_fvgs=tuple(fvgs),
        active_obs=tuple(obs),
        active_pools=tuple(pools),
        htf_bias=hb,
        active_htf_pois=tuple(htf_pois),
    )


class TestS05LTFConfirmationStateMachine(unittest.TestCase):
    """Exhaustive 22-test suite for S05 LTF confirmation state machine."""

    def setUp(self):
        self.cfg_ltf = S05Config(
            require_ltf_confirmation=True,
            ltf_confirmation_event_types=("CHoCH", "BOS"),
            ltf_entry_zone="either",
            ltf_zone_expiry_bars=25,
            sl_anchor="ltf_zone",
            min_rr=1.5,
            fallback_rr=2.0,
        )

    # -------------------------------------------------------------------------
    # Test 1: HTF bias bullish accepts only bullish OB
    # -------------------------------------------------------------------------
    def test_01_htf_bias_bullish_accepts_only_bullish_ob(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        bearish_ob = _make_htf_ob(poi_id="htf_bear_1", direction="bearish", top=2060.0, bottom=2050.0)
        # Context with bullish bias but only bearish HTF OB
        ctx = _make_context(bar_index=0, bias="bullish", active_htf_pois=(bearish_ob,))
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(strat.current_state, S05MacroState.WAIT_HTF_OB)
        self.assertEqual(strat.last_rejection_reason, "missing_htf_ob")

        # Now supply bullish HTF OB
        strat.reset()
        bullish_ob = _make_htf_ob(poi_id="htf_bull_1", direction="bullish", top=2050.0, bottom=2040.0, touch_count=0)
        ctx2 = _make_context(bar_index=0, open_p=2055.0, high_p=2056.0, low_p=2052.0, close_p=2054.0, bias="bullish", active_htf_pois=(bullish_ob,))
        cands2 = strat.evaluate(ctx2)
        self.assertEqual(len(cands2), 0)
        self.assertEqual(strat.current_state, S05MacroState.WAIT_HTF_OB_RETEST)

    # -------------------------------------------------------------------------
    # Test 2: HTF bias bearish accepts only bearish OB
    # -------------------------------------------------------------------------
    def test_02_htf_bias_bearish_accepts_only_bearish_ob(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        bullish_ob = _make_htf_ob(poi_id="htf_bull_1", direction="bullish", top=2050.0, bottom=2040.0)
        ctx = _make_context(bar_index=0, bias="bearish", active_htf_pois=(bullish_ob,))
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(strat.current_state, S05MacroState.WAIT_HTF_OB)
        self.assertEqual(strat.last_rejection_reason, "missing_htf_ob")

        # Now supply bearish HTF OB
        strat.reset()
        bearish_ob = _make_htf_ob(poi_id="htf_bear_1", direction="bearish", top=2060.0, bottom=2050.0, touch_count=0)
        ctx2 = _make_context(bar_index=0, open_p=2045.0, high_p=2048.0, low_p=2042.0, close_p=2044.0, bias="bearish", active_htf_pois=(bearish_ob,))
        cands2 = strat.evaluate(ctx2)
        self.assertEqual(len(cands2), 0)
        self.assertEqual(strat.current_state, S05MacroState.WAIT_HTF_OB_RETEST)

    # -------------------------------------------------------------------------
    # Test 3: Price has not touched HTF OB -> stays in WAIT_HTF_OB_RETEST
    # -------------------------------------------------------------------------
    def test_03_no_confirmation_search_before_htf_ob_touch(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob = _make_htf_ob(top=2040.0, bottom=2030.0, touch_count=0, last_touch_bar=None)
        # Price is strictly above OB [2030, 2040], e.g. low = 2045
        mss = _make_structure_event(index=0, event_type="CHoCH", direction="bullish")
        ctx = _make_context(bar_index=0, open_p=2050.0, high_p=2055.0, low_p=2045.0, close_p=2052.0, structures=(mss,), active_htf_pois=(ob,))
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(strat.current_state, S05MacroState.WAIT_HTF_OB_RETEST)
        self.assertEqual(strat.last_rejection_reason, "price_not_in_htf_ob")

    # -------------------------------------------------------------------------
    # Test 4: MSS/CHoCH before OB touch rejected
    # -------------------------------------------------------------------------
    def test_04_mss_before_ob_touch_rejected(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=5)
        # MSS happened at bar 3, which is before touch_bar 5
        mss_early = _make_structure_event(index=3, event_type="CHoCH", direction="bullish")
        fvg = _make_fvg(index=4, direction="bullish")
        ctx = _make_context(bar_index=5, open_p=2048.0, high_p=2052.0, low_p=2042.0, close_p=2045.0, structures=(mss_early,), fvgs=(fvg,), active_htf_pois=(ob,))
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(strat.current_state, S05MacroState.WAIT_LTF_CONFIRMATION)

    # -------------------------------------------------------------------------
    # Test 5: MSS/CHoCH against bias rejected
    # -------------------------------------------------------------------------
    def test_05_mss_against_bias_rejected(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob = _make_htf_ob(direction="bullish", top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        # Bias is bullish, but structure event at bar 6 is bearish
        mss_bear = _make_structure_event(index=6, event_type="CHoCH", direction="bearish")
        ctx = _make_context(bar_index=6, bias="bullish", structures=(mss_bear,), active_htf_pois=(ob,))
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        # Narrative must be invalidated by opposite structure shift
        self.assertEqual(strat.current_state, S05MacroState.OPPOSITE_STRUCTURE_SHIFT)

    # -------------------------------------------------------------------------
    # Test 6: Wick break rejected as confirmation
    # -------------------------------------------------------------------------
    def test_06_wick_break_rejected(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        # Event at bar 4 is wick break
        mss_wick = _make_structure_event(index=4, event_type="CHoCH", break_type="wick")
        ctx = _make_context(bar_index=4, structures=(mss_wick,), active_htf_pois=(ob,))
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(strat.current_state, S05MacroState.WAIT_LTF_CONFIRMATION)

    # -------------------------------------------------------------------------
    # Test 7: BOS creating HTF OB cannot be reused as LTF confirmation
    # -------------------------------------------------------------------------
    def test_07_bos_creating_htf_ob_cannot_be_reused_as_ltf_confirmation(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        # HTF OB created by BOS at bar 2
        ob = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=5, source_event_index=2)
        bos_parent = _make_structure_event(index=2, event_type="BOS")
        ctx = _make_context(bar_index=5, structures=(bos_parent,), active_htf_pois=(ob,))
        cands = strat.evaluate(ctx)
        self.assertEqual(len(cands), 0)
        self.assertEqual(strat.current_state, S05MacroState.WAIT_LTF_CONFIRMATION)

    # -------------------------------------------------------------------------
    # Test 8: LTF FVG same structure leg selected
    # -------------------------------------------------------------------------
    def test_08_ltf_fvg_same_structure_leg_selected(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, event_type="CHoCH", leg_id="leg_target")
        # FVG 1 from different leg, FVG 2 from target leg (both available at bar 4)
        fvg_diff = _make_fvg(index=3, top=2047.0, bottom=2044.0, leg_id="other_leg")
        fvg_match = _make_fvg(index=4, top=2046.0, bottom=2043.0, leg_id="leg_target")
        
        # Advance bar 4 (confirmation seen and both FVGs present)
        ctx4 = _make_context(bar_index=4, structures=(conf,), fvgs=(fvg_diff, fvg_match), active_htf_pois=(ob,))
        strat.evaluate(ctx4)
        
        # Advance bar 5 where price retests fvg_match [2043, 2046]
        ctx5 = _make_context(bar_index=5, low_p=2044.0, high_p=2048.0, structures=(conf,), fvgs=(fvg_diff, fvg_match), active_htf_pois=(ob,))
        cands = strat.evaluate(ctx5)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].meta["ltf_entry_zone_id"], getattr(fvg_match, "zone_id", f"fvg_{fvg_match.index}"))

    # -------------------------------------------------------------------------
    # Test 9: LTF OB same structure leg selected
    # -------------------------------------------------------------------------
    def test_09_ltf_ob_same_structure_leg_selected(self):
        cfg = S05Config(require_ltf_confirmation=True, ltf_entry_zone="ob")
        strat = S05BOSOBRetestStrategy(cfg)
        ob_htf = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, event_type="BOS", leg_id="leg_alpha")
        ob_diff = _make_ob(index=3, leg_id="leg_beta", source_event_index=1)
        ob_match = _make_ob(index=4, leg_id="leg_alpha", source_event_index=4)

        ctx4 = _make_context(bar_index=4, structures=(conf,), active_htf_pois=(ob_htf,))
        strat.evaluate(ctx4)
        # Advance bar 5
        strat.evaluate(_make_context(bar_index=5, low_p=2046.0, high_p=2050.0, structures=(conf,), obs=(ob_diff, ob_match), active_htf_pois=(ob_htf,)))

        # Retest ob_match [2041, 2044] at bar 6
        ctx6 = _make_context(bar_index=6, low_p=2042.0, high_p=2046.0, structures=(conf,), obs=(ob_diff, ob_match), active_htf_pois=(ob_htf,))
        cands = strat.evaluate(ctx6)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].meta["ltf_entry_zone_type"], "ob")

    # -------------------------------------------------------------------------
    # Test 10: Mode 'fvg' only creates candidate when FVG exists
    # -------------------------------------------------------------------------
    def test_10_mode_fvg_only_creates_candidate_with_fvg(self):
        cfg = S05Config(require_ltf_confirmation=True, ltf_entry_zone="fvg")
        strat = S05BOSOBRetestStrategy(cfg)
        ob_htf = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, leg_id="leg_1")
        ob_ltf = _make_ob(index=4, leg_id="leg_1", source_event_index=4)

        strat.evaluate(_make_context(bar_index=4, structures=(conf,), active_htf_pois=(ob_htf,)))
        # Price touches LTF OB, but mode is 'fvg', so no candidate
        cands = strat.evaluate(_make_context(bar_index=5, low_p=2042.0, high_p=2045.0, structures=(conf,), obs=(ob_ltf,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands), 0)

        # Now supply FVG
        fvg = _make_fvg(index=4, leg_id="leg_1")
        cands2 = strat.evaluate(_make_context(bar_index=6, low_p=2043.0, high_p=2047.0, structures=(conf,), fvgs=(fvg,), obs=(ob_ltf,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands2), 1)
        self.assertEqual(cands2[0].meta["ltf_entry_zone_type"], "fvg")

    # -------------------------------------------------------------------------
    # Test 11: Mode 'ob' only creates candidate when OB exists
    # -------------------------------------------------------------------------
    def test_11_mode_ob_only_creates_candidate_with_ob(self):
        cfg = S05Config(require_ltf_confirmation=True, ltf_entry_zone="ob")
        strat = S05BOSOBRetestStrategy(cfg)
        ob_htf = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, leg_id="leg_1")
        fvg = _make_fvg(index=4, leg_id="leg_1")

        strat.evaluate(_make_context(bar_index=4, structures=(conf,), active_htf_pois=(ob_htf,)))
        # Only FVG present -> rejected in mode 'ob'
        cands = strat.evaluate(_make_context(bar_index=5, low_p=2043.0, high_p=2047.0, structures=(conf,), fvgs=(fvg,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands), 0)

        # Supply LTF OB
        ob_ltf = _make_ob(index=4, leg_id="leg_1", source_event_index=4)
        cands2 = strat.evaluate(_make_context(bar_index=6, low_p=2042.0, high_p=2046.0, structures=(conf,), fvgs=(fvg,), obs=(ob_ltf,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands2), 1)
        self.assertEqual(cands2[0].meta["ltf_entry_zone_type"], "ob")

    # -------------------------------------------------------------------------
    # Test 12: Mode 'either' works when only one zone exists
    # -------------------------------------------------------------------------
    def test_12_mode_either_works_with_single_zone_type(self):
        cfg = S05Config(require_ltf_confirmation=True, ltf_entry_zone="either")
        strat = S05BOSOBRetestStrategy(cfg)
        ob_htf = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, leg_id="leg_1")
        fvg = _make_fvg(index=4, leg_id="leg_1")

        strat.evaluate(_make_context(bar_index=4, structures=(conf,), active_htf_pois=(ob_htf,)))
        cands = strat.evaluate(_make_context(bar_index=5, low_p=2043.0, high_p=2047.0, structures=(conf,), fvgs=(fvg,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].meta["ltf_entry_zone_type"], "fvg")

    # -------------------------------------------------------------------------
    # Test 13: Mode 'confluence' rejects when missing one zone
    # -------------------------------------------------------------------------
    def test_13_mode_confluence_rejects_when_missing_one_zone(self):
        cfg = S05Config(require_ltf_confirmation=True, ltf_entry_zone="confluence")
        strat = S05BOSOBRetestStrategy(cfg)
        ob_htf = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, leg_id="leg_1")
        fvg = _make_fvg(index=4, leg_id="leg_1")

        strat.evaluate(_make_context(bar_index=4, structures=(conf,), active_htf_pois=(ob_htf,)))
        # Only FVG -> no confluence
        cands = strat.evaluate(_make_context(bar_index=5, low_p=2043.0, high_p=2047.0, structures=(conf,), fvgs=(fvg,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands), 0)

        # Add LTF OB -> both present
        ob_ltf = _make_ob(index=4, leg_id="leg_1", source_event_index=4)
        cands2 = strat.evaluate(_make_context(bar_index=6, low_p=2043.0, high_p=2047.0, structures=(conf,), fvgs=(fvg,), obs=(ob_ltf,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands2), 1)
        self.assertEqual(cands2[0].meta["ltf_entry_zone_type"], "confluence")

    # -------------------------------------------------------------------------
    # Test 14: No entry on confirmation bar itself
    # -------------------------------------------------------------------------
    def test_14_no_entry_on_confirmation_bar(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob_htf = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=5, leg_id="leg_1")
        fvg = _make_fvg(index=5, leg_id="leg_1")

        # On bar 5, confirmation happens and price is inside FVG
        ctx5 = _make_context(bar_index=5, low_p=2043.0, high_p=2047.0, structures=(conf,), fvgs=(fvg,), active_htf_pois=(ob_htf,))
        cands = strat.evaluate(ctx5)
        self.assertEqual(len(cands), 0, "Never enter on confirmation bar itself")

    # -------------------------------------------------------------------------
    # Test 15: Candidate generated only after LTF zone retest
    # -------------------------------------------------------------------------
    def test_15_candidate_only_generated_after_ltf_zone_retest(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob_htf = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, leg_id="leg_1")
        fvg = _make_fvg(index=4, top=2045.0, bottom=2042.0, leg_id="leg_1")

        # Bar 4: Confirmation seen
        strat.evaluate(_make_context(bar_index=4, structures=(conf,), active_htf_pois=(ob_htf,)))
        
        # Bar 5: Price moves away [2046, 2050], no touch of [2042, 2045]
        cands5 = strat.evaluate(_make_context(bar_index=5, open_p=2047.0, high_p=2050.0, low_p=2046.0, close_p=2048.0, structures=(conf,), fvgs=(fvg,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands5), 0)
        self.assertEqual(strat.current_state, S05MacroState.WAIT_LTF_RETEST)

        # Bar 6: Price pulls back into [2042, 2045] (low=2043)
        cands6 = strat.evaluate(_make_context(bar_index=6, open_p=2048.0, high_p=2049.0, low_p=2043.0, close_p=2046.0, structures=(conf,), fvgs=(fvg,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands6), 1)
        self.assertEqual(strat.current_state, S05MacroState.S05_READY)

    # -------------------------------------------------------------------------
    # Test 16: Fill occurs at next bar open (N+1)
    # -------------------------------------------------------------------------
    def test_16_fill_occurs_at_next_bar_open(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob_htf = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, leg_id="leg_1")
        fvg = _make_fvg(index=4, top=2045.0, bottom=2042.0, leg_id="leg_1")

        strat.evaluate(_make_context(bar_index=4, structures=(conf,), active_htf_pois=(ob_htf,)))
        cands = strat.evaluate(_make_context(bar_index=5, open_p=2048.0, high_p=2049.0, low_p=2043.0, close_p=2046.0, structures=(conf,), fvgs=(fvg,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands), 1)
        cand = cands[0]
        self.assertEqual(cand.bar_index, 5)
        self.assertEqual(cand.meta["signal_bar"], 5)
        self.assertEqual(cand.meta["available_from_bar"], 6)

    # -------------------------------------------------------------------------
    # Test 17: LTF zone closed through invalidates narrative
    # -------------------------------------------------------------------------
    def test_17_ltf_zone_closed_through_invalidates_narrative(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob_htf = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, leg_id="leg_1")
        fvg = _make_fvg(index=4, top=2045.0, bottom=2042.0, leg_id="leg_1")

        # Bar 4: Confirmation seen and FVG provided so narrative selects FVG and enters WAIT_LTF_RETEST
        strat.evaluate(_make_context(bar_index=4, structures=(conf,), fvgs=(fvg,), active_htf_pois=(ob_htf,)))
        # Bar 5 closes below FVG bottom (close = 2040 < 2042)
        cands = strat.evaluate(_make_context(bar_index=5, open_p=2044.0, high_p=2044.0, low_p=2039.0, close_p=2040.0, structures=(conf,), fvgs=(fvg,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands), 0)
        self.assertEqual(strat.current_state, S05MacroState.LTF_ZONE_INVALIDATED)
        self.assertEqual(strat.last_rejection_reason, "zone_invalidated_by_close")

    # -------------------------------------------------------------------------
    # Test 18: Opposite structure shift cancels setup
    # -------------------------------------------------------------------------
    def test_18_opposite_structure_shift_cancels_setup(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob_htf = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, direction="bullish", leg_id="leg_1")
        fvg = _make_fvg(index=4, direction="bullish", leg_id="leg_1")

        strat.evaluate(_make_context(bar_index=4, structures=(conf,), active_htf_pois=(ob_htf,)))
        # Opposite shift at bar 5 (bearish CHoCH)
        opp_shift = _make_structure_event(index=5, direction="bearish", event_type="CHoCH")
        cands = strat.evaluate(_make_context(bar_index=5, structures=(conf, opp_shift), fvgs=(fvg,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands), 0)
        self.assertEqual(strat.current_state, S05MacroState.OPPOSITE_STRUCTURE_SHIFT)
        self.assertEqual(strat.last_rejection_reason, "opposite_structure_shift")

    # -------------------------------------------------------------------------
    # Test 19: Candidate metadata contains all 9 required fields
    # -------------------------------------------------------------------------
    def test_19_candidate_metadata_contains_all_mandatory_fields(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob_htf = _make_htf_ob(poi_id="htf_ob_gold_1", top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, event_type="CHoCH", direction="bullish", leg_id="leg_1")
        fvg = _make_fvg(index=4, top=2045.0, bottom=2042.0, leg_id="leg_1")

        strat.evaluate(_make_context(bar_index=4, structures=(conf,), active_htf_pois=(ob_htf,)))
        cands = strat.evaluate(_make_context(bar_index=5, low_p=2043.0, high_p=2047.0, structures=(conf,), fvgs=(fvg,), active_htf_pois=(ob_htf,)))
        self.assertEqual(len(cands), 1)
        meta = cands[0].meta

        required_keys = (
            "htf_bias",
            "htf_ob_id",
            "htf_ob_touch_bar",
            "ltf_confirmation_id",
            "ltf_confirmation_type",
            "ltf_entry_zone_type",
            "ltf_entry_zone_id",
            "ltf_zone_touch_bar",
            "trade_direction",
        )
        for key in required_keys:
            self.assertIn(key, meta, f"Missing required metadata key: {key}")
            self.assertIsNotNone(meta[key], f"Key {key} must not be None")

        self.assertEqual(meta["htf_bias"], "bullish")
        self.assertEqual(meta["htf_ob_id"], "htf_ob_gold_1")
        self.assertEqual(meta["htf_ob_touch_bar"], 2)
        self.assertEqual(meta["ltf_confirmation_type"], "CHoCH")
        self.assertEqual(meta["ltf_entry_zone_type"], "fvg")
        self.assertEqual(meta["trade_direction"], "BUY")

    # -------------------------------------------------------------------------
    # Test 20: Batch, incremental, and replay parity
    # -------------------------------------------------------------------------
    def test_20_batch_incremental_replay_parity(self):
        ob_htf = _make_htf_ob(top=2050.0, bottom=2040.0, touch_count=1, last_touch_bar=2)
        conf = _make_structure_event(index=4, leg_id="leg_1")
        fvg = _make_fvg(index=4, top=2045.0, bottom=2042.0, leg_id="leg_1")

        ctx_list = [
            _make_context(bar_index=2, low_p=2044.0, high_p=2052.0, active_htf_pois=(ob_htf,)),
            _make_context(bar_index=3, low_p=2046.0, high_p=2052.0, active_htf_pois=(ob_htf,)),
            _make_context(bar_index=4, structures=(conf,), active_htf_pois=(ob_htf,)),
            _make_context(bar_index=5, low_p=2043.0, high_p=2047.0, structures=(conf,), fvgs=(fvg,), active_htf_pois=(ob_htf,)),
        ]

        # Run 1: Sequential incremental
        s1 = S05BOSOBRetestStrategy(self.cfg_ltf)
        cands_run1 = []
        for c in ctx_list:
            res = s1.evaluate(c)
            if res:
                cands_run1.extend(res)

        # Run 2: Reset & Replay
        s1.reset()
        cands_run2 = []
        for c in ctx_list:
            res = s1.evaluate(c)
            if res:
                cands_run2.extend(res)

        # Run 3: Fresh instance
        s2 = S05BOSOBRetestStrategy(self.cfg_ltf)
        cands_run3 = []
        for c in ctx_list:
            res = s2.evaluate(c)
            if res:
                cands_run3.extend(res)

        self.assertEqual(len(cands_run1), len(cands_run2))
        self.assertEqual(len(cands_run1), len(cands_run3))
        self.assertEqual([c.setup_id for c in cands_run1], [c.setup_id for c in cands_run2])
        self.assertEqual([c.setup_id for c in cands_run1], [c.setup_id for c in cands_run3])

    # -------------------------------------------------------------------------
    # Test 21: Feature flag False preserves legacy output
    # -------------------------------------------------------------------------
    def test_21_feature_flag_false_preserves_legacy_output(self):
        cfg_legacy = S05Config(require_ltf_confirmation=False)
        self.assertFalse(cfg_legacy.require_ltf_confirmation)
        strat = S05BOSOBRetestStrategy(cfg_legacy)

        bos = StructureEventSnapshot(
            index=2,
            time=_bar_ts(2),
            event_type="BOS",
            direction="bullish",
            broken_swing_index=1,
            broken_swing_price=2040.0,
            close_price=2045.0,
            displacement=True,
            mode="internal",
            break_type="close",
            structure_leg_id="leg_0",
        )
        ob_bar2 = OrderBlockSnapshot(
            index=2,
            time=_bar_ts(2),
            direction="bullish",
            high=2042.0,
            low=2038.0,
            open=2040.0,
            close=2041.0,
            mode="internal",
            origin_type="BOS",
            source_event_type="BOS",
            source_event_index=2,
            quality="base",
            valid=True,
            created_at=2,
            structure_leg_id="leg_0",
            retest_count=0,
            mitigated_at=None,
        )
        ob_bar3 = OrderBlockSnapshot(
            index=2,
            time=_bar_ts(2),
            direction="bullish",
            high=2042.0,
            low=2038.0,
            open=2040.0,
            close=2041.0,
            mode="internal",
            origin_type="BOS",
            source_event_type="BOS",
            source_event_index=2,
            quality="base",
            valid=True,
            created_at=2,
            structure_leg_id="leg_0",
            retest_count=1,
            mitigated_at=3,
        )

        strat.evaluate(_make_context(bar_index=2, structures=(bos,), obs=(ob_bar2,)))
        # Retest on bar 3
        cands = strat.evaluate(_make_context(bar_index=3, low_p=2040.0, high_p=2045.0, structures=(bos,), obs=(ob_bar3,)))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].meta.get("first_retest"), True)
        self.assertNotIn("flow_type", cands[0].meta)

    # -------------------------------------------------------------------------
    # Test 22: Zero lookahead guarantee
    # -------------------------------------------------------------------------
    def test_22_zero_lookahead_guarantee(self):
        strat = S05BOSOBRetestStrategy(self.cfg_ltf)
        ob_future = _make_ob(index=5, open_p=2042.0, close_p=2043.0)
        
        # At bar 2, OB from bar 5 must trigger StrategyStateError
        with self.assertRaises(StrategyStateError):
            strat.evaluate(_make_context(bar_index=2, obs=(ob_future,)))


if __name__ == "__main__":
    unittest.main()
