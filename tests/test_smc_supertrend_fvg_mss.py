"""
tests/test_smc_supertrend_fvg_mss.py
====================================
Comprehensive test suite verifying the 20 mandatory test cases for:
SMC — HTF Supertrend + FVG + LTF MSS Strategy.
"""

import unittest
import math
import pandas as pd
import numpy as np

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
from smc.engine.strategies.smc_supertrend_fvg_mss import (
    SMCSupertrendFVGMSSConfig,
    SMCSupertrendFVGMSSStrategy,
    SMCSupertrendMacroState,
)
from smc.context.htf_supertrend_fvg import HTFFairValueGap


def _bar_ts(minute: int) -> pd.Timestamp:
    return pd.Timestamp("2026-01-15 10:00:00+00:00") + pd.Timedelta(minutes=minute)


def _make_poi(
    poi_id: str = "htf_fvg_bull_1",
    direction: str = "bullish",
    top: float = 2050.0,
    bottom: float = 2030.0,
    touch_count: int = 1,
) -> HTFPOISnapshot:
    return HTFPOISnapshot(
        poi_id=poi_id,
        poi_type="FVG",  # type: ignore
        direction=direction,  # type: ignore
        top=top,
        bottom=bottom,
        timeframe="H1",
        created_at=0,
        status="active",  # type: ignore
        touch_count=touch_count,
        last_touch_bar=5,
    )


def _make_ctx(
    bar_index: int,
    open_p: float = 2040.0,
    high_p: float = 2045.0,
    low_p: float = 2035.0,
    close_p: float = 2042.0,
    bias: str = "bullish",
    sweeps=(),
    structures=(),
    fvgs=(),
    pools=(),
    active_htf_pois=None,
    meta=None,
) -> StrategyContext:
    ts = _bar_ts(bar_index)
    hb = None
    if bias is not None:
        hb = BiasStateSnapshot(
            bias=bias,  # type: ignore
            timestamp=ts,
            reason="supertrend_confirmed",
            source_event_type="SUPERTREND",
            source_event_index=0,
        )

    pois = active_htf_pois
    if pois is None:
        if bias in ("bullish", "bearish"):
            pois = (_make_poi(direction=bias, top=2070.0, bottom=2020.0),)
        else:
            pois = ()

    ctx_meta = dict(meta or {})
    if bias in ("bullish", "bearish"):
        ctx_meta["htf_supertrend"] = {"bias": bias, "trend": 1 if bias == "bullish" else -1}

    return StrategyContext(
        bar_index=bar_index,
        timestamp=ts,
        bar_close_time=ts + pd.Timedelta(minutes=5),
        symbol="XAUUSD",
        timeframe="M5",
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
        meta=ctx_meta,
    )


def _make_sweep(
    index: int = 10,
    direction: str = "bullish",
    price_wick: float = 2028.0,
) -> LiquiditySweepSnapshot:
    return LiquiditySweepSnapshot(
        index=index,
        direction=direction,  # type: ignore
        pool_kind="swing_low" if direction == "bullish" else "swing_high",
        pool_price=2030.0 if direction == "bullish" else 2070.0,
        pool_indices=(5,),
        price_wick=price_wick,
        close_price=2032.0 if direction == "bullish" else 2068.0,
        mode="internal",
        time=_bar_ts(index * 5),
        created_at=index,
        confirmed_at=index,
        swept_at=index,
        valid=True,
    )


def _make_mss(
    index: int = 15,
    direction: str = "bullish",
    break_type: str = "close",
    displacement: bool = True,
    leg_id: str = "leg_01",
) -> StructureEventSnapshot:
    return StructureEventSnapshot(
        index=index,
        time=_bar_ts(index * 5),
        event_type="CHoCH",
        direction=direction,  # type: ignore
        broken_swing_index=8,
        broken_swing_price=2045.0 if direction == "bullish" else 2055.0,
        close_price=2048.0 if direction == "bullish" else 2052.0,
        displacement=displacement,
        mode="internal",
        break_type=break_type,
        confirmed_swing_at=index,
        structure_leg_id=leg_id,
    )


def _make_fvg(
    index: int = 16,
    direction: str = "bullish",
    top: float = 2042.0,
    bottom: float = 2038.0,
    leg_id: str = "leg_01",
) -> FairValueGapSnapshot:
    return FairValueGapSnapshot(
        index=index,
        time=_bar_ts(index * 5),
        direction=direction,  # type: ignore
        top=top,
        bottom=bottom,
        mode="internal",
        confirmed_at=index,
        structure_leg_id=leg_id,
    )


class TestSMCSupertrendFVGMSS(unittest.TestCase):
    """Verifies all 20 required tests specified in the prompt."""

    def test_01_no_htf_bias_no_signal(self):
        """1. Không có HTF bias -> không tạo signal (WAIT_HTF_BIAS, missing_htf_bias)."""
        strat = SMCSupertrendFVGMSSStrategy()
        ctx = _make_ctx(10, bias=None)
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.WAIT_HTF_BIAS)
        self.assertEqual(strat.last_rejection_reason, "missing_htf_bias")

    def test_02_bullish_supertrend_only_bullish_fvg(self):
        """2. HTF Supertrend bullish -> chỉ nhận bullish FVG."""
        strat = SMCSupertrendFVGMSSStrategy()
        bearish_fvg = _make_poi(direction="bearish", top=2070.0, bottom=2050.0)
        ctx = _make_ctx(10, bias="bullish", active_htf_pois=(bearish_fvg,))
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.WAIT_HTF_FVG)
        self.assertEqual(strat.last_rejection_reason, "missing_htf_fvg")

    def test_03_bearish_supertrend_only_bearish_fvg(self):
        """3. HTF Supertrend bearish -> chỉ nhận bearish FVG."""
        strat = SMCSupertrendFVGMSSStrategy()
        bullish_fvg = _make_poi(direction="bullish", top=2050.0, bottom=2030.0)
        ctx = _make_ctx(10, bias="bearish", active_htf_pois=(bullish_fvg,))
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.WAIT_HTF_FVG)
        self.assertEqual(strat.last_rejection_reason, "missing_htf_fvg")

    def test_04_price_not_in_htf_fvg_no_ltf_setup(self):
        """4. Giá chưa vào HTF FVG -> không chuyển sang LTF setup (WAIT_PRICE_IN_HTF_FVG)."""
        strat = SMCSupertrendFVGMSSStrategy()
        fvg = _make_poi(direction="bullish", top=2010.0, bottom=2000.0, touch_count=0)
        ctx = _make_ctx(10, open_p=2050.0, high_p=2055.0, low_p=2045.0, close_p=2052.0, active_htf_pois=(fvg,))
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.WAIT_PRICE_IN_HTF_FVG)
        self.assertEqual(strat.last_rejection_reason, "price_not_in_htf_fvg")

    def test_05_has_htf_fvg_no_sweep_no_entry(self):
        """5. Có HTF FVG nhưng không có sweep -> không vào (WAIT_LTF_SWEEP)."""
        strat = SMCSupertrendFVGMSSStrategy()
        fvg = _make_poi(direction="bullish", top=2050.0, bottom=2030.0, touch_count=1)
        ctx = _make_ctx(10, active_htf_pois=(fvg,), sweeps=())
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.WAIT_LTF_SWEEP)
        self.assertEqual(strat.last_rejection_reason, "sweep_not_confirmed")

    def test_06_has_sweep_no_mss_timeout(self):
        """6. Có sweep nhưng không có MSS (vượt quá 8 bars timeout) -> không vào (MSS_TIMEOUT)."""
        strat = SMCSupertrendFVGMSSStrategy(SMCSupertrendFVGMSSConfig(sweep_to_mss_max_bars=8))
        fvg = _make_poi(touch_count=1)
        sweep = _make_sweep(index=10)
        # Bar 19 is 9 bars after sweep (timeout > 8)
        ctx = _make_ctx(19, active_htf_pois=(fvg,), sweeps=(sweep,), structures=())
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.MSS_TIMEOUT)
        self.assertEqual(strat.last_rejection_reason, "mss_not_confirmed")

    def test_07_mss_opposing_htf_rejected(self):
        """7. MSS ngược hướng HTF -> loại setup (mss_opposing_bias)."""
        strat = SMCSupertrendFVGMSSStrategy()
        fvg = _make_poi(direction="bullish", touch_count=1)
        sweep = _make_sweep(index=10, direction="bullish")
        mss_bear = _make_mss(index=12, direction="bearish")
        ctx = _make_ctx(13, bias="bullish", active_htf_pois=(fvg,), sweeps=(sweep,), structures=(mss_bear,))
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.WAIT_LTF_MSS)
        self.assertEqual(strat.last_rejection_reason, "mss_opposing_bias")

    def test_08_mss_wick_only_rejected(self):
        """8. MSS không có close break (chỉ có wick break) -> loại setup (mss_wick_only_rejected)."""
        strat = SMCSupertrendFVGMSSStrategy()
        fvg = _make_poi(direction="bullish", touch_count=1)
        sweep = _make_sweep(index=10, direction="bullish")
        mss_wick = _make_mss(index=12, direction="bullish", break_type="wick")
        ctx = _make_ctx(13, bias="bullish", active_htf_pois=(fvg,), sweeps=(sweep,), structures=(mss_wick,))
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.WAIT_LTF_MSS)
        self.assertEqual(strat.last_rejection_reason, "mss_wick_only_rejected")

    def test_09_mss_no_displacement_rejected(self):
        """9. MSS không có displacement -> loại setup (missing_displacement)."""
        strat = SMCSupertrendFVGMSSStrategy(SMCSupertrendFVGMSSConfig(require_displacement=True))
        fvg = _make_poi(direction="bullish", touch_count=1)
        sweep = _make_sweep(index=10, direction="bullish")
        mss_no_disp = _make_mss(index=12, direction="bullish", displacement=False)
        ctx = _make_ctx(13, bias="bullish", active_htf_pois=(fvg,), sweeps=(sweep,), structures=(mss_no_disp,))
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.WAIT_LTF_MSS)
        self.assertEqual(strat.last_rejection_reason, "missing_displacement")

    def test_10_has_mss_no_ltf_fvg_no_entry(self):
        """10. Có MSS nhưng không có LTF FVG hình thành sau MSS -> không vào (WAIT_LTF_FVG)."""
        strat = SMCSupertrendFVGMSSStrategy()
        fvg = _make_poi(touch_count=1)
        sweep = _make_sweep(index=10)
        mss = _make_mss(index=12)
        ctx = _make_ctx(13, active_htf_pois=(fvg,), sweeps=(sweep,), structures=(mss,), fvgs=())
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.WAIT_LTF_FVG)
        self.assertEqual(strat.last_rejection_reason, "ltf_fvg_missing")

    def test_11_has_ltf_fvg_no_retest_no_entry(self):
        """11. Có LTF FVG nhưng chưa retest 50% CE -> chưa vào (WAIT_LTF_RETEST)."""
        strat = SMCSupertrendFVGMSSStrategy()
        fvg_htf = _make_poi(touch_count=1)
        sweep = _make_sweep(index=10)
        mss = _make_mss(index=12)
        # FVG is [2036.0, 2040.0], CE is 2038.0. Price is at [2042.0, 2045.0], never touches CE 2038.0
        ltf_fvg = _make_fvg(index=13, top=2040.0, bottom=2036.0)
        ctx = _make_ctx(
            14,
            open_p=2043.0, high_p=2045.0, low_p=2042.0, close_p=2044.0,
            active_htf_pois=(fvg_htf,), sweeps=(sweep,), structures=(mss,), fvgs=(ltf_fvg,)
        )
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.WAIT_LTF_RETEST)
        self.assertEqual(strat.last_rejection_reason, "waiting_ltf_retest")

    def test_12_retest_50_pct_ce_creates_candidate(self):
        """12. Retest đúng 50% FVG -> tạo candidate chính xác."""
        strat = SMCSupertrendFVGMSSStrategy()
        fvg_htf = _make_poi(touch_count=1)
        sweep = _make_sweep(index=10, price_wick=2028.0)
        mss = _make_mss(index=12)
        # FVG is [2036.0, 2040.0], CE is 2038.0.
        ltf_fvg = _make_fvg(index=13, top=2040.0, bottom=2036.0)
        # Current bar dips to low 2037.5 (below CE 2038.0) and closes at 2039.0 (above bottom 2036.0)
        ctx = _make_ctx(
            14,
            open_p=2041.0, high_p=2042.0, low_p=2037.5, close_p=2039.0,
            active_htf_pois=(fvg_htf,), sweeps=(sweep,), structures=(mss,), fvgs=(ltf_fvg,)
        )
        res = strat.evaluate(ctx)
        self.assertEqual(len(res), 1)
        cand = res[0]
        self.assertEqual(cand.direction, "BUY")
        self.assertEqual(cand.entry_price, 2038.0)
        self.assertEqual(cand.strategy_id, "smc_st_fvg_mss")
        self.assertEqual(cand.planned_rr, 3.0)

    def test_13_sl_strictly_outside_sweep(self):
        """13. SL nằm ngoài vùng sweep (sweep_low - buffer cho Buy, sweep_high + buffer cho Sell)."""
        strat = SMCSupertrendFVGMSSStrategy(SMCSupertrendFVGMSSConfig(sl_buffer_atr=0.2))
        fvg_htf = _make_poi(touch_count=1)
        sweep = _make_sweep(index=10, price_wick=2028.0)  # Sweep low is 2028.0
        mss = _make_mss(index=12)
        ltf_fvg = _make_fvg(index=13, top=2040.0, bottom=2036.0)
        ctx = _make_ctx(
            14,
            open_p=2041.0, high_p=2042.0, low_p=2037.5, close_p=2039.0,
            active_htf_pois=(fvg_htf,), sweeps=(sweep,), structures=(mss,), fvgs=(ltf_fvg,)
        )
        res = strat.evaluate(ctx)
        self.assertEqual(len(res), 1)
        cand = res[0]
        # atr14 is 2.0 -> buffer = 0.2 * 2.0 = 0.4. SL = 2028.0 - 0.4 = 2027.6
        self.assertLess(cand.stop_loss, 2028.0)
        self.assertEqual(cand.stop_loss, 2027.6)
        self.assertLess(cand.stop_loss, ltf_fvg.bottom)

    def test_14_tp_strictly_3r(self):
        """14. TP chính xác bằng 3R (risk = |entry - sl|, tp = entry + 3 * risk)."""
        strat = SMCSupertrendFVGMSSStrategy(SMCSupertrendFVGMSSConfig(fixed_rr=3.0))
        fvg_htf = _make_poi(touch_count=1)
        sweep = _make_sweep(index=10, price_wick=2028.0)
        mss = _make_mss(index=12)
        ltf_fvg = _make_fvg(index=13, top=2040.0, bottom=2036.0)
        ctx = _make_ctx(
            14,
            open_p=2041.0, high_p=2042.0, low_p=2037.5, close_p=2039.0,
            active_htf_pois=(fvg_htf,), sweeps=(sweep,), structures=(mss,), fvgs=(ltf_fvg,)
        )
        res = strat.evaluate(ctx)
        self.assertEqual(len(res), 1)
        cand = res[0]
        risk = abs(cand.entry_price - cand.stop_loss)
        expected_tp = round(cand.entry_price + 3.0 * risk, 5)
        self.assertAlmostEqual(cand.take_profit, expected_tp, places=4)

    def test_15_fvg_invalidated_before_retest(self):
        """15. FVG bị invalid trước khi vào (nến đóng cửa xuyên qua đáy FVG) -> hủy setup."""
        strat = SMCSupertrendFVGMSSStrategy()
        fvg_htf = _make_poi(touch_count=1)
        sweep = _make_sweep(index=10)
        mss = _make_mss(index=12)
        ltf_fvg = _make_fvg(index=13, top=2040.0, bottom=2036.0)
        # Price closes at 2034.0 (< bottom 2036.0) -> close through invalidation
        ctx = _make_ctx(
            14,
            open_p=2037.0, high_p=2038.0, low_p=2033.0, close_p=2034.0,
            active_htf_pois=(fvg_htf,), sweeps=(sweep,), structures=(mss,), fvgs=(ltf_fvg,)
        )
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.LTF_FVG_INVALIDATED)
        self.assertEqual(strat.last_rejection_reason, "ltf_fvg_invalidated")

    def test_16_htf_supertrend_flipped_cancels_setup(self):
        """16. HTF Supertrend đổi hướng -> hủy setup ngay lập tức (HTF_BIAS_FLIPPED)."""
        strat = SMCSupertrendFVGMSSStrategy()
        fvg_htf = _make_poi(direction="bullish", touch_count=1)
        sweep = _make_sweep(index=10)
        # Bar 11: bullish setup in progress
        ctx11 = _make_ctx(11, bias="bullish", active_htf_pois=(fvg_htf,), sweeps=(sweep,))
        strat.evaluate(ctx11)
        self.assertEqual(len(strat._active_narratives), 1)

        # Bar 12: Supertrend flips to bearish!
        fvg_bear = _make_poi(poi_id="bear_1", direction="bearish", touch_count=0)
        ctx12 = _make_ctx(12, bias="bearish", active_htf_pois=(fvg_bear,))
        strat.evaluate(ctx12)
        # Previous bullish narrative cancelled
        self.assertNotIn("BUY_htf_fvg_bull_1", strat._active_narratives)

    def test_17_setup_expired_timeout(self):
        """17. Setup hết hạn (quá 15 bars sau khi có FVG mà chưa retest) -> hủy setup."""
        strat = SMCSupertrendFVGMSSStrategy(SMCSupertrendFVGMSSConfig(mss_to_entry_max_bars=15))
        fvg_htf = _make_poi(touch_count=1)
        sweep = _make_sweep(index=10)
        mss = _make_mss(index=12)
        ltf_fvg = _make_fvg(index=13, top=2040.0, bottom=2036.0)
        # Bar 29 is 16 bars after FVG confirmation (index 13) -> timeout > 15
        ctx = _make_ctx(
            29,
            open_p=2045.0, high_p=2048.0, low_p=2044.0, close_p=2046.0,
            active_htf_pois=(fvg_htf,), sweeps=(sweep,), structures=(mss,), fvgs=(ltf_fvg,)
        )
        res = strat.evaluate(ctx)
        self.assertEqual(res, ())
        self.assertEqual(strat.current_state, SMCSupertrendMacroState.SETUP_EXPIRED)
        self.assertEqual(strat.last_rejection_reason, "setup_expired")

    def test_18_no_lookahead_prefix_invariance(self):
        """18. Không look-ahead: chuỗi đánh giá nến không bị thay đổi bởi nến tương lai."""
        strat1 = SMCSupertrendFVGMSSStrategy()
        strat2 = SMCSupertrendFVGMSSStrategy()

        fvg_htf = _make_poi(touch_count=1)
        sweep = _make_sweep(index=10, price_wick=2028.0)
        mss = _make_mss(index=12)
        ltf_fvg = _make_fvg(index=13, top=2040.0, bottom=2036.0)

        # Context at bar 14 triggers entry
        ctx14 = _make_ctx(
            14,
            open_p=2041.0, high_p=2042.0, low_p=2037.5, close_p=2039.0,
            active_htf_pois=(fvg_htf,), sweeps=(sweep,), structures=(mss,), fvgs=(ltf_fvg,)
        )

        res1 = strat1.evaluate(ctx14)
        self.assertEqual(len(res1), 1)

        # Now evaluate strat2 through sequence up to 14
        ctx12 = _make_ctx(12, active_htf_pois=(fvg_htf,), sweeps=(sweep,))
        ctx13 = _make_ctx(13, active_htf_pois=(fvg_htf,), sweeps=(sweep,), structures=(mss,))
        strat2.evaluate(ctx12)
        strat2.evaluate(ctx13)
        res2 = strat2.evaluate(ctx14)

        self.assertEqual(len(res2), 1)
        self.assertEqual(res1[0].setup_id, res2[0].setup_id)
        self.assertEqual(res1[0].entry_price, res2[0].entry_price)
        self.assertEqual(res1[0].stop_loss, res2[0].stop_loss)
        self.assertEqual(res1[0].take_profit, res2[0].take_profit)

    def test_19_replay_stepped_evaluation(self):
        """19. Replay từng nến không tạo tín hiệu sớm (chỉ xuất hiện tại nến retest)."""
        strat = SMCSupertrendFVGMSSStrategy()
        fvg_htf = _make_poi(touch_count=1)
        sweep = _make_sweep(index=10, price_wick=2028.0)
        mss = _make_mss(index=12)
        ltf_fvg = _make_fvg(index=13, top=2040.0, bottom=2036.0)

        # Bar 11: only sweep -> no signal
        c11 = _make_ctx(11, active_htf_pois=(fvg_htf,), sweeps=(sweep,))
        self.assertEqual(strat.evaluate(c11), ())

        # Bar 12: MSS appears -> no signal
        c12 = _make_ctx(12, active_htf_pois=(fvg_htf,), sweeps=(sweep,), structures=(mss,))
        self.assertEqual(strat.evaluate(c12), ())

        # Bar 13: LTF FVG confirmed, price at 2042 (above CE 2038) -> no signal
        c13 = _make_ctx(
            13,
            open_p=2045.0, high_p=2046.0, low_p=2041.0, close_p=2043.0,
            active_htf_pois=(fvg_htf,), sweeps=(sweep,), structures=(mss,), fvgs=(ltf_fvg,)
        )
        self.assertEqual(strat.evaluate(c13), ())

        # Bar 14: Price retests CE (low 2037.5 <= 2038.0) -> signal generated!
        c14 = _make_ctx(
            14,
            open_p=2041.0, high_p=2042.0, low_p=2037.5, close_p=2039.0,
            active_htf_pois=(fvg_htf,), sweeps=(sweep,), structures=(mss,), fvgs=(ltf_fvg,)
        )
        res14 = strat.evaluate(c14)
        self.assertEqual(len(res14), 1)

    def test_20_legacy_strategies_regression(self):
        """20. Các chiến lược cũ (S01, S05, S09) vẫn được đảm bảo tính tương thích và độc lập."""
        from smc.engine.strategies import S01ICT2022Strategy, S05BOSOBRetestStrategy, S09ICTSilverBulletStrategy
        s1 = S01ICT2022Strategy()
        s5 = S05BOSOBRetestStrategy()
        s9 = S09ICTSilverBulletStrategy()

        self.assertEqual(s1.strategy_id, "S01")
        self.assertEqual(s5.strategy_id, "S05")
        self.assertEqual(s9.strategy_id, "S09")


if __name__ == "__main__":
    unittest.main()
