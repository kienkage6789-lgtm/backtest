"""
smc/engine/strategies/smc_supertrend_fvg_mss.py
================================================
SMC Strategy: HTF Supertrend -> HTF FVG -> LTF Liquidity Sweep -> LTF MSS -> LTF FVG Entry -> TP 3R.

Deterministic, multi-timeframe state machine adhering strictly to the StrategyTemplate protocol.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Set, Tuple, Union

import numpy as np
import pandas as pd

from smc.engine.errors import StrategyValidationError
from smc.engine.models import (
    CandidateSetup,
    EvidenceRef,
    FairValueGapSnapshot,
    HTFPOISnapshot,
    LiquidityPoolSnapshot,
    LiquiditySweepSnapshot,
    StrategyContext,
    StrategyProfile,
    StructureEventSnapshot,
    make_cluster_id,
    make_evidence_id,
    make_setup_id,
)
from smc.engine.protocol import validate_strategy_id
from smc.context.htf_supertrend_fvg import HTFFairValueGap, HTFSupertrendFVGTracker
from smc.indicators.supertrend import SupertrendState


class SMCSupertrendMacroState(str, Enum):
    WAIT_HTF_BIAS = "WAIT_HTF_BIAS"
    WAIT_HTF_FVG = "WAIT_HTF_FVG"
    WAIT_PRICE_IN_HTF_FVG = "WAIT_PRICE_IN_HTF_FVG"
    WAIT_LTF_SWEEP = "WAIT_LTF_SWEEP"
    WAIT_LTF_MSS = "WAIT_LTF_MSS"
    WAIT_LTF_FVG = "WAIT_LTF_FVG"
    WAIT_LTF_RETEST = "WAIT_LTF_RETEST"
    PLACE_ORDER = "PLACE_ORDER"
    POSITION_OPEN = "POSITION_OPEN"
    TARGET_3R = "TARGET_3R"
    STOPPED = "STOPPED"
    EXPIRED = "EXPIRED"

    # Invalidation / Rejection States
    HTF_FVG_INVALIDATED = "HTF_FVG_INVALIDATED"
    SWEEP_TIMEOUT = "SWEEP_TIMEOUT"
    MSS_TIMEOUT = "MSS_TIMEOUT"
    LTF_FVG_INVALIDATED = "LTF_FVG_INVALIDATED"
    HTF_BIAS_FLIPPED = "HTF_BIAS_FLIPPED"
    SETUP_EXPIRED = "SETUP_EXPIRED"
    SL_INVALID = "SL_INVALID"


@dataclass(frozen=True)
class SMCSupertrendFVGMSSConfig:
    """
    Immutable configuration for SMC Supertrend + FVG + LTF MSS Strategy.
    """
    htf_timeframe: str = "H1"
    ltf_timeframe: str = "M5"
    supertrend_atr_length: int = 10
    supertrend_multiplier: float = 3.0
    supertrend_min_bars: int = 2
    htf_fvg_expiry_bars: int = 30
    sweep_to_mss_max_bars: int = 8
    mss_to_entry_max_bars: int = 15
    entry_level: str = "ce_50"  # 'ce_50' or 'proximal'
    sl_buffer_atr: float = 0.2
    min_sl_atr: float = 0.5
    max_sl_atr: float = 10.0
    fixed_rr: float = 3.0
    risk_per_trade_pct: float = 0.5
    max_trades_per_htf_setup: int = 1
    # Displacement is retained as an optional research filter, but is not a
    # mandatory condition for the baseline strategy.
    require_displacement: bool = False
    mode: str = "internal"

    def __post_init__(self) -> None:
        if self.supertrend_atr_length <= 0:
            raise StrategyValidationError("supertrend_atr_length must be > 0.")
        if self.supertrend_multiplier <= 0.0:
            raise StrategyValidationError("supertrend_multiplier must be > 0.0.")
        if self.supertrend_min_bars < 1:
            raise StrategyValidationError("supertrend_min_bars must be >= 1.")
        if self.htf_fvg_expiry_bars <= 0:
            raise StrategyValidationError("htf_fvg_expiry_bars must be > 0.")
        if self.sweep_to_mss_max_bars <= 0:
            raise StrategyValidationError("sweep_to_mss_max_bars must be > 0.")
        if self.mss_to_entry_max_bars <= 0:
            raise StrategyValidationError("mss_to_entry_max_bars must be > 0.")
        if self.entry_level not in ("ce_50", "proximal"):
            raise StrategyValidationError("entry_level must be 'ce_50' or 'proximal'.")
        if self.sl_buffer_atr < 0.0:
            raise StrategyValidationError("sl_buffer_atr must be >= 0.0.")
        if self.min_sl_atr <= 0.0:
            raise StrategyValidationError("min_sl_atr must be > 0.0.")
        if self.max_sl_atr < self.min_sl_atr:
            raise StrategyValidationError("max_sl_atr must be >= min_sl_atr.")
        if self.fixed_rr <= 0.0:
            raise StrategyValidationError("fixed_rr must be > 0.0.")
        if self.mode not in ("internal", "swing"):
            raise StrategyValidationError("mode must be 'internal' or 'swing'.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "htf_timeframe": str(self.htf_timeframe),
            "ltf_timeframe": str(self.ltf_timeframe),
            "supertrend_atr_length": int(self.supertrend_atr_length),
            "supertrend_multiplier": float(self.supertrend_multiplier),
            "supertrend_min_bars": int(self.supertrend_min_bars),
            "htf_fvg_expiry_bars": int(self.htf_fvg_expiry_bars),
            "sweep_to_mss_max_bars": int(self.sweep_to_mss_max_bars),
            "mss_to_entry_max_bars": int(self.mss_to_entry_max_bars),
            "entry_level": str(self.entry_level),
            "sl_buffer_atr": float(self.sl_buffer_atr),
            "min_sl_atr": float(self.min_sl_atr),
            "max_sl_atr": float(self.max_sl_atr),
            "fixed_rr": float(self.fixed_rr),
            "risk_per_trade_pct": float(self.risk_per_trade_pct),
            "max_trades_per_htf_setup": int(self.max_trades_per_htf_setup),
            "require_displacement": bool(self.require_displacement),
            "mode": str(self.mode),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SMCSupertrendFVGMSSConfig:
        if not isinstance(data, dict):
            raise StrategyValidationError(f"Expected dict, got {type(data).__name__}.")
        return cls(**{k: v for k, v in data.items() if hasattr(cls, k)})


@dataclass
class SetupNarrative:
    """Internal tracker for a candidate setup flow across stages."""
    setup_key: str
    direction: Literal["BUY", "SELL"]
    htf_fvg_id: str
    htf_fvg_top: float
    htf_fvg_bottom: float
    htf_fvg_ce: float
    htf_touch_bar: int
    macro_state: SMCSupertrendMacroState = SMCSupertrendMacroState.WAIT_LTF_SWEEP
    sweep_event: Optional[Any] = None
    sweep_bar: Optional[int] = None
    sweep_extreme: Optional[float] = None
    mss_event: Optional[Any] = None
    mss_bar: Optional[int] = None
    ltf_fvg: Optional[Any] = None
    ltf_fvg_bar: Optional[int] = None
    retest_bar: Optional[int] = None
    terminal_reason: Optional[str] = None


class SMCSupertrendFVGMSSStrategy:
    """
    SMC Multi-timeframe Strategy:
    HTF Supertrend -> HTF FVG -> LTF Liquidity Sweep -> LTF MSS -> LTF FVG Entry -> TP 3R
    """

    def __init__(
        self,
        config: Optional[SMCSupertrendFVGMSSConfig] = None,
        strategy_id: str = "smc_st_fvg_mss",
        htf_candles: Optional[Union[pd.DataFrame, Sequence[dict[str, Any]]]] = None,
    ) -> None:
        self.strategy_id: str = validate_strategy_id(strategy_id)
        self.config: SMCSupertrendFVGMSSConfig = config or SMCSupertrendFVGMSSConfig()

        self.profile: StrategyProfile = StrategyProfile(
            strategy_id=self.strategy_id,
            name="SMC — HTF Supertrend + FVG + LTF MSS",
            version="1.0.0",
            style="continuation",
            allowed_directions=("BUY", "SELL"),
            timeframes=("M1", "M5", "M15"),
            max_setup_age_bars=self.config.mss_to_entry_max_bars,
            cooldown_bars=3,
            min_rr=self.config.fixed_rr,
            params=self.config.to_dict(),
        )

        # HTF Tracker
        self.htf_tracker = HTFSupertrendFVGTracker(
            htf_candles=htf_candles,
            timeframe=self.config.htf_timeframe,
            supertrend_atr_length=self.config.supertrend_atr_length,
            supertrend_multiplier=self.config.supertrend_multiplier,
            supertrend_min_bars=self.config.supertrend_min_bars,
            fvg_expiry_bars=self.config.htf_fvg_expiry_bars,
        )

        # Runtime State
        self._current_macro_state: SMCSupertrendMacroState = SMCSupertrendMacroState.WAIT_HTF_BIAS
        self._last_rejection_reason: str = "missing_htf_bias"
        self._active_narratives: dict[str, SetupNarrative] = {}
        self._triggered_htf_fvg_ids: set[str] = set()
        self._emitted_setups: list[CandidateSetup] = []

    @property
    def current_state(self) -> SMCSupertrendMacroState:
        return self._current_macro_state

    @property
    def last_rejection_reason(self) -> str:
        return self._last_rejection_reason

    def reset(self) -> None:
        """Reset all runtime state for clean replay."""
        self.htf_tracker.reset()
        self._current_macro_state = SMCSupertrendMacroState.WAIT_HTF_BIAS
        self._last_rejection_reason = "missing_htf_bias"
        self._active_narratives.clear()
        self._triggered_htf_fvg_ids.clear()
        self._emitted_setups.clear()

    def set_htf_candles(self, candles: Union[pd.DataFrame, Sequence[dict[str, Any]]]) -> None:
        """Register or update HTF candle feed."""
        self.htf_tracker.set_htf_candles(candles)

    def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
        """
        Evaluate market conditions as-of closed LTF bar N.
        Zero lookahead: strictly closed bars.
        """
        N = context.bar_index
        c_low = float(context.low)
        c_high = float(context.high)
        c_close = float(context.close)
        c_open = float(context.open)
        c_time = context.bar_close_time

        # Update HTF tracker interaction with current LTF candle
        self.htf_tracker.check_ltf_candle_interaction({
            "low": c_low,
            "high": c_high,
            "close": c_close,
            "time": context.timestamp,
            "bar_close_time": c_time,
        })

        # --- TẦNG 1: HTF Supertrend Bias ---
        htf_bias, st_state, htf_fvgs = self.htf_tracker.update_as_of(c_time)

        # Fallback to context.htf_bias if htf_tracker has no candles
        if htf_bias == "neutral" and context.htf_bias is not None:
            raw_b = str(getattr(context.htf_bias, "bias", "neutral")).lower()
            if raw_b in ("bullish", "bearish"):
                htf_bias = raw_b

        # If context.meta has explicit htf_supertrend
        if "htf_supertrend" in context.meta:
            meta_st = context.meta["htf_supertrend"]
            if isinstance(meta_st, dict) and "bias" in meta_st:
                htf_bias = meta_st["bias"]

        if htf_bias not in ("bullish", "bearish"):
            self._current_macro_state = SMCSupertrendMacroState.WAIT_HTF_BIAS
            self._last_rejection_reason = "missing_htf_bias"
            self._cleanup_narratives_on_bias_flip(None)
            return ()

        expected_dir: Literal["BUY", "SELL"] = "BUY" if htf_bias == "bullish" else "SELL"
        expected_fvg_dir: Literal["bullish", "bearish"] = "bullish" if htf_bias == "bullish" else "bearish"

        # Cleanup narratives if bias flipped
        self._cleanup_narratives_on_bias_flip(expected_dir)

        # --- TẦNG 2: HTF FVG Discovery ---
        best_htf_fvg = self.htf_tracker.select_best_fvg(current_price=c_close, bias=expected_fvg_dir)

        # Fallback: check context.active_htf_pois for FVG if htf_tracker returned none
        if best_htf_fvg is None and hasattr(context, "active_htf_pois"):
            for poi in context.active_htf_pois:
                p_type = getattr(poi, "poi_type", "").upper()
                p_dir = getattr(poi, "direction", "").lower()
                p_status = getattr(poi, "status", "active")
                if p_type == "FVG" and p_dir == expected_fvg_dir and p_status != "invalidated":
                    top = float(poi.top)
                    bottom = float(poi.bottom)
                    best_htf_fvg = HTFFairValueGap(
                        fvg_id=str(poi.poi_id),
                        direction=expected_fvg_dir,
                        top=top,
                        bottom=bottom,
                        ce=(top + bottom) / 2.0,
                        gap_size=top - bottom,
                        timeframe=getattr(poi, "timeframe", self.config.htf_timeframe),
                        confirmed_bar=0,
                        confirmed_time=context.timestamp,
                        displacement=True,
                        touch_count=int(getattr(poi, "touch_count", 0)),
                    )
                    break

        if best_htf_fvg is None:
            self._current_macro_state = SMCSupertrendMacroState.WAIT_HTF_FVG
            self._last_rejection_reason = "missing_htf_fvg"
            return ()

        # --- TẦNG 3: Giá hồi về HTF FVG (Touch / Retest) ---
        # Check distal invalidation
        if (expected_dir == "BUY" and c_close < best_htf_fvg.bottom) or (expected_dir == "SELL" and c_close > best_htf_fvg.top):
            best_htf_fvg.status = "invalidated"
            self._current_macro_state = SMCSupertrendMacroState.HTF_FVG_INVALIDATED
            self._last_rejection_reason = "htf_fvg_invalidated"
            return ()

        overlaps_now = (c_low <= best_htf_fvg.top and c_high >= best_htf_fvg.bottom)
        has_touched = (best_htf_fvg.touch_count > 0 or overlaps_now)

        if not has_touched:
            self._current_macro_state = SMCSupertrendMacroState.WAIT_PRICE_IN_HTF_FVG
            self._last_rejection_reason = "price_not_in_htf_fvg"
            return ()

        # Ensure active narrative for this HTF FVG
        narrative_key = f"{expected_dir}_{best_htf_fvg.fvg_id}"
        if narrative_key not in self._active_narratives:
            self._active_narratives[narrative_key] = SetupNarrative(
                setup_key=narrative_key,
                direction=expected_dir,
                htf_fvg_id=best_htf_fvg.fvg_id,
                htf_fvg_top=best_htf_fvg.top,
                htf_fvg_bottom=best_htf_fvg.bottom,
                htf_fvg_ce=best_htf_fvg.ce,
                htf_touch_bar=N,
                macro_state=SMCSupertrendMacroState.WAIT_LTF_SWEEP,
            )

        narrative = self._active_narratives[narrative_key]

        # --- TẦNG 4: LTF Liquidity Sweep ---
        if narrative.sweep_event is None:
            valid_sweep, sweep_extreme = self._find_valid_sweep(context, expected_dir, best_htf_fvg)
            if valid_sweep is not None:
                narrative.sweep_event = valid_sweep
                narrative.sweep_bar = int(valid_sweep.index)
                narrative.sweep_extreme = float(sweep_extreme)
                narrative.macro_state = SMCSupertrendMacroState.WAIT_LTF_MSS
            else:
                self._current_macro_state = SMCSupertrendMacroState.WAIT_LTF_SWEEP
                self._last_rejection_reason = "sweep_not_confirmed"
                return ()

        # --- TẦNG 5: LTF MSS Confirmation ---
        if narrative.mss_event is None:
            valid_mss, rejection = self._find_valid_mss(context, expected_dir, narrative.sweep_bar)
            if valid_mss is not None:
                # Check if MSS occurred within sweep_to_mss_max_bars
                if int(valid_mss.index) - narrative.sweep_bar > self.config.sweep_to_mss_max_bars:
                    narrative.macro_state = SMCSupertrendMacroState.MSS_TIMEOUT
                    narrative.terminal_reason = "mss_not_confirmed"
                    self._current_macro_state = SMCSupertrendMacroState.MSS_TIMEOUT
                    self._last_rejection_reason = "mss_not_confirmed"
                    self._active_narratives.pop(narrative_key, None)
                    return ()

                narrative.mss_event = valid_mss
                narrative.mss_bar = int(valid_mss.index)
                narrative.macro_state = SMCSupertrendMacroState.WAIT_LTF_FVG
            else:
                bars_since_sweep = N - narrative.sweep_bar
                if bars_since_sweep > self.config.sweep_to_mss_max_bars:
                    narrative.macro_state = SMCSupertrendMacroState.MSS_TIMEOUT
                    narrative.terminal_reason = "mss_not_confirmed"
                    self._current_macro_state = SMCSupertrendMacroState.MSS_TIMEOUT
                    self._last_rejection_reason = "mss_not_confirmed"
                    self._active_narratives.pop(narrative_key, None)
                    return ()

                self._current_macro_state = SMCSupertrendMacroState.WAIT_LTF_MSS
                self._last_rejection_reason = rejection or "mss_not_confirmed"
                return ()

        # --- TẦNG 6: LTF FVG Detection after MSS ---
        if narrative.ltf_fvg is None:
            valid_ltf_fvg = self._find_valid_ltf_fvg(context, expected_dir, narrative.mss_bar)
            if valid_ltf_fvg is not None:
                narrative.ltf_fvg = valid_ltf_fvg
                narrative.ltf_fvg_bar = int(valid_ltf_fvg.confirmed_at)
                narrative.macro_state = SMCSupertrendMacroState.WAIT_LTF_RETEST
            else:
                self._current_macro_state = SMCSupertrendMacroState.WAIT_LTF_FVG
                self._last_rejection_reason = "ltf_fvg_missing"
                return ()

        ltf_fvg = narrative.ltf_fvg

        # Check LTF FVG Invalidation (closed through distal)
        if (expected_dir == "BUY" and c_close < ltf_fvg.bottom) or (expected_dir == "SELL" and c_close > ltf_fvg.top):
            narrative.macro_state = SMCSupertrendMacroState.LTF_FVG_INVALIDATED
            narrative.terminal_reason = "ltf_fvg_invalidated"
            self._current_macro_state = SMCSupertrendMacroState.LTF_FVG_INVALIDATED
            self._last_rejection_reason = "ltf_fvg_invalidated"
            self._active_narratives.pop(narrative_key, None)
            return ()

        # Check Retest Timeout (15 bars from FVG confirmation)
        bars_since_fvg = N - narrative.ltf_fvg_bar
        if bars_since_fvg > self.config.mss_to_entry_max_bars:
            narrative.macro_state = SMCSupertrendMacroState.SETUP_EXPIRED
            narrative.terminal_reason = "setup_expired"
            self._current_macro_state = SMCSupertrendMacroState.SETUP_EXPIRED
            self._last_rejection_reason = "setup_expired"
            self._active_narratives.pop(narrative_key, None)
            return ()

        # --- TẦNG 7: Retest 50% FVG LTF ---
        entry_price = float(ltf_fvg.ce) if self.config.entry_level == "ce_50" else (float(ltf_fvg.top) if expected_dir == "BUY" else float(ltf_fvg.bottom))
        is_retested = False
        if expected_dir == "BUY":
            is_retested = (c_low <= entry_price and c_close >= ltf_fvg.bottom)
        else:
            is_retested = (c_high >= entry_price and c_close <= ltf_fvg.top)

        if not is_retested:
            self._current_macro_state = SMCSupertrendMacroState.WAIT_LTF_RETEST
            self._last_rejection_reason = "waiting_ltf_retest"
            return ()

        # --- TẦNG 8: SL và TP Calculation & Verification ---
        atr_val = float(context.atr14) if context.atr14 and context.atr14 > 0 else max(1.0, abs(c_high - c_low))
        sl_buffer = self.config.sl_buffer_atr * atr_val

        if expected_dir == "BUY":
            stop_loss = narrative.sweep_extreme - sl_buffer
            # SL must be strictly below sweep low
            if stop_loss >= narrative.sweep_extreme:
                stop_loss = narrative.sweep_extreme - 0.01

            # SL must be outside LTF FVG
            if stop_loss >= ltf_fvg.bottom:
                self._current_macro_state = SMCSupertrendMacroState.SL_INVALID
                self._last_rejection_reason = "sl_in_fvg"
                return ()

            sl_dist = entry_price - stop_loss
            if sl_dist < self.config.min_sl_atr * atr_val or sl_dist > self.config.max_sl_atr * atr_val:
                self._current_macro_state = SMCSupertrendMacroState.SL_INVALID
                self._last_rejection_reason = "sl_too_large" if sl_dist > self.config.max_sl_atr * atr_val else "sl_too_small"
                return ()

            risk = abs(entry_price - stop_loss)
            take_profit = entry_price + self.config.fixed_rr * risk

        else:  # SELL
            stop_loss = narrative.sweep_extreme + sl_buffer
            # SL must be strictly above sweep high
            if stop_loss <= narrative.sweep_extreme:
                stop_loss = narrative.sweep_extreme + 0.01

            # SL must be outside LTF FVG
            if stop_loss <= ltf_fvg.top:
                self._current_macro_state = SMCSupertrendMacroState.SL_INVALID
                self._last_rejection_reason = "sl_in_fvg"
                return ()

            sl_dist = stop_loss - entry_price
            if sl_dist < self.config.min_sl_atr * atr_val or sl_dist > self.config.max_sl_atr * atr_val:
                self._current_macro_state = SMCSupertrendMacroState.SL_INVALID
                self._last_rejection_reason = "sl_too_large" if sl_dist > self.config.max_sl_atr * atr_val else "sl_too_small"
                return ()

            risk = abs(entry_price - stop_loss)
            take_profit = entry_price - self.config.fixed_rr * risk

        # Check maximum trades per HTF setup
        if narrative.htf_fvg_id in self._triggered_htf_fvg_ids:
            self._current_macro_state = SMCSupertrendMacroState.PLACE_ORDER
            self._last_rejection_reason = "duplicate_htf_setup"
            return ()

        # --- TẦNG 9: Emit CandidateSetup ---
        self._triggered_htf_fvg_ids.add(narrative.htf_fvg_id)
        best_htf_fvg.trades_triggered += 1

        # Structure/FVG leg identifiers may use ':' as a semantic separator
        # (e.g. ``internal:bullish:1690``), while cluster-id base components
        # deliberately reject ':' as a delimiter. Normalize only the
        # identifier used for the cluster; preserve the original evidence
        # metadata unchanged.
        leg_token = str(getattr(narrative.mss_event, "structure_leg_id", None) or f"leg_{narrative.mss_bar}").replace(":", "_")
        zone_token = str(getattr(ltf_fvg, "structure_leg_id", None) or f"fvg_{ltf_fvg.confirmed_at}").replace(":", "_")
        cluster_id = make_cluster_id(expected_dir, leg_token, zone_token)
        setup_id = make_setup_id(self.strategy_id, expected_dir, N, cluster_id)

        evidences = (
            EvidenceRef(
                evidence_id=make_evidence_id("htf_fvg", self.config.mode, N, narrative.htf_fvg_id),
                kind="htf_poi",
                bar_index=narrative.htf_touch_bar,
                price=narrative.htf_fvg_ce,
                time=context.timestamp,
                details={"direction": htf_bias, "source_id": narrative.htf_fvg_id},
            ),
            EvidenceRef(
                evidence_id=make_evidence_id("sweep", self.config.mode, N, str(getattr(narrative.sweep_event, "index", 0))),
                kind="liquidity_sweep",
                bar_index=narrative.sweep_bar,
                price=narrative.sweep_extreme,
                time=context.timestamp,
                details={"direction": htf_bias},
            ),
            EvidenceRef(
                evidence_id=make_evidence_id("mss", self.config.mode, N, str(getattr(narrative.mss_event, "index", 0))),
                kind="structure_event",
                bar_index=narrative.mss_bar,
                price=float(getattr(narrative.mss_event, "close_price", entry_price)),
                time=context.timestamp,
                details={"direction": htf_bias},
            ),
            EvidenceRef(
                evidence_id=make_evidence_id("ltf_fvg", self.config.mode, N, str(getattr(ltf_fvg, "index", 0))),
                kind="fair_value_gap",
                bar_index=narrative.ltf_fvg_bar,
                price=float(ltf_fvg.ce),
                time=context.timestamp,
                details={"direction": htf_bias},
            ),
        )

        candidate = CandidateSetup(
            setup_id=setup_id,
            strategy_id=self.strategy_id,
            direction=expected_dir,
            bar_index=N,
            timestamp=context.timestamp,
            entry_price=round(entry_price, 5),
            stop_loss=round(stop_loss, 5),
            take_profit=round(take_profit, 5),
            planned_rr=self.config.fixed_rr,
            evidences=evidences,
            evidence_cluster_id=cluster_id,
            expiry_bar=N + self.config.mss_to_entry_max_bars,
            target_type="fixed_rr",
            meta={
                "strategy_name": "SMC — HTF Supertrend + FVG + LTF MSS",
                "htf_fvg_id": narrative.htf_fvg_id,
                "sweep_bar": narrative.sweep_bar,
                "mss_bar": narrative.mss_bar,
                "ltf_fvg_bar": narrative.ltf_fvg_bar,
                "retest_bar": N,
                "risk": round(risk, 5),
                "rr": self.config.fixed_rr,
            },
        )

        narrative.macro_state = SMCSupertrendMacroState.PLACE_ORDER
        self._current_macro_state = SMCSupertrendMacroState.PLACE_ORDER
        self._last_rejection_reason = "none"
        self._emitted_setups.append(candidate)
        self._active_narratives.pop(narrative_key, None)

        return (candidate,)

    def _cleanup_narratives_on_bias_flip(self, active_dir: Optional[str]) -> None:
        """Invalidate in-progress setups when bias flips."""
        to_del = []
        for k, n in self._active_narratives.items():
            if active_dir is None or n.direction != active_dir:
                n.macro_state = SMCSupertrendMacroState.HTF_BIAS_FLIPPED
                n.terminal_reason = "htf_bias_flipped"
                to_del.append(k)
        for k in to_del:
            self._active_narratives.pop(k, None)

    def _find_valid_sweep(
        self,
        context: StrategyContext,
        expected_dir: str,
        fvg: Any,
    ) -> Tuple[Optional[Any], Optional[float]]:
        """Look for liquidity sweep matching direction occurring within FVG active context."""
        target_sweep_dir = "bullish" if expected_dir == "BUY" else "bearish"

        fvg_touch_cnt = int(getattr(fvg, "touch_count", 0))
        fvg_touch_bar = getattr(fvg, "last_touch_bar", None) or 0

        candidate_sweeps = []
        for s in context.recent_sweeps:
            if getattr(s, "direction", "") != target_sweep_dir or not getattr(s, "valid", True):
                continue
            s_idx = int(getattr(s, "index", 0))
            if s_idx > context.bar_index:
                continue
            # If FVG was already touched earlier or price is currently in FVG
            if fvg_touch_cnt > 0 or s_idx >= fvg_touch_bar:
                candidate_sweeps.append(s)

        if not candidate_sweeps:
            return None, None

        candidate_sweeps.sort(key=lambda s: int(getattr(s, "index", 0)), reverse=True)
        sweep = candidate_sweeps[0]
        extreme = getattr(sweep, "price_wick", None)
        if extreme is None:
            extreme = getattr(sweep, "pool_price", None)
        if extreme is None:
            extreme = float(context.low) if expected_dir == "BUY" else float(context.high)

        return sweep, float(extreme)

    def _find_valid_mss(
        self,
        context: StrategyContext,
        expected_dir: str,
        sweep_bar: int,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """Look for MSS with close break, displacement, and alignment."""
        target_dir = "bullish" if expected_dir == "BUY" else "bearish"

        all_structures = list(context.recent_structures)
        # Filter structures occurring strictly after sweep
        candidate_events = [
            e for e in all_structures
            if getattr(e, "index", 0) > sweep_bar
            and getattr(e, "event_type", "") in ("CHoCH", "BOS", "MSS")
        ]
        if not candidate_events:
            return None, "mss_not_confirmed"

        candidate_events.sort(key=lambda e: getattr(e, "index", 0))

        for ev in candidate_events:
            ev_dir = getattr(ev, "direction", "")
            # Direction check
            if ev_dir != target_dir:
                return None, "mss_opposing_bias"

            # Close break check (not wick only)
            break_type = getattr(ev, "break_type", "close")
            if break_type != "close":
                return None, "mss_wick_only_rejected"

            # Displacement check
            if self.config.require_displacement and not getattr(ev, "displacement", False):
                return None, "missing_displacement"

            return ev, None

        return None, "mss_not_confirmed"

    def _find_valid_ltf_fvg(
        self,
        context: StrategyContext,
        expected_dir: str,
        mss_bar: int,
    ) -> Optional[Any]:
        """Look for LTF FVG formed during or after MSS displacement."""
        target_dir = "bullish" if expected_dir == "BUY" else "bearish"

        candidate_fvgs = [
            f for f in context.active_fvgs
            if getattr(f, "direction", "") == target_dir
            and getattr(f, "confirmed_at", getattr(f, "index", 0)) >= mss_bar
            and not getattr(f, "filled", False)
        ]
        if not candidate_fvgs:
            return None

        # Pick the most aligned / nearest FVG to the MSS leg
        candidate_fvgs.sort(key=lambda f: getattr(f, "confirmed_at", getattr(f, "index", 0)))
        return candidate_fvgs[0]


__all__ = [
    "SMCSupertrendMacroState",
    "SMCSupertrendFVGMSSConfig",
    "SMCSupertrendFVGMSSStrategy",
]
