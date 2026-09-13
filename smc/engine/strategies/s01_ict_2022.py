"""
smc/engine/strategies/s01_ict_2022.py
======================================
S01: ICT 2022 Model (Liquidity Sweep -> MSS/CHoCH -> FVG Retest Reversal).
Deterministic, multi-narrative state machine implementation adhering to T53.4.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from smc.engine.errors import (
    StrategyStateError,
    StrategyValidationError,
)
from smc.engine.models import (
    CandidateSetup,
    EvidenceRef,
    FairValueGapSnapshot,
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


@dataclass(frozen=True)
class S01Config:
    """
    Immutable configuration for S01 ICT 2022 Reversal Strategy.

    Attributes:
        mode: Detection mode ('internal' or 'swing'). Default 'internal'.
        sweep_to_mss_max_bars: Maximum bars between sweep and MSS confirmation. Default 20.
        fvg_to_mss_max_bars: Maximum bars between FVG candle and MSS confirmation. Default 10.
        entry_expiry_bars: Expiry window in bars after MSS/FVG ready. Default 15.
        entry_level: Entry price level ('proximal' or 'ce_50'). Default 'proximal'.
        sl_buffer_price: Extra price buffer added/subtracted beyond extreme SL. Default 0.20.
        min_rr: Minimum structural risk/reward ratio required. Default 1.50.
        fallback_rr: Fallback fixed risk/reward ratio when opposing pool is absent or RR < min_rr. Default 2.00.
        require_displacement: Whether MSS must have displacement=True. Default True.
        allow_mss_without_sweep: In experimental adaptive mode, allow an HTF-aligned
            MSS to create a setup without a preceding liquidity sweep. Default False.
    """

    mode: str = "internal"
    sweep_to_mss_max_bars: int = 20
    fvg_to_mss_max_bars: int = 10
    entry_expiry_bars: int = 15
    entry_level: str = "proximal"
    sl_buffer_price: float = 0.20
    min_rr: float = 1.50
    fallback_rr: float = 2.00
    require_displacement: bool = True
    allow_mss_without_sweep: bool = False

    def __post_init__(self) -> None:
        # 1. mode validation
        if isinstance(self.mode, (bool, np.bool_)) or not isinstance(self.mode, str):
            raise StrategyValidationError(
                f"mode must be a string, got {type(self.mode).__name__}."
            )
        if self.mode not in ("internal", "swing"):
            raise StrategyValidationError(
                f"mode must be 'internal' or 'swing', got '{self.mode}'."
            )

        # 2. sweep_to_mss_max_bars
        if isinstance(self.sweep_to_mss_max_bars, (bool, np.bool_)) or not isinstance(
            self.sweep_to_mss_max_bars, (int, np.integer)
        ):
            raise StrategyValidationError(
                f"sweep_to_mss_max_bars must be an integer, got {type(self.sweep_to_mss_max_bars).__name__}."
            )
        if int(self.sweep_to_mss_max_bars) <= 0:
            raise StrategyValidationError("sweep_to_mss_max_bars must be > 0.")
        object.__setattr__(self, "sweep_to_mss_max_bars", int(self.sweep_to_mss_max_bars))

        # 3. fvg_to_mss_max_bars
        if isinstance(self.fvg_to_mss_max_bars, (bool, np.bool_)) or not isinstance(
            self.fvg_to_mss_max_bars, (int, np.integer)
        ):
            raise StrategyValidationError(
                f"fvg_to_mss_max_bars must be an integer, got {type(self.fvg_to_mss_max_bars).__name__}."
            )
        if int(self.fvg_to_mss_max_bars) <= 0:
            raise StrategyValidationError("fvg_to_mss_max_bars must be > 0.")
        object.__setattr__(self, "fvg_to_mss_max_bars", int(self.fvg_to_mss_max_bars))

        # 4. entry_expiry_bars
        if isinstance(self.entry_expiry_bars, (bool, np.bool_)) or not isinstance(
            self.entry_expiry_bars, (int, np.integer)
        ):
            raise StrategyValidationError(
                f"entry_expiry_bars must be an integer, got {type(self.entry_expiry_bars).__name__}."
            )
        if int(self.entry_expiry_bars) <= 0:
            raise StrategyValidationError("entry_expiry_bars must be > 0.")
        object.__setattr__(self, "entry_expiry_bars", int(self.entry_expiry_bars))

        # 5. entry_level
        if isinstance(self.entry_level, (bool, np.bool_)) or not isinstance(
            self.entry_level, str
        ):
            raise StrategyValidationError(
                f"entry_level must be a string, got {type(self.entry_level).__name__}."
            )
        if self.entry_level not in ("proximal", "ce_50"):
            raise StrategyValidationError(
                f"entry_level must be 'proximal' or 'ce_50', got '{self.entry_level}'."
            )

        # 6. sl_buffer_price
        if isinstance(self.sl_buffer_price, (bool, np.bool_)) or not isinstance(
            self.sl_buffer_price, (int, float, np.integer, np.floating)
        ):
            raise StrategyValidationError(
                f"sl_buffer_price must be a float, got {type(self.sl_buffer_price).__name__}."
            )
        sl_buf = float(self.sl_buffer_price)
        if not math.isfinite(sl_buf) or sl_buf <= 0.0:
            raise StrategyValidationError("sl_buffer_price must be a finite float > 0.")
        object.__setattr__(self, "sl_buffer_price", sl_buf)

        # 7. min_rr
        if isinstance(self.min_rr, (bool, np.bool_)) or not isinstance(
            self.min_rr, (int, float, np.integer, np.floating)
        ):
            raise StrategyValidationError(
                f"min_rr must be a float, got {type(self.min_rr).__name__}."
            )
        m_rr = float(self.min_rr)
        if not math.isfinite(m_rr) or m_rr <= 0.0:
            raise StrategyValidationError("min_rr must be a finite float > 0.")
        object.__setattr__(self, "min_rr", m_rr)

        # 8. fallback_rr
        if isinstance(self.fallback_rr, (bool, np.bool_)) or not isinstance(
            self.fallback_rr, (int, float, np.integer, np.floating)
        ):
            raise StrategyValidationError(
                f"fallback_rr must be a float, got {type(self.fallback_rr).__name__}."
            )
        f_rr = float(self.fallback_rr)
        if not math.isfinite(f_rr) or f_rr < self.min_rr:
            raise StrategyValidationError(
                f"fallback_rr ({f_rr}) must be a finite float >= min_rr ({self.min_rr})."
            )
        object.__setattr__(self, "fallback_rr", f_rr)

        # 9. require_displacement
        if isinstance(self.require_displacement, (int, np.integer)) and not isinstance(
            self.require_displacement, (bool, np.bool_)
        ):
            raise StrategyValidationError(
                f"require_displacement must be a boolean, got {type(self.require_displacement).__name__}."
            )
        if not isinstance(self.require_displacement, (bool, np.bool_)):
            raise StrategyValidationError(
                f"require_displacement must be a boolean, got {type(self.require_displacement).__name__}."
            )
        object.__setattr__(self, "require_displacement", bool(self.require_displacement))

        # 10. allow_mss_without_sweep
        if not isinstance(self.allow_mss_without_sweep, (bool, np.bool_)):
            raise StrategyValidationError(
                f"allow_mss_without_sweep must be a boolean, got {type(self.allow_mss_without_sweep).__name__}."
            )
        object.__setattr__(self, "allow_mss_without_sweep", bool(self.allow_mss_without_sweep))

    def to_dict(self) -> dict[str, Any]:
        """Serialize configuration to JSON-safe dictionary."""
        return {
            "mode": str(self.mode),
            "sweep_to_mss_max_bars": int(self.sweep_to_mss_max_bars),
            "fvg_to_mss_max_bars": int(self.fvg_to_mss_max_bars),
            "entry_expiry_bars": int(self.entry_expiry_bars),
            "entry_level": str(self.entry_level),
            "sl_buffer_price": float(self.sl_buffer_price),
            "min_rr": float(self.min_rr),
            "fallback_rr": float(self.fallback_rr),
            "require_displacement": bool(self.require_displacement),
            "allow_mss_without_sweep": bool(self.allow_mss_without_sweep),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> S01Config:
        """Construct S01Config from dictionary with strict key and type validation."""
        if not isinstance(data, dict):
            raise StrategyValidationError(
                f"Expected dict for S01Config, got {type(data).__name__}."
            )
        allowed_keys = {
            "mode",
            "sweep_to_mss_max_bars",
            "fvg_to_mss_max_bars",
            "entry_expiry_bars",
            "entry_level",
            "sl_buffer_price",
            "min_rr",
            "fallback_rr",
            "require_displacement",
            "allow_mss_without_sweep",
        }
        extra_keys = set(data.keys()) - allowed_keys
        if extra_keys:
            raise StrategyValidationError(
                f"Unexpected config fields for S01Config: {sorted(extra_keys)}."
            )
        return cls(**data)


class NarrativeStage(str, Enum):
    SWEEP_SEEN = "SWEEP_SEEN"
    FVG_READY = "FVG_READY"
    EMITTED = "EMITTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class S01MacroState(str, Enum):
    WAIT_HTF_BIAS = "WAIT_HTF_BIAS"
    WAIT_HTF_POI = "WAIT_HTF_POI"
    HTF_POI_ACTIVE = "HTF_POI_ACTIVE"
    WAIT_LTF_SWEEP = "WAIT_LTF_SWEEP"
    WAIT_LTF_MSS = "WAIT_LTF_MSS"
    WAIT_LTF_ENTRY_ZONE = "WAIT_LTF_ENTRY_ZONE"
    WAIT_LTF_RETEST = "WAIT_LTF_RETEST"
    S1_READY = "S1_READY"
    EXECUTED = "EXECUTED"

    # Invalidation states
    POI_INVALIDATED = "POI_INVALIDATED"
    BIAS_CHANGED = "BIAS_CHANGED"
    SWEEP_EXPIRED = "SWEEP_EXPIRED"
    MSS_NOT_FOUND = "MSS_NOT_FOUND"
    LTF_ZONE_INVALIDATED = "LTF_ZONE_INVALIDATED"
    ENTRY_EXPIRED = "ENTRY_EXPIRED"
    RR_INVALID = "RR_INVALID"


SweepKey = tuple[str, str, int, tuple[int, ...]]
MssKey = tuple[str, str, str, int, int]
FvgKey = tuple[str, str, int]


@dataclass
class S01Narrative:
    sweep_key: SweepKey
    direction: str  # "BUY" or "SELL"
    sweep: Optional[LiquiditySweepSnapshot]
    trigger_type: str = "sweep"  # "sweep" or "mss_without_sweep"
    stage: NarrativeStage = NarrativeStage.SWEEP_SEEN
    macro_state: S01MacroState = S01MacroState.WAIT_LTF_MSS
    poi: Optional[Any] = None
    poi_touch_bar: Optional[int] = None
    mss_key: Optional[MssKey] = None
    mss: Optional[StructureEventSnapshot] = None
    fvg_key: Optional[FvgKey] = None
    fvg: Optional[FairValueGapSnapshot] = None
    ltf_ob_id: Optional[str] = None
    ready_at: Optional[int] = None
    expiry_bar: Optional[int] = None
    fvg_filled_internally: bool = False
    fvg_filled_bar: Optional[int] = None
    terminal_reason: Optional[str] = None

    def __deepcopy__(self, memo: dict[int, Any]) -> S01Narrative:
        new_obj = S01Narrative(
            sweep_key=self.sweep_key,
            direction=self.direction,
            sweep=self.sweep,
            trigger_type=self.trigger_type,
            stage=self.stage,
            macro_state=self.macro_state,
            poi=self.poi,
            poi_touch_bar=self.poi_touch_bar,
            mss_key=self.mss_key,
            mss=self.mss,
            fvg_key=self.fvg_key,
            fvg=self.fvg,
            ltf_ob_id=self.ltf_ob_id,
            ready_at=self.ready_at,
            expiry_bar=self.expiry_bar,
            fvg_filled_internally=self.fvg_filled_internally,
            fvg_filled_bar=self.fvg_filled_bar,
            terminal_reason=self.terminal_reason,
        )
        memo[id(self)] = new_obj
        return new_obj


class S01ICT2022Strategy:
    """
    S01: ICT 2022 Reversal Strategy Template.

    Implements StrategyTemplate Protocol:
        - strategy_id: str = "S01"
        - profile: StrategyProfile
        - evaluate(context: StrategyContext) -> tuple[CandidateSetup, ...]
        - reset() -> None
    """

    strategy_id: str = "S01"

    def __init__(self, config: Optional[S01Config] = None) -> None:
        if config is None:
            config = S01Config()
        elif not isinstance(config, S01Config):
            raise StrategyValidationError(
                f"Expected S01Config or None, got {type(config).__name__}."
            )

        self._config: S01Config = config
        self.profile: StrategyProfile = StrategyProfile(
            strategy_id=self.strategy_id,
            name="ICT 2022 Reversal",
            version="1.0.0",
            style="reversal",
            allowed_directions=("BUY", "SELL"),
            timeframes=("M1", "M5", "M15"),
            max_setup_age_bars=config.entry_expiry_bars,
            cooldown_bars=3,
            min_rr=config.min_rr,
            params=config.to_dict(),
        )

        # Internal state machines
        self._narratives: dict[SweepKey, S01Narrative] = {}
        self._emitted_clusters: dict[str, int] = {}
        self._macro_state: S01MacroState = S01MacroState.WAIT_HTF_BIAS
        self._last_rejection_reason: Optional[str] = None
        self._active_poi: Optional[Any] = None

        # Monotonicity & Idempotency cache
        self._last_bar_index: Optional[int] = None
        self._last_timestamp: Any = None
        self._last_context_payload: Optional[dict[str, Any]] = None
        self._last_result: Optional[tuple[CandidateSetup, ...]] = None

    @property
    def config(self) -> S01Config:
        """Return the immutable strategy configuration."""
        return self._config

    @property
    def current_state(self) -> S01MacroState:
        """Return the current S1 macro state machine state."""
        return self._macro_state

    @property
    def last_rejection_reason(self) -> Optional[str]:
        """Return the latest rejection reason."""
        return self._last_rejection_reason

    @property
    def active_poi(self) -> Optional[Any]:
        """Return the currently tracked active HTF POI, if any."""
        return self._active_poi

    def reset(self) -> None:
        """Reset internal state machine for replay or fresh run."""
        self._narratives.clear()
        self._emitted_clusters.clear()
        self._macro_state = S01MacroState.WAIT_HTF_BIAS
        self._last_rejection_reason = None
        self._active_poi = None
        self._last_bar_index = None
        self._last_timestamp = None
        self._last_context_payload = None
        self._last_result = None

    def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
        """
        Evaluate closed bar context and return generated CandidateSetup tuple.
        """
        # 1. Validation
        if not isinstance(context, StrategyContext):
            raise StrategyValidationError(
                f"Expected StrategyContext, got {type(context).__name__}."
            )
        if context.timeframe not in self.profile.timeframes:
            raise StrategyValidationError(
                f"Timeframe '{context.timeframe}' not supported by S01 profile {self.profile.timeframes}."
            )

        # 2. Monotonicity & Idempotent Retry Guard
        N = context.bar_index
        if self._last_bar_index is None:
            if N < 0:
                raise StrategyValidationError("bar_index must be >= 0.")
        else:
            if N == self._last_bar_index:
                if (
                    context.timestamp == self._last_timestamp
                    and context.to_dict() == self._last_context_payload
                ):
                    assert self._last_result is not None
                    return self._last_result
                raise StrategyStateError(
                    "Conflicting context payload for identical bar_index."
                )
            elif N < self._last_bar_index:
                raise StrategyStateError(
                    f"Context bar_index decreased from {self._last_bar_index} to {N}."
                )
            elif N > self._last_bar_index + 1:
                raise StrategyStateError(
                    f"Context bar_index must advance sequentially by 1 (last was {self._last_bar_index}, got {N})."
                )
            else:  # N == self._last_bar_index + 1
                if context.timestamp <= self._last_timestamp:
                    raise StrategyStateError(
                        f"Context timestamp ({context.timestamp}) must strictly increase on bar advance (last was {self._last_timestamp})."
                    )

        # 3. Future Leak Defense Check on Context Evidence
        self._assert_zero_future_leak(context)

        # 4. Atomic working copy preparation
        working_narratives: dict[SweepKey, S01Narrative] = {
            k: copy.copy(v) for k, v in self._narratives.items()
        }
        # Prune expired clusters (N > expiry_bar)
        working_emitted_clusters: dict[str, int] = {
            cid: exp for cid, exp in self._emitted_clusters.items() if N <= exp
        }
        candidates_for_bar: list[CandidateSetup] = []

        # 5. HTF Bias Check
        if context.htf_bias is None or context.htf_bias.bias == "neutral":
            self._macro_state = S01MacroState.WAIT_HTF_BIAS
            self._last_rejection_reason = "missing_htf_bias"
            self._active_poi = None
            for k, n in working_narratives.items():
                n.stage = NarrativeStage.INVALIDATED
                n.macro_state = S01MacroState.BIAS_CHANGED
                n.terminal_reason = "bias_changed"
            self._narratives.clear()
            self._emitted_clusters = working_emitted_clusters
            self._last_bar_index = N
            self._last_timestamp = context.timestamp
            self._last_context_payload = context.to_dict()
            self._last_result = ()
            return ()

        current_bias = context.htf_bias.bias
        expected_dir = "BUY" if current_bias == "bullish" else "SELL"
        expected_poi_dir = "bullish" if current_bias == "bullish" else "bearish"

        # 6. HTF POI Validation & Interaction Check
        all_pois = tuple(getattr(context, "active_htf_pois", ()))
        if not all_pois:
            self._macro_state = S01MacroState.WAIT_HTF_POI
            self._last_rejection_reason = "missing_htf_poi"
            self._active_poi = None
            for k, n in list(working_narratives.items()):
                n.stage = NarrativeStage.INVALIDATED
                n.macro_state = S01MacroState.POI_INVALIDATED
                n.terminal_reason = "missing_htf_poi"
            self._narratives = {
                k: v for k, v in working_narratives.items()
                if v.stage in (NarrativeStage.SWEEP_SEEN, NarrativeStage.FVG_READY)
            }
            self._emitted_clusters = working_emitted_clusters
            self._last_bar_index = N
            self._last_timestamp = context.timestamp
            self._last_context_payload = context.to_dict()
            self._last_result = ()
            return ()

        dir_pois = [p for p in all_pois if getattr(p, "direction", "") == expected_poi_dir]
        if not dir_pois:
            self._macro_state = S01MacroState.WAIT_HTF_POI
            self._last_rejection_reason = "wrong_poi_direction"
            self._active_poi = None
            for k, n in list(working_narratives.items()):
                n.stage = NarrativeStage.INVALIDATED
                n.macro_state = S01MacroState.POI_INVALIDATED
                n.terminal_reason = "wrong_poi_direction"
            self._narratives = {
                k: v for k, v in working_narratives.items()
                if v.stage in (NarrativeStage.SWEEP_SEEN, NarrativeStage.FVG_READY)
            }
            self._emitted_clusters = working_emitted_clusters
            self._last_bar_index = N
            self._last_timestamp = context.timestamp
            self._last_context_payload = context.to_dict()
            self._last_result = ()
            return ()

        active_pois = []
        for p in dir_pois:
            is_close_through = (
                (expected_poi_dir == "bullish" and context.close < p.bottom)
                or (expected_poi_dir == "bearish" and context.close > p.top)
            )
            if getattr(p, "status", "active") != "invalidated" and not is_close_through:
                active_pois.append(p)

        if not active_pois:
            self._macro_state = S01MacroState.POI_INVALIDATED
            self._last_rejection_reason = "poi_invalidated"
            self._active_poi = None
            for k, n in list(working_narratives.items()):
                n.stage = NarrativeStage.INVALIDATED
                n.macro_state = S01MacroState.POI_INVALIDATED
                n.terminal_reason = "poi_invalidated"
            self._narratives = {
                k: v for k, v in working_narratives.items()
                if v.stage in (NarrativeStage.SWEEP_SEEN, NarrativeStage.FVG_READY)
            }
            self._emitted_clusters = working_emitted_clusters
            self._last_bar_index = N
            self._last_timestamp = context.timestamp
            self._last_context_payload = context.to_dict()
            self._last_result = ()
            return ()

        touched_pois = []
        for p in active_pois:
            overlaps_now = (context.low <= p.top and context.high >= p.bottom)
            touch_cnt = int(getattr(p, "touch_count", 0))
            if touch_cnt > 0 or overlaps_now:
                touched_pois.append(p)

        if not touched_pois:
            self._macro_state = S01MacroState.WAIT_HTF_POI
            self._last_rejection_reason = "price_not_in_htf_poi"
            self._active_poi = active_pois[0]
        else:
            self._active_poi = sorted(
                touched_pois,
                key=lambda p: (
                    abs(((p.top + p.bottom) / 2.0) - context.close),
                    -int(getattr(p, "touch_count", 0)),
                    p.poi_id,
                ),
            )[0]
            self._macro_state = S01MacroState.HTF_POI_ACTIVE
            self._last_rejection_reason = None

        # 7. Ingest new sweeps arriving at bar N (Only allowed after price in POI)
        if touched_pois and self._active_poi is not None:
            for sweep in context.recent_sweeps:
                if (
                    sweep.valid
                    and sweep.mode == self._config.mode
                    and sweep.index == N
                    and sweep.confirmed_at <= N
                    and sweep.swept_at <= N
                ):
                    # Extract semantic fields with backward-compatible defaults
                    l_side = getattr(sweep, "liquidity_side", None)
                    if l_side is None:
                        if sweep.pool_kind in ("equal_lows", "swing_low"):
                            l_side = "SELL_SIDE"
                        elif sweep.pool_kind in ("equal_highs", "swing_high"):
                            l_side = "BUY_SIDE"

                    rev_dir = getattr(sweep, "reversal_direction", None) or sweep.direction
                    raid_dir = getattr(sweep, "raid_direction", None) or ("bearish" if l_side == "SELL_SIDE" else "bullish")

                    # Semantic coherence check: reject any internally contradictory sweep
                    if l_side == "SELL_SIDE":
                        if sweep.direction != "bullish" or rev_dir != "bullish" or raid_dir != "bearish" or sweep.pool_kind not in {"equal_lows", "swing_low"}:
                            continue
                        sweep_trade_dir = "BUY"
                    elif l_side == "BUY_SIDE":
                        if sweep.direction != "bearish" or rev_dir != "bearish" or raid_dir != "bullish" or sweep.pool_kind not in {"equal_highs", "swing_high"}:
                            continue
                        sweep_trade_dir = "SELL"
                    else:
                        continue

                    # S1 strictly trades in the direction of HTF Bias:
                    # - HTF Bullish (expected_dir == "BUY") MUST sweep SELL_SIDE liquidity (lows) and reverse bullish
                    # - HTF Bearish (expected_dir == "SELL") MUST sweep BUY_SIDE liquidity (highs) and reverse bearish
                    if sweep_trade_dir != expected_dir:
                        continue
                    direction = sweep_trade_dir

                    s_key: SweepKey = (
                        sweep.mode,
                        sweep.direction,
                        sweep.index,
                        tuple(sorted(sweep.pool_indices)),
                    )
                    if s_key not in working_narratives:
                        touch_bar = getattr(self._active_poi, "last_touch_bar", None)
                        if touch_bar is None:
                            touch_bar = N
                        working_narratives[s_key] = S01Narrative(
                            sweep_key=s_key,
                            direction=direction,
                            sweep=sweep,
                            stage=NarrativeStage.SWEEP_SEEN,
                            macro_state=S01MacroState.WAIT_LTF_MSS,
                            poi=self._active_poi,
                            poi_touch_bar=touch_bar,
                        )

            # Experimental adaptive path: an HTF-aligned MSS can start the
            # narrative without a sweep.  The sweep path above remains
            # unchanged; this path still requires an FVG retest and all later
            # geometry / RR / execution gates.
            if self._config.allow_mss_without_sweep:
                for mss in context.recent_structures:
                    if not (
                        mss.valid if hasattr(mss, "valid") else True
                    ):
                        continue
                    if not (
                        mss.mode == self._config.mode
                        and mss.index == N
                        and mss.direction == ("bullish" if expected_dir == "BUY" else "bearish")
                        and mss.event_type in {"BOS", "CHoCH"}
                        and mss.break_type == "close"
                        and mss.confirmed_swing_at <= N
                        and mss.structure_leg_id is not None
                    ):
                        continue
                    if self._config.require_displacement and not mss.displacement:
                        continue

                    no_sweep_key: SweepKey = (
                        "MSS_NO_SWEEP",
                        mss.direction,
                        mss.index,
                        (mss.broken_swing_index,),
                    )
                    if no_sweep_key not in working_narratives:
                        touch_bar = getattr(self._active_poi, "last_touch_bar", None)
                        if touch_bar is None:
                            touch_bar = N
                        working_narratives[no_sweep_key] = S01Narrative(
                            sweep_key=no_sweep_key,
                            direction=expected_dir,
                            sweep=None,
                            trigger_type="mss_without_sweep",
                            stage=NarrativeStage.SWEEP_SEEN,
                            macro_state=S01MacroState.WAIT_LTF_MSS,
                            poi=self._active_poi,
                            poi_touch_bar=touch_bar,
                            mss=mss,
                            mss_key=(
                                mss.mode,
                                mss.event_type,
                                mss.direction,
                                mss.index,
                                mss.broken_swing_index,
                            ),
                        )

        # 8. Process active narratives
        # Deterministic processing order
        sorted_keys = sorted(
            working_narratives.keys(),
            key=lambda k: (
                working_narratives[k].sweep.index
                if working_narratives[k].sweep is not None
                else (working_narratives[k].mss.index if working_narratives[k].mss is not None else N),
                k,
            ),
        )

        for s_key in sorted_keys:
            narrative = working_narratives[s_key]

            # Invalidate narrative if HTF bias changed
            if narrative.direction != expected_dir:
                narrative.stage = NarrativeStage.INVALIDATED
                narrative.macro_state = S01MacroState.BIAS_CHANGED
                narrative.terminal_reason = "bias_changed"
                continue

            # Invalidate narrative if linked POI was invalidated
            if narrative.poi is not None:
                is_close_through = (
                    (narrative.direction == "BUY" and context.close < narrative.poi.bottom)
                    or (narrative.direction == "SELL" and context.close > narrative.poi.top)
                )
                poi_still_active = any(
                    p.poi_id == narrative.poi.poi_id and getattr(p, "status", "active") != "invalidated"
                    for p in all_pois
                )
                if is_close_through or not poi_still_active:
                    narrative.stage = NarrativeStage.INVALIDATED
                    narrative.macro_state = S01MacroState.POI_INVALIDATED
                    narrative.terminal_reason = "poi_invalidated"
                    continue

            # --- Stage: SWEEP_SEEN -> Look for valid MSS & FVG ---
            if narrative.stage == NarrativeStage.SWEEP_SEEN:
                if narrative.sweep is None:
                    assert narrative.mss is not None
                    if N > narrative.mss.index + self._config.fvg_to_mss_max_bars:
                        narrative.stage = NarrativeStage.EXPIRED
                        narrative.macro_state = S01MacroState.ENTRY_EXPIRED
                        narrative.terminal_reason = "fvg_not_confirmed"
                        continue
                    valid_fvg = self._find_valid_fvg_for_mss(narrative.mss, context)
                    if valid_fvg is not None:
                        narrative.stage = NarrativeStage.FVG_READY
                        narrative.macro_state = S01MacroState.WAIT_LTF_RETEST
                        narrative.fvg = valid_fvg
                        narrative.fvg_key = (
                            valid_fvg.mode,
                            valid_fvg.direction,
                            valid_fvg.index,
                        )
                        narrative.ready_at = narrative.mss.index
                        narrative.expiry_bar = (
                            narrative.mss.index + self._config.entry_expiry_bars
                        )
                        narrative.fvg_filled_internally = False
                        narrative.fvg_filled_bar = None
                else:
                    if N > narrative.sweep.index + self._config.sweep_to_mss_max_bars:
                        narrative.stage = NarrativeStage.EXPIRED
                        narrative.macro_state = S01MacroState.SWEEP_EXPIRED
                        narrative.terminal_reason = "mss_not_confirmed"
                        continue

                    # Search candidate (MSS, FVG) pair. In adaptive mode a
                    # sweep only needs a CHoCH; legacy mode keeps both BOS and
                    # CHoCH behavior exactly as before.
                    valid_pair = self._find_valid_mss_fvg_pair(
                        narrative,
                        context,
                        allowed_event_types={"CHoCH"}
                        if self._config.allow_mss_without_sweep
                        else None,
                    )
                    if valid_pair is not None:
                        valid_mss, valid_fvg = valid_pair
                        narrative.stage = NarrativeStage.FVG_READY
                        narrative.macro_state = S01MacroState.WAIT_LTF_RETEST
                        narrative.mss = valid_mss
                        narrative.mss_key = (
                            valid_mss.mode,
                            valid_mss.event_type,
                            valid_mss.direction,
                            valid_mss.index,
                            valid_mss.broken_swing_index,
                        )
                        narrative.fvg = valid_fvg
                        narrative.fvg_key = (
                            valid_fvg.mode,
                            valid_fvg.direction,
                            valid_fvg.index,
                        )
                        narrative.ready_at = valid_mss.index
                        narrative.expiry_bar = (
                            valid_mss.index + self._config.entry_expiry_bars
                        )
                        narrative.fvg_filled_internally = False
                        narrative.fvg_filled_bar = None

            # --- Stage: FVG_READY -> Invalidation & Retest ---
            if narrative.stage == NarrativeStage.FVG_READY:
                assert narrative.mss is not None
                assert narrative.fvg is not None
                assert narrative.ready_at is not None
                assert narrative.expiry_bar is not None

                # Must not retest on MSS bar itself
                if N <= narrative.ready_at:
                    continue

                # A. Sweep extreme invalidation
                if narrative.sweep is not None and (
                    narrative.direction == "BUY"
                    and context.close < narrative.sweep.price_wick
                ):
                    narrative.stage = NarrativeStage.INVALIDATED
                    narrative.macro_state = S01MacroState.LTF_ZONE_INVALIDATED
                    narrative.terminal_reason = "sweep_extreme_violated"
                    continue
                if narrative.sweep is not None and (
                    narrative.direction == "SELL"
                    and context.close > narrative.sweep.price_wick
                ):
                    narrative.stage = NarrativeStage.INVALIDATED
                    narrative.macro_state = S01MacroState.LTF_ZONE_INVALIDATED
                    narrative.terminal_reason = "sweep_extreme_violated"
                    continue

                # B. Opposite structure event invalidation (mss.index < event.index <= N)
                if self._has_opposite_structure_after_mss(narrative, context):
                    narrative.stage = NarrativeStage.INVALIDATED
                    narrative.macro_state = S01MacroState.LTF_ZONE_INVALIDATED
                    narrative.terminal_reason = "opposite_structure_event"
                    continue

                # C. Prior full-fill invalidation (filled at bar < N)
                if (
                    narrative.fvg_filled_internally
                    and narrative.fvg_filled_bar is not None
                    and narrative.fvg_filled_bar < N
                ):
                    narrative.stage = NarrativeStage.INVALIDATED
                    narrative.macro_state = S01MacroState.LTF_ZONE_INVALIDATED
                    narrative.terminal_reason = "fvg_filled_prior_to_retest"
                    continue

                # D. Expiry check
                if N > narrative.expiry_bar:
                    narrative.stage = NarrativeStage.EXPIRED
                    narrative.macro_state = S01MacroState.ENTRY_EXPIRED
                    narrative.terminal_reason = "entry_expired"
                    continue

                # Track internal FVG full-fill at current bar N
                fvg = narrative.fvg
                if narrative.direction == "BUY":
                    if context.low <= fvg.bottom:
                        narrative.fvg_filled_internally = True
                        narrative.fvg_filled_bar = N
                else:  # SELL
                    if context.high >= fvg.top:
                        narrative.fvg_filled_internally = True
                        narrative.fvg_filled_bar = N

                # E. Close violation check (blowing through FVG boundary)
                if narrative.direction == "BUY":
                    if context.close < fvg.bottom:
                        narrative.stage = NarrativeStage.INVALIDATED
                        narrative.macro_state = S01MacroState.LTF_ZONE_INVALIDATED
                        narrative.terminal_reason = "fvg_close_through"
                        continue
                else:  # SELL
                    if context.close > fvg.top:
                        narrative.stage = NarrativeStage.INVALIDATED
                        narrative.macro_state = S01MacroState.LTF_ZONE_INVALIDATED
                        narrative.terminal_reason = "fvg_close_through"
                        continue

                # F. Retest zone overlap check
                overlaps_zone = (
                    context.low <= fvg.top and context.high >= fvg.bottom
                )
                if not overlaps_zone:
                    narrative.macro_state = S01MacroState.WAIT_LTF_RETEST
                    continue

                # Retest detected!
                narrative.macro_state = S01MacroState.S1_READY

                # Build CandidateSetup
                cand = self._build_candidate(
                    context, narrative, working_emitted_clusters
                )
                if cand is not None:
                    candidates_for_bar.append(cand)
                    working_emitted_clusters[cand.evidence_cluster_id] = narrative.expiry_bar
                    narrative.stage = NarrativeStage.EMITTED
                    narrative.terminal_reason = "candidate_emitted"
                else:
                    narrative.stage = NarrativeStage.INVALIDATED
                    if narrative.terminal_reason in ("insufficient_rr", "rr_below_min"):
                        narrative.macro_state = S01MacroState.RR_INVALID

        # 9. Update overall strategy macro state
        if candidates_for_bar:
            self._macro_state = S01MacroState.S1_READY
        elif any(n.macro_state == S01MacroState.WAIT_LTF_RETEST for n in working_narratives.values() if n.stage == NarrativeStage.FVG_READY):
            self._macro_state = S01MacroState.WAIT_LTF_RETEST
        elif any(n.macro_state == S01MacroState.WAIT_LTF_MSS for n in working_narratives.values() if n.stage == NarrativeStage.SWEEP_SEEN):
            self._macro_state = S01MacroState.WAIT_LTF_MSS
        elif touched_pois:
            self._macro_state = S01MacroState.WAIT_LTF_SWEEP

        # 10. Prune terminal narratives (EMITTED, EXPIRED, INVALIDATED)
        active_narratives = {
            k: v
            for k, v in working_narratives.items()
            if v.stage in (NarrativeStage.SWEEP_SEEN, NarrativeStage.FVG_READY)
        }

        # 11. Sort candidates canonically and commit state
        candidates_for_bar.sort(
            key=lambda c: (c.direction, c.evidence_cluster_id, c.setup_id)
        )
        final_result = tuple(candidates_for_bar)

        self._narratives = active_narratives
        self._emitted_clusters = working_emitted_clusters
        self._last_bar_index = N
        self._last_timestamp = context.timestamp
        self._last_context_payload = context.to_dict()
        self._last_result = final_result

        return final_result

    def _assert_zero_future_leak(self, context: StrategyContext) -> None:
        """Defensive guard ensuring StrategyContext contains zero future-leaking evidence."""
        N = context.bar_index
        for s in context.recent_sweeps:
            if s.index > N or s.confirmed_at > N or s.swept_at > N:
                raise StrategyStateError(
                    f"Future leak in sweep: index={s.index}, confirmed_at={s.confirmed_at}, swept_at={s.swept_at} > bar_index={N}."
                )
        for m in context.recent_structures:
            if m.index > N or m.confirmed_swing_at > N:
                raise StrategyStateError(
                    f"Future leak in structure event: index={m.index}, confirmed_swing_at={m.confirmed_swing_at} > bar_index={N}."
                )
        for f in context.active_fvgs:
            if f.index > N or f.confirmed_at > N or (f.filled_at is not None and f.filled_at > N):
                raise StrategyStateError(
                    f"Future leak in FVG: index={f.index}, confirmed_at={f.confirmed_at}, filled_at={f.filled_at} > bar_index={N}."
                )
        for p in context.active_pools:
            if p.confirmed_at > N or (p.swept_at is not None and p.swept_at > N):
                raise StrategyStateError(
                    f"Future leak in pool: confirmed_at={p.confirmed_at}, swept_at={p.swept_at} > bar_index={N}."
                )

        # HTF Bias future leak defense
        if context.htf_bias is not None:
            b = context.htf_bias
            close_time = context.bar_close_time

            def _ensure_comparable(t1: pd.Timestamp, t2: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
                if t1.tzinfo is None and t2.tzinfo is not None:
                    t1 = t1.tz_localize(t2.tzinfo)
                elif t1.tzinfo is not None and t2.tzinfo is None:
                    t2 = t2.tz_localize(t1.tzinfo)
                return t1, t2

            if b.as_of is not None:
                as_of, ref = _ensure_comparable(b.as_of, close_time)
                if as_of > ref:
                    raise StrategyStateError(
                        f"Future leak in htf_bias.as_of ({b.as_of}) > bar_close_time ({close_time})."
                    )
            if b.source_event_time is not None:
                src_time, ref = _ensure_comparable(b.source_event_time, close_time)
                if src_time > ref:
                    raise StrategyStateError(
                        f"Future leak in htf_bias.source_event_time ({b.source_event_time}) > bar_close_time ({close_time})."
                    )
            if b.timestamp is not None:
                ts, ref = _ensure_comparable(b.timestamp, close_time)
                if ts > ref:
                    raise StrategyStateError(
                        f"Future leak in htf_bias.timestamp ({b.timestamp}) > bar_close_time ({close_time})."
                    )
            if getattr(b, "pending_reversal_event_time", None) is not None:
                pr_time, ref = _ensure_comparable(b.pending_reversal_event_time, close_time)
                if pr_time > ref:
                    raise StrategyStateError(
                        f"Future leak in htf_bias.pending_reversal_event_time ({b.pending_reversal_event_time}) > bar_close_time ({close_time})."
                    )

        if context.session_decision is not None:
            close_time = context.bar_close_time
            sd_ts = context.session_decision.timestamp
            if sd_ts.tzinfo is None and close_time.tzinfo is not None:
                sd_ts = sd_ts.tz_localize(close_time.tzinfo)
            elif sd_ts.tzinfo is not None and close_time.tzinfo is None:
                close_time = close_time.tz_localize(sd_ts.tzinfo)
            if sd_ts > close_time:
                raise StrategyStateError(
                    f"Future leak in session_decision.timestamp ({context.session_decision.timestamp}) > bar_close_time ({close_time})."
                )

    def _find_valid_mss_fvg_pair(
        self,
        narrative: S01Narrative,
        context: StrategyContext,
        allowed_event_types: Optional[set[str]] = None,
    ) -> Optional[tuple[StructureEventSnapshot, FairValueGapSnapshot]]:
        """
        Find best deterministic (MSS, FVG) pair matching narrative sweep on same structure leg.
        Prevents an early MSS without an FVG from blocking a valid later MSS that has a valid FVG.
        """
        N = context.bar_index
        valid_pairs: list[tuple[StructureEventSnapshot, FairValueGapSnapshot]] = []
        assert narrative.sweep is not None
        event_types = allowed_event_types or {"BOS", "CHoCH"}

        # 1. Collect candidate MSS
        candidate_mss_list: list[StructureEventSnapshot] = []
        for mss in context.recent_structures:
            if (
                mss.mode == narrative.sweep.mode
                and mss.direction == narrative.sweep.direction
                and mss.event_type in event_types
                and mss.break_type == "close"
                and mss.index <= N
                and narrative.sweep.index < mss.index <= narrative.sweep.index + self._config.sweep_to_mss_max_bars
            ):
                if self._config.require_displacement and not mss.displacement:
                    continue
                if mss.structure_leg_id is None:
                    continue
                candidate_mss_list.append(mss)

        # 2. For each candidate MSS, find matching candidate FVGs
        for mss in candidate_mss_list:
            for fvg in context.active_fvgs:
                if (
                    fvg.mode == mss.mode
                    and fvg.direction == mss.direction
                    and narrative.sweep.index <= fvg.index < mss.index
                    and fvg.confirmed_at <= mss.index
                    and mss.index - fvg.index <= self._config.fvg_to_mss_max_bars
                    and fvg.structure_leg_id is not None
                    and fvg.structure_leg_id == mss.structure_leg_id
                ):
                    # Must not have been filled prior to or at MSS
                    if fvg.filled_at is not None and fvg.filled_at <= mss.index:
                        continue

                    # Must not have opposite structure event in closed window [fvg.index, mss.index]
                    if self._has_opposite_structure_in_window(
                        fvg.index, mss.index, mss.direction, context
                    ):
                        continue

                    valid_pairs.append((mss, fvg))

        if not valid_pairs:
            return None

        # Deterministic tie-breaking:
        # 1. mss.index ascending
        # 2. CHoCH before BOS when same index
        # 3. mss.broken_swing_index ascending
        # 4. mss.structure_leg_id ascending
        # 5. fvg.index descending (FVG closest to MSS)
        # 6. fvg.confirmed_at descending
        # 7. fvg.top ascending
        # 8. fvg.bottom ascending
        valid_pairs.sort(
            key=lambda pair: (
                pair[0].index,
                0 if pair[0].event_type == "CHoCH" else 1,
                pair[0].broken_swing_index,
                pair[0].structure_leg_id or "",
                -pair[1].index,
                -pair[1].confirmed_at,
                pair[1].top,
                pair[1].bottom,
            )
        )
        return valid_pairs[0]

    def _find_valid_fvg_for_mss(
        self,
        mss: StructureEventSnapshot,
        context: StrategyContext,
    ) -> Optional[FairValueGapSnapshot]:
        """Find an FVG linked to an HTF-aligned MSS without requiring a sweep."""
        N = context.bar_index
        candidates: list[FairValueGapSnapshot] = []
        for fvg in context.active_fvgs:
            if not (
                fvg.mode == mss.mode
                and fvg.direction == mss.direction
                and fvg.index != mss.index
                and fvg.confirmed_at <= N
                and fvg.structure_leg_id is not None
                and fvg.structure_leg_id == mss.structure_leg_id
                and abs(fvg.index - mss.index) <= self._config.fvg_to_mss_max_bars
            ):
                continue
            if fvg.filled_at is not None and fvg.filled_at <= N:
                continue
            lo, hi = sorted((mss.index, fvg.index))
            if self._has_opposite_structure_in_window(lo, hi, mss.direction, context):
                continue
            candidates.append(fvg)

        if not candidates:
            return None
        candidates.sort(
            key=lambda f: (
                abs(f.index - mss.index),
                0 if f.index >= mss.index else 1,
                -f.index,
                -f.confirmed_at,
                f.top,
                f.bottom,
            )
        )
        return candidates[0]

    def _has_opposite_structure_in_window(
        self,
        start_idx: int,
        end_idx: int,
        narrative_direction: str,
        context: StrategyContext,
    ) -> bool:
        """Check if any opposite structure event exists in closed range [start_idx, end_idx]."""
        for ev in context.recent_structures:
            if (
                ev.mode == self._config.mode
                and ev.direction != narrative_direction
                and ev.event_type in {"BOS", "CHoCH"}
                and start_idx <= ev.index <= end_idx
            ):
                return True
        return False

    def _has_opposite_structure_after_mss(
        self, narrative: S01Narrative, context: StrategyContext
    ) -> bool:
        """Check if any opposite structure event occurred after mss.index up to context.bar_index."""
        assert narrative.ready_at is not None
        structure_direction = "bullish" if narrative.direction == "BUY" else "bearish"
        for ev in context.recent_structures:
            if (
                ev.mode == self._config.mode
                and ev.direction != structure_direction
                and ev.event_type in {"BOS", "CHoCH"}
                and narrative.ready_at < ev.index <= context.bar_index
            ):
                return True
        return False

    def _build_candidate(
        self,
        context: StrategyContext,
        narrative: S01Narrative,
        working_emitted_clusters: Mapping[str, int],
    ) -> Optional[CandidateSetup]:
        """Build and validate CandidateSetup with strict precision rounding and evidence refs."""
        assert narrative.mss is not None
        assert narrative.fvg is not None
        assert narrative.expiry_bar is not None

        if narrative.poi is None:
            narrative.terminal_reason = "missing_htf_poi"
            return None
        if narrative.poi_touch_bar is None:
            narrative.terminal_reason = "price_not_in_htf_poi"
            return None

        direction = narrative.direction
        sweep = narrative.sweep
        mss = narrative.mss
        fvg = narrative.fvg
        N = context.bar_index

        # 1. Entry price calculation
        if self._config.entry_level == "proximal":
            raw_entry = fvg.top if direction == "BUY" else fvg.bottom
        else:  # "ce_50"
            raw_entry = (fvg.top + fvg.bottom) / 2.0

        # 2. Stop loss calculation
        if direction == "BUY":
            anchor = (
                min(sweep.price_wick, mss.broken_swing_price)
                if sweep is not None
                else mss.broken_swing_price
            )
            raw_sl = anchor - self._config.sl_buffer_price
        else:  # SELL
            anchor = (
                max(sweep.price_wick, mss.broken_swing_price)
                if sweep is not None
                else mss.broken_swing_price
            )
            raw_sl = anchor + self._config.sl_buffer_price

        # 3. Target selection from opposing active pools
        target_pool: Optional[LiquidityPoolSnapshot] = None
        target_type = "fixed_rr"
        raw_tp: float

        eligible_pools: list[LiquidityPoolSnapshot] = []
        for p in context.active_pools:
            if (
                p.valid
                and not p.swept
                and p.confirmed_at <= N
                and p.mode == self._config.mode
            ):
                if direction == "BUY" and p.kind in {"equal_highs", "swing_high"} and p.price > raw_entry:
                    eligible_pools.append(p)
                elif direction == "SELL" and p.kind in {"equal_lows", "swing_low"} and p.price < raw_entry:
                    eligible_pools.append(p)

        if eligible_pools:
            eligible_pools.sort(
                key=lambda p: (
                    abs(p.price - raw_entry),
                    p.confirmed_at,
                    p.kind,
                    tuple(sorted(p.indices)),
                )
            )
            closest_pool = eligible_pools[0]
            cand_tp = closest_pool.price

            # Round levels to test RR feasibility
            test_e = round(raw_entry, 3)
            test_sl = round(raw_sl, 3)
            test_tp = round(cand_tp, 3)

            # Geometry check for structural pool target
            if (direction == "BUY" and test_sl < test_e < test_tp) or (
                direction == "SELL" and test_tp < test_e < test_sl
            ):
                risk = (test_e - test_sl) if direction == "BUY" else (test_sl - test_e)
                reward = (test_tp - test_e) if direction == "BUY" else (test_e - test_tp)
                if risk > 0.0:
                    cand_rr = round(reward / risk, 2)
                    if cand_rr >= self._config.min_rr:
                        target_pool = closest_pool
                        target_type = "opposing_pool"
                        raw_tp = cand_tp

        if target_pool is None:
            # Fallback to fixed RR
            target_type = "fixed_rr"
            if direction == "BUY":
                raw_tp = raw_entry + self._config.fallback_rr * (raw_entry - raw_sl)
            else:
                raw_tp = raw_entry - self._config.fallback_rr * (raw_sl - raw_entry)

        # 4. Final 3-decimal rounding
        entry_price = round(raw_entry, 3)
        stop_loss = round(raw_sl, 3)
        take_profit = round(raw_tp, 3)

        # 5. Strict post-rounding geometry validation
        if direction == "BUY":
            if not (stop_loss < entry_price < take_profit):
                narrative.terminal_reason = "geometry_collapse"
                return None
            risk = entry_price - stop_loss
            reward = take_profit - entry_price
        else:  # SELL
            if not (take_profit < entry_price < stop_loss):
                narrative.terminal_reason = "geometry_collapse"
                return None
            risk = stop_loss - entry_price
            reward = entry_price - take_profit

        if risk <= 0.0:
            narrative.terminal_reason = "geometry_collapse"
            return None

        # 6. Final 2-decimal planned RR calculation
        planned_rr = round(reward / risk, 2)
        if planned_rr < self._config.min_rr:
            narrative.terminal_reason = "rr_below_min"
            return None

        # 7. Cluster ID & duplicate emission check
        leg_component = f"leg-{self._config.mode}-{direction}-{mss.broken_swing_index}"
        zone_component = f"fvg-{self._config.mode}-{direction}-{fvg.index}"
        cluster_id = make_cluster_id(direction, leg_component, zone_component)

        if cluster_id in working_emitted_clusters:
            narrative.terminal_reason = "cluster_already_emitted"
            return None

        setup_id = make_setup_id(self.strategy_id, direction, N, cluster_id)

        # 8. Evidence references in canonical order: optional Sweep -> MSS -> FVG [-> Pool]
        ev_sweep_id: Optional[str] = None
        ev_sweep: Optional[EvidenceRef] = None
        sweep_l_side: Optional[str] = None
        sweep_raid_dir: Optional[str] = None
        sweep_rev_dir: Optional[str] = None
        if sweep is not None:
            sweep_l_side = getattr(
                sweep,
                "liquidity_side",
                "SELL_SIDE" if sweep.pool_kind in ("equal_lows", "swing_low") else "BUY_SIDE",
            )
            sweep_raid_dir = getattr(
                sweep,
                "raid_direction",
                "bearish" if sweep.pool_kind in ("equal_lows", "swing_low") else "bullish",
            )
            sweep_rev_dir = getattr(sweep, "reversal_direction", sweep.direction)

            indices_token = "_".join(str(i) for i in sorted(sweep.pool_indices)) or "none"
            sub_key_sweep = f"sweep_{sweep.direction}_{indices_token}"
            ev_sweep_id = make_evidence_id(
                "liquidity_sweep",
                self._config.mode,
                sweep.index,
                sub_key_sweep,
            )
            ev_sweep = EvidenceRef(
                evidence_id=ev_sweep_id,
                kind="liquidity_sweep",
                bar_index=sweep.index,
                price=sweep.price_wick,
                time=sweep.time,
                details={
                    "pool_indices": list(sorted(sweep.pool_indices)),
                    "direction": sweep.direction,
                    "pool_kind": sweep.pool_kind,
                    "sweep_type": sweep.sweep_type,
                    "liquidity_side": sweep_l_side,
                    "raid_direction": sweep_raid_dir,
                    "reversal_direction": sweep_rev_dir,
                },
            )

        ev_mss_id = make_evidence_id(
            "structure_event",
            self._config.mode,
            mss.index,
            f"{mss.event_type}_{mss.direction}_{mss.index}",
        )
        ev_mss = EvidenceRef(
            evidence_id=ev_mss_id,
            kind="structure_event",
            bar_index=mss.index,
            price=mss.broken_swing_price,
            time=mss.time,
            details={
                "event_type": mss.event_type,
                "direction": mss.direction,
                "broken_swing_index": mss.broken_swing_index,
                "displacement": mss.displacement,
                "structure_leg_id": mss.structure_leg_id,
            },
        )

        ev_fvg_id = make_evidence_id(
            "fair_value_gap",
            self._config.mode,
            fvg.index,
            f"fvg_{fvg.direction}_{fvg.index}",
        )
        ev_fvg = EvidenceRef(
            evidence_id=ev_fvg_id,
            kind="fair_value_gap",
            bar_index=fvg.index,
            price=entry_price,
            time=fvg.time,
            details={
                "top": fvg.top,
                "bottom": fvg.bottom,
                "direction": fvg.direction,
                "structure_leg_id": fvg.structure_leg_id,
            },
        )

        evidences: tuple[EvidenceRef, ...]
        if target_pool is not None:
            pool_indices_token = (
                "_".join(str(i) for i in sorted(target_pool.indices)) or "none"
            )
            sub_key_pool = f"target_pool_{target_pool.kind}_{pool_indices_token}"
            ev_pool_id = make_evidence_id(
                "liquidity_pool",
                self._config.mode,
                target_pool.confirmed_at,
                sub_key_pool,
            )
            ev_pool = EvidenceRef(
                evidence_id=ev_pool_id,
                kind="liquidity_pool",
                bar_index=target_pool.confirmed_at,
                price=target_pool.price,
                time=getattr(target_pool, "time", None),
                details={
                    "kind": target_pool.kind,
                    "price": target_pool.price,
                    "indices": list(sorted(target_pool.indices)),
                },
            )
            evidences = ((ev_sweep,) if ev_sweep is not None else ()) + (ev_mss, ev_fvg, ev_pool)
        else:
            evidences = ((ev_sweep,) if ev_sweep is not None else ()) + (ev_mss, ev_fvg)

        # 9. Audit metadata dictionary
        metadata: dict[str, Any] = {
            "signal_bar": N,
            "available_from_bar": N + 1,
            "sweep_key": (
                [
                    narrative.sweep_key[0],
                    narrative.sweep_key[1],
                    narrative.sweep_key[2],
                    list(narrative.sweep_key[3]),
                ]
                if sweep is not None
                else None
            ),
            "trigger_type": narrative.trigger_type,
            "mss_key": (
                list(narrative.mss_key) if narrative.mss_key else None
            ),
            "fvg_key": (
                list(narrative.fvg_key) if narrative.fvg_key else None
            ),
            "structure_leg_id": mss.structure_leg_id,
            "ready_at": narrative.ready_at,
            "expiry_bar": narrative.expiry_bar,
            "entry_level": self._config.entry_level,
            "stop_anchor": "sweep_or_broken_swing_extreme" if sweep is not None else "broken_swing_extreme",
            "sl_buffer_price": self._config.sl_buffer_price,
            "target_source": target_type,
            "target_pool_key": (
                [target_pool.mode, target_pool.kind, list(target_pool.indices)]
                if target_pool
                else None
            ),
            "fixed_rr": (
                self._config.fallback_rr if target_type == "fixed_rr" else None
            ),
            "rr_basis": "structural_pre_fill",
            # Section 8 POI metadata linkage
            "htf_bias": context.htf_bias.bias if context.htf_bias else None,
            "htf_bias_source": getattr(context.htf_bias, "source_event_type", None) if context.htf_bias else None,
            "poi_id": getattr(narrative.poi, "poi_id", None) if narrative.poi else None,
            "poi_type": getattr(narrative.poi, "poi_type", None) if narrative.poi else None,
            "poi_timeframe": getattr(narrative.poi, "timeframe", None) if narrative.poi else None,
            "poi_top": float(getattr(narrative.poi, "top", 0.0)) if narrative.poi else None,
            "poi_bottom": float(getattr(narrative.poi, "bottom", 0.0)) if narrative.poi else None,
            "poi_touch_bar": narrative.poi_touch_bar,
            "ltf_sweep_id": ev_sweep_id,
            "ltf_mss_id": ev_mss_id,
            "ltf_fvg_id": ev_fvg_id,
            "ltf_ob_id": narrative.ltf_ob_id,
            # Semantic direction metadata
            "sweep_liquidity_side": sweep_l_side,
            "sweep_raid_direction": sweep_raid_dir,
            "sweep_reversal_direction": sweep_rev_dir,
            "mss_direction": mss.direction,
            "fvg_direction": fvg.direction,
            "trade_direction": direction,
        }

        # 10. Construct immutable CandidateSetup
        return CandidateSetup(
            setup_id=setup_id,
            strategy_id=self.strategy_id,
            direction=direction,
            bar_index=N,
            timestamp=context.timestamp,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            planned_rr=planned_rr,
            evidences=evidences,
            evidence_cluster_id=cluster_id,
            expiry_bar=narrative.expiry_bar,
            target_type=target_type,
            meta=metadata,
        )


__all__ = [
    "S01Config",
    "S01ICT2022Strategy",
    "NarrativeStage",
    "S01MacroState",
]
