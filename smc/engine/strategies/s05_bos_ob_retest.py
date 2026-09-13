"""
smc/engine/strategies/s05_bos_ob_retest.py
==========================================
S05: BOS -> Order Block First Retest Strategy Template (Continuation).
Deterministic, multi-narrative state machine implementation adhering to T53.5 and ADR 21.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Literal, Dict, Tuple

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
    HTFPOISnapshot,
    LiquidityPoolSnapshot,
    OrderBlockSnapshot,
    StrategyContext,
    StrategyProfile,
    StructureEventSnapshot,
    make_cluster_id,
    make_evidence_id,
    make_setup_id,
)
from smc.models import (
    FairValueGap,
    HTFPOI,
    OrderBlock,
    StructureEvent,
)
from smc.engine.protocol import validate_strategy_id


QUALITY_ORDER: dict[str, int] = {
    "base": 1,
    "strong": 2,
    "premium_candidate": 3,
}


@dataclass(frozen=True)
class S05Config:
    """
    Immutable configuration for S05 BOS -> Order Block First Retest Strategy.

    Attributes:
        mode: Detection mode ('internal' or 'swing'). Default 'internal'.
        max_ob_age_bars: Maximum bars from OB creation to retest. Default 25.
        entry_level: Entry level ('proximal' or 'ce_50'). Default 'proximal'.
        sl_buffer_price: Extra price buffer beyond OB distal. Default 0.20.
        min_rr: Minimum structural risk/reward ratio. Default 1.50.
        fallback_rr: Fallback fixed RR when opposing pool is absent or RR < min_rr. Default 2.00.
        require_displacement: Whether BOS must have displacement=True. Default True.
        min_ob_quality: Minimum OB quality ('base', 'strong', 'premium_candidate'). Default 'base'.
        require_ltf_confirmation: Whether to require top-down HTF OB -> LTF MSS/CHoCH confirmation. Default False.
        ltf_confirmation_event_types: Allowed confirmation event types. Default ("CHoCH", "BOS").
        ltf_entry_zone: Entry zone selection mode ('fvg', 'ob', 'either', 'confluence'). Default 'either'.
        ltf_zone_expiry_bars: Expiry bars after confirmation for LTF zone retest. Default 25.
        sl_anchor: SL anchor distal source ('ltf_zone', 'ltf_ob', 'ltf_fvg'). Default 'ltf_zone'.
    """

    mode: str = "internal"
    max_ob_age_bars: int = 25
    entry_level: str = "proximal"
    sl_buffer_price: float = 0.20
    min_rr: float = 1.50
    fallback_rr: float = 2.00
    require_displacement: bool = True
    min_ob_quality: str = "base"
    require_ltf_confirmation: bool = False
    ltf_confirmation_event_types: tuple[str, ...] = ("CHoCH", "BOS")
    ltf_entry_zone: str = "either"
    ltf_zone_expiry_bars: int = 25
    sl_anchor: str = "ltf_zone"

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

        # 2. max_ob_age_bars
        if isinstance(self.max_ob_age_bars, (bool, np.bool_)) or not isinstance(
            self.max_ob_age_bars, (int, np.integer)
        ):
            raise StrategyValidationError(
                f"max_ob_age_bars must be an integer, got {type(self.max_ob_age_bars).__name__}."
            )
        if int(self.max_ob_age_bars) <= 0:
            raise StrategyValidationError("max_ob_age_bars must be > 0.")
        object.__setattr__(self, "max_ob_age_bars", int(self.max_ob_age_bars))

        # 3. entry_level
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

        # 4. sl_buffer_price
        if isinstance(self.sl_buffer_price, (bool, np.bool_)) or not isinstance(
            self.sl_buffer_price, (int, float, np.integer, np.floating)
        ):
            raise StrategyValidationError(
                f"sl_buffer_price must be a numeric float, got {type(self.sl_buffer_price).__name__}."
            )
        f_sl = float(self.sl_buffer_price)
        if not math.isfinite(f_sl) or f_sl <= 0.0:
            raise StrategyValidationError("sl_buffer_price must be a positive finite float.")
        object.__setattr__(self, "sl_buffer_price", f_sl)

        # 5. min_rr
        if isinstance(self.min_rr, (bool, np.bool_)) or not isinstance(
            self.min_rr, (int, float, np.integer, np.floating)
        ):
            raise StrategyValidationError(
                f"min_rr must be a numeric float, got {type(self.min_rr).__name__}."
            )
        f_min_rr = float(self.min_rr)
        if not math.isfinite(f_min_rr) or f_min_rr <= 0.0:
            raise StrategyValidationError("min_rr must be a positive finite float.")
        object.__setattr__(self, "min_rr", f_min_rr)

        # 6. fallback_rr
        if isinstance(self.fallback_rr, (bool, np.bool_)) or not isinstance(
            self.fallback_rr, (int, float, np.integer, np.floating)
        ):
            raise StrategyValidationError(
                f"fallback_rr must be a numeric float, got {type(self.fallback_rr).__name__}."
            )
        f_fb_rr = float(self.fallback_rr)
        if not math.isfinite(f_fb_rr) or f_fb_rr < f_min_rr:
            raise StrategyValidationError(
                f"fallback_rr ({f_fb_rr}) must be >= min_rr ({f_min_rr})."
            )
        object.__setattr__(self, "fallback_rr", f_fb_rr)

        # 7. require_displacement
        if not isinstance(self.require_displacement, (bool, np.bool_)):
            raise StrategyValidationError(
                f"require_displacement must be a boolean, got {type(self.require_displacement).__name__}."
            )
        object.__setattr__(self, "require_displacement", bool(self.require_displacement))

        # 8. min_ob_quality
        if isinstance(self.min_ob_quality, (bool, np.bool_)) or not isinstance(
            self.min_ob_quality, str
        ):
            raise StrategyValidationError(
                f"min_ob_quality must be a string, got {type(self.min_ob_quality).__name__}."
            )
        if self.min_ob_quality not in QUALITY_ORDER:
            raise StrategyValidationError(
                f"min_ob_quality must be one of {sorted(QUALITY_ORDER.keys())}, got '{self.min_ob_quality}'."
            )

        # 9. require_ltf_confirmation
        if not isinstance(self.require_ltf_confirmation, (bool, np.bool_)):
            raise StrategyValidationError(
                f"require_ltf_confirmation must be a boolean, got {type(self.require_ltf_confirmation).__name__}."
            )
        object.__setattr__(self, "require_ltf_confirmation", bool(self.require_ltf_confirmation))

        # 10. ltf_confirmation_event_types
        if isinstance(self.ltf_confirmation_event_types, str):
            c_types = (self.ltf_confirmation_event_types,)
        elif isinstance(self.ltf_confirmation_event_types, (list, tuple)):
            c_types = tuple(self.ltf_confirmation_event_types)
        else:
            raise StrategyValidationError(
                f"ltf_confirmation_event_types must be a tuple/list of strings, got {type(self.ltf_confirmation_event_types).__name__}."
            )
        if not c_types:
            raise StrategyValidationError("ltf_confirmation_event_types cannot be empty.")
        for t in c_types:
            if t not in ("CHoCH", "BOS", "MSS"):
                raise StrategyValidationError(
                    f"ltf_confirmation_event_types elements must be 'CHoCH', 'BOS', or 'MSS', got '{t}'."
                )
        object.__setattr__(self, "ltf_confirmation_event_types", c_types)

        # 11. ltf_entry_zone
        if isinstance(self.ltf_entry_zone, (bool, np.bool_)) or not isinstance(
            self.ltf_entry_zone, str
        ):
            raise StrategyValidationError(
                f"ltf_entry_zone must be a string, got {type(self.ltf_entry_zone).__name__}."
            )
        if self.ltf_entry_zone not in ("fvg", "ob", "either", "confluence"):
            raise StrategyValidationError(
                f"ltf_entry_zone must be 'fvg', 'ob', 'either', or 'confluence', got '{self.ltf_entry_zone}'."
            )

        # 12. ltf_zone_expiry_bars
        if isinstance(self.ltf_zone_expiry_bars, (bool, np.bool_)) or not isinstance(
            self.ltf_zone_expiry_bars, (int, np.integer)
        ):
            raise StrategyValidationError(
                f"ltf_zone_expiry_bars must be an integer, got {type(self.ltf_zone_expiry_bars).__name__}."
            )
        if int(self.ltf_zone_expiry_bars) <= 0:
            raise StrategyValidationError("ltf_zone_expiry_bars must be > 0.")
        object.__setattr__(self, "ltf_zone_expiry_bars", int(self.ltf_zone_expiry_bars))

        # 13. sl_anchor
        if isinstance(self.sl_anchor, (bool, np.bool_)) or not isinstance(
            self.sl_anchor, str
        ):
            raise StrategyValidationError(
                f"sl_anchor must be a string, got {type(self.sl_anchor).__name__}."
            )
        if self.sl_anchor not in ("ltf_zone", "ltf_ob", "ltf_fvg"):
            raise StrategyValidationError(
                f"sl_anchor must be 'ltf_zone', 'ltf_ob', or 'ltf_fvg', got '{self.sl_anchor}'."
            )

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe configuration dictionary."""
        return {
            "mode": self.mode,
            "max_ob_age_bars": self.max_ob_age_bars,
            "entry_level": self.entry_level,
            "sl_buffer_price": self.sl_buffer_price,
            "min_rr": self.min_rr,
            "fallback_rr": self.fallback_rr,
            "require_displacement": self.require_displacement,
            "min_ob_quality": self.min_ob_quality,
            "require_ltf_confirmation": self.require_ltf_confirmation,
            "ltf_confirmation_event_types": self.ltf_confirmation_event_types,
            "ltf_entry_zone": self.ltf_entry_zone,
            "ltf_zone_expiry_bars": self.ltf_zone_expiry_bars,
            "sl_anchor": self.sl_anchor,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> S05Config:
        """Construct immutable S05Config from mapping with strict validation."""
        if not isinstance(data, (dict, Mapping)):
            raise StrategyValidationError(
                f"Expected mapping for S05Config, got {type(data).__name__}."
            )
        allowed_keys = {
            "mode",
            "max_ob_age_bars",
            "entry_level",
            "sl_buffer_price",
            "min_rr",
            "fallback_rr",
            "require_displacement",
            "min_ob_quality",
            "require_ltf_confirmation",
            "ltf_confirmation_event_types",
            "ltf_entry_zone",
            "ltf_zone_expiry_bars",
            "sl_anchor",
        }
        extra_keys = set(data.keys()) - allowed_keys
        if extra_keys:
            raise StrategyValidationError(
                f"Unexpected config fields for S05Config: {sorted(extra_keys)}."
            )
        clean_data = dict(data)
        if "ltf_confirmation_event_types" in clean_data and isinstance(
            clean_data["ltf_confirmation_event_types"], list
        ):
            clean_data["ltf_confirmation_event_types"] = tuple(clean_data["ltf_confirmation_event_types"])
        return cls(**clean_data)


class S05NarrativeStage(str, Enum):
    BOS_SEEN = "BOS_SEEN"
    OB_READY = "OB_READY"
    EMITTED = "EMITTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class S05MacroState(str, Enum):
    WAIT_HTF_BIAS = "WAIT_HTF_BIAS"
    WAIT_HTF_OB = "WAIT_HTF_OB"
    WAIT_HTF_OB_RETEST = "WAIT_HTF_OB_RETEST"
    WAIT_LTF_CONFIRMATION = "WAIT_LTF_CONFIRMATION"
    WAIT_LTF_ENTRY_ZONE = "WAIT_LTF_ENTRY_ZONE"
    WAIT_LTF_RETEST = "WAIT_LTF_RETEST"
    S05_READY = "S05_READY"
    ORDER_SELECTED = "ORDER_SELECTED"
    FILLED = "FILLED"

    # Invalidation states
    HTF_OB_INVALIDATED = "HTF_OB_INVALIDATED"
    LTF_CONFIRMATION_EXPIRED = "LTF_CONFIRMATION_EXPIRED"
    LTF_ZONE_INVALIDATED = "LTF_ZONE_INVALIDATED"
    ENTRY_EXPIRED = "ENTRY_EXPIRED"
    OPPOSITE_STRUCTURE_SHIFT = "OPPOSITE_STRUCTURE_SHIFT"
    RR_INVALID = "RR_INVALID"


BOSKey = tuple[str, str, int, int, Optional[str]]
OBKey = tuple[str, str, int, int]


@dataclass
class S05Narrative:
    bos_key: BOSKey
    direction: str  # "BUY" or "SELL"
    bos: StructureEventSnapshot
    stage: S05NarrativeStage = S05NarrativeStage.BOS_SEEN
    ob: Optional[OrderBlockSnapshot] = None
    ob_key: Optional[OBKey] = None
    ready_at: Optional[int] = None
    effective_created_at: Optional[int] = None
    expiry_bar: Optional[int] = None
    terminal_reason: Optional[str] = None
    linkage_method: Optional[str] = None


@dataclass
class S05ConfirmationNarrative:
    narrative_id: str
    direction: str  # "BUY" or "SELL"
    htf_ob_id: str
    htf_ob: Any
    htf_ob_touch_bar: int
    macro_state: S05MacroState = S05MacroState.WAIT_LTF_CONFIRMATION
    confirmation: Optional[StructureEventSnapshot] = None
    confirmation_id: Optional[str] = None
    confirmation_type: Optional[str] = None
    confirmation_bar: Optional[int] = None
    entry_zone: Optional[Any] = None
    entry_zone_type: Optional[str] = None  # "fvg" or "ob"
    entry_zone_id: Optional[str] = None
    confluence_zone: Optional[Any] = None
    retest_bar: Optional[int] = None
    expiry_bar: Optional[int] = None
    terminal_reason: Optional[str] = None


class S05BOSOBRetestStrategy:
    """
    S05: BOS -> Order Block First Retest Strategy Template.

    Implements StrategyTemplate Protocol:
        - strategy_id: str = "S05"
        - profile: StrategyProfile
        - evaluate(context: StrategyContext) -> tuple[CandidateSetup, ...]
        - reset() -> None
    """

    strategy_id: str = "S05"

    def __init__(self, config: Optional[S05Config] = None) -> None:
        validate_strategy_id(self.strategy_id)
        if config is None:
            config = S05Config()
        elif not isinstance(config, S05Config):
            raise StrategyValidationError(
                f"Expected S05Config or None, got {type(config).__name__}."
            )
        self._config = config

        # Internal state
        self._narratives: dict[BOSKey, S05Narrative] = {}
        self._confirmation_narratives: dict[str, S05ConfirmationNarrative] = {}
        self._emitted_clusters: dict[str, int] = {}
        self._macro_state: S05MacroState = S05MacroState.WAIT_HTF_BIAS
        self._last_rejection_reason: Optional[str] = None
        self._active_htf_ob: Optional[Any] = None
        self._last_bar_index: Optional[int] = None
        self._last_timestamp: Optional[pd.Timestamp] = None
        self._last_context_payload: Optional[dict[str, Any]] = None
        self._last_result: Optional[tuple[CandidateSetup, ...]] = None

    @property
    def config(self) -> S05Config:
        """Return the immutable strategy configuration."""
        return self._config

    @property
    def profile(self) -> StrategyProfile:
        """Return the public immutable StrategyProfile."""
        return StrategyProfile(
            strategy_id=self.strategy_id,
            name="BOS Order Block First Retest",
            version="1.0.0",
            style="continuation",
            allowed_directions=("BUY", "SELL"),
            timeframes=("M1", "M5", "M15"),
            max_setup_age_bars=self._config.max_ob_age_bars,
            cooldown_bars=3,
            min_rr=self._config.min_rr,
            params=self._config.to_dict(),
        )

    @property
    def current_state(self) -> S05MacroState:
        """Return the current macro state."""
        return self._macro_state

    @property
    def last_rejection_reason(self) -> Optional[str]:
        """Return the latest rejection reason, if any."""
        return self._last_rejection_reason

    @property
    def active_htf_ob(self) -> Optional[Any]:
        """Return the currently tracked active HTF OB, if any."""
        return self._active_htf_ob

    def reset(self) -> None:
        """Reset internal state machine for replay or fresh run."""
        self._narratives.clear()
        self._confirmation_narratives.clear()
        self._emitted_clusters.clear()
        self._macro_state = S05MacroState.WAIT_HTF_BIAS
        self._last_rejection_reason = None
        self._active_htf_ob = None
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
                f"Timeframe '{context.timeframe}' not supported by S05 profile {self.profile.timeframes}."
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

        # 3. Future Leak Defense Check
        self._assert_zero_future_leak(context)

        if not self._config.require_ltf_confirmation:
            return self._evaluate_legacy(context)
        return self._evaluate_ltf_confirmation(context)

    def _evaluate_legacy(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
        N = context.bar_index
        # 4. Atomic working copy preparation
        working_narratives: dict[BOSKey, S05Narrative] = {
            k: copy.copy(v) for k, v in self._narratives.items()
        }
        # Prune expired clusters (N > expiry_bar)
        working_emitted_clusters: dict[str, int] = {
            cid: exp for cid, exp in self._emitted_clusters.items() if N <= exp
        }
        proposals: list[tuple[S05Narrative, OrderBlockSnapshot]] = []

        # 5. Ingest new BOS events arriving at bar N
        for event in context.recent_structures:
            if (
                event.index == N
                and event.event_type == "BOS"
                and event.mode == self._config.mode
            ):
                # Must be close-break
                if getattr(event, "break_type", "close") != "close":
                    continue
                # Displacement check
                if self._config.require_displacement and not event.displacement:
                    continue
                # Bias check at admission
                if context.htf_bias is None:
                    continue
                bias_val = context.htf_bias.bias
                if event.direction == "bullish" and bias_val != "bullish":
                    continue
                if event.direction == "bearish" and bias_val != "bearish":
                    continue

                # Ambiguity check: opposite BOS/CHoCH on the exact same bar N
                has_opposite_on_same_bar = any(
                    ev.index == N
                    and ev.mode == self._config.mode
                    and ev.event_type in ("BOS", "CHoCH")
                    and ev.direction != event.direction
                    for ev in context.recent_structures
                )
                if has_opposite_on_same_bar:
                    continue

                direction = "BUY" if event.direction == "bullish" else "SELL"
                bos_key: BOSKey = (
                    event.mode,
                    event.direction,
                    event.index,
                    event.broken_swing_index,
                    event.structure_leg_id,
                )
                if bos_key not in working_narratives:
                    working_narratives[bos_key] = S05Narrative(
                        bos_key=bos_key,
                        direction=direction,
                        bos=event,
                        stage=S05NarrativeStage.BOS_SEEN,
                        expiry_bar=event.index + self._config.max_ob_age_bars,
                    )

        # 6. Evaluate all active narratives
        sorted_keys = sorted(
            working_narratives.keys(),
            key=lambda k: (k[2], k[1], k[3], k[4] or ""),
        )

        for k in sorted_keys:
            narrative = working_narratives[k]
            if narrative.stage not in (
                S05NarrativeStage.BOS_SEEN,
                S05NarrativeStage.OB_READY,
            ):
                continue

            # A. Expiry check
            if narrative.expiry_bar is not None and N > narrative.expiry_bar:
                narrative.stage = S05NarrativeStage.EXPIRED
                narrative.terminal_reason = "expired"
                continue

            # B. HTF Bias alignment check
            if context.htf_bias is None:
                narrative.stage = S05NarrativeStage.INVALIDATED
                narrative.terminal_reason = "htf_bias_mismatch"
                continue
            cur_bias = context.htf_bias.bias
            if narrative.direction == "BUY" and cur_bias != "bullish":
                narrative.stage = S05NarrativeStage.INVALIDATED
                narrative.terminal_reason = "htf_bias_mismatch"
                continue
            if narrative.direction == "SELL" and cur_bias != "bearish":
                narrative.stage = S05NarrativeStage.INVALIDATED
                narrative.terminal_reason = "htf_bias_mismatch"
                continue

            # C. Opposite structure shift in closed interval [bos.index, N]
            has_opposite_shift = any(
                narrative.bos.index <= ev.index <= N
                and ev.mode == self._config.mode
                and ev.event_type in ("BOS", "CHoCH")
                and ev.direction != narrative.bos.direction
                for ev in context.recent_structures
            )
            if has_opposite_shift:
                narrative.stage = S05NarrativeStage.INVALIDATED
                narrative.terminal_reason = "opposite_structure_shift"
                continue

            # D. BOS_SEEN -> Find canonical OB match
            if narrative.stage == S05NarrativeStage.BOS_SEEN:
                match = self._find_canonical_ob_for_bos(narrative, context)
                if match is not None:
                    matched_ob, method, eff_created = match
                    narrative.ob = matched_ob
                    narrative.ob_key = (
                        matched_ob.mode,
                        matched_ob.direction,
                        matched_ob.source_event_index,
                        matched_ob.index,
                    )
                    narrative.linkage_method = method
                    narrative.ready_at = eff_created
                    narrative.effective_created_at = eff_created
                    narrative.expiry_bar = eff_created + self._config.max_ob_age_bars
                    narrative.stage = S05NarrativeStage.OB_READY

            # E. OB_READY -> Lifecycle refresh and first-retest check
            if narrative.stage == S05NarrativeStage.OB_READY:
                # Refresh snapshot from active_obs
                found_ob = next(
                    (
                        ob
                        for ob in context.active_obs
                        if (
                            ob.mode == narrative.ob_key[0]
                            and ob.direction == narrative.ob_key[1]
                            and ob.source_event_index == narrative.ob_key[2]
                            and ob.index == narrative.ob_key[3]
                        )
                    ),
                    None,
                )
                if found_ob is not None:
                    narrative.ob = found_ob
                else:
                    # Missing from active_obs (e.g. evicted by context capacity)
                    # Do not emit, narrative continues waiting until expiry
                    continue

                # Explicit invalidation check on refreshed snapshot
                if not narrative.ob.valid or narrative.ob.invalidated_at is not None:
                    narrative.stage = S05NarrativeStage.INVALIDATED
                    narrative.terminal_reason = "ob_invalidated"
                    continue

                # First retest airtight boundary:
                # 1. ob.mitigated_at == N
                # 2. ob.retest_count == 1
                # 3. ob.valid is True
                # 4. ob.invalidated_at is None
                # 5. N > effective_created_at
                # 6. N <= expiry_bar
                ob = narrative.ob
                eff_created = (
                    narrative.effective_created_at
                    if narrative.effective_created_at is not None
                    else (ob.created_at if ob.created_at != -1 else ob.source_event_index)
                )
                if (
                    ob.mitigated_at == N
                    and ob.retest_count == 1
                    and ob.valid is True
                    and ob.invalidated_at is None
                    and N > eff_created
                    and N <= narrative.expiry_bar
                ):
                    proposals.append((narrative, ob))

        # 7. Multi-narrative Same-OB Global Ownership Resolution
        # If multiple narratives propose emission on the same ob_key at bar N:
        # 1. exact source-event pair before same-leg fallback pair
        # 2. bos.index desc
        # 3. bos.broken_swing_index desc
        # 4. bos_key lexicographical asc
        ob_claims: dict[OBKey, list[tuple[S05Narrative, OrderBlockSnapshot]]] = {}
        for narr, ob in proposals:
            assert narr.ob_key is not None
            ob_claims.setdefault(narr.ob_key, []).append((narr, ob))

        winning_proposals: list[tuple[S05Narrative, OrderBlockSnapshot]] = []
        for ob_key, claim_list in ob_claims.items():
            if len(claim_list) == 1:
                winning_proposals.append(claim_list[0])
            else:
                def claim_rank_key(item):
                    n, _ = item
                    method_rank = 0 if n.linkage_method == "exact_source_event" else 1
                    return (
                        method_rank,
                        -n.bos.index,
                        -n.bos.broken_swing_index,
                        n.bos_key,
                    )
                sorted_claims = sorted(claim_list, key=claim_rank_key)
                winner = sorted_claims[0]
                winning_proposals.append(winner)
                # Losing narratives become terminal duplicate_ob_ownership
                for loser_narr, _ in sorted_claims[1:]:
                    loser_narr.stage = S05NarrativeStage.INVALIDATED
                    loser_narr.terminal_reason = "duplicate_ob_ownership"

        # 8. Build CandidateSetup for winning proposals
        candidates_for_bar: list[CandidateSetup] = []
        for narrative, ob in winning_proposals:
            mode = self._config.mode
            direction = narrative.direction
            leg_component = f"leg-{mode}-{direction}-{narrative.bos.index}-{narrative.bos.broken_swing_index}"
            zone_component = f"ob-{mode}-{direction}-{ob.source_event_index}-{ob.index}"
            cluster_id = make_cluster_id(direction, leg_component, zone_component)

            if cluster_id in working_emitted_clusters:
                narrative.stage = S05NarrativeStage.EMITTED
                continue

            cand = self._build_candidate(narrative, ob, cluster_id, context)
            if cand is not None:
                candidates_for_bar.append(cand)
                working_emitted_clusters[cluster_id] = (
                    narrative.expiry_bar if narrative.expiry_bar is not None else N + self._config.max_ob_age_bars
                )
                narrative.stage = S05NarrativeStage.EMITTED

        # 9. Canonical sort
        sorted_candidates = tuple(
            sorted(
                candidates_for_bar,
                key=lambda c: (c.direction, c.evidence_cluster_id, c.setup_id),
            )
        )

        # 10. Atomic state commit with terminal narrative pruning
        active_narratives = {
            k: v
            for k, v in working_narratives.items()
            if v.stage in (S05NarrativeStage.BOS_SEEN, S05NarrativeStage.OB_READY)
        }
        self._narratives = active_narratives
        self._emitted_clusters = working_emitted_clusters
        self._last_bar_index = N
        self._last_timestamp = context.timestamp
        self._last_context_payload = context.to_dict()
        self._last_result = sorted_candidates

        return sorted_candidates

    def _find_canonical_ob_for_bos(
        self, narrative: S05Narrative, context: StrategyContext
    ) -> Optional[tuple[OrderBlockSnapshot, str, int]]:
        """
        Search active_obs for eligible OB matching the given BOS narrative.
        Returns (selected_ob, linkage_method, effective_created_at) or None.
        """
        bos = narrative.bos
        N = context.bar_index
        candidates: list[tuple[OrderBlockSnapshot, str, int]] = []

        for ob in context.active_obs:
            if not ob.valid:
                continue
            if ob.direction != bos.direction:
                continue
            if ob.mode != bos.mode or ob.mode != self._config.mode:
                continue
            # Origin / source event type must be BOS
            if ob.origin_type != "BOS" and ob.source_event_type != "BOS":
                continue

            eff_created = ob.created_at if ob.created_at != -1 else ob.source_event_index
            if eff_created < bos.index or eff_created > N:
                continue

            # Quality check
            ob_qual_rank = QUALITY_ORDER.get(ob.quality, 0)
            req_qual_rank = QUALITY_ORDER.get(self._config.min_ob_quality, 1)
            if ob_qual_rank < req_qual_rank:
                continue

            # Linkage match
            linkage_method: Optional[str] = None
            if ob.source_event_index == bos.index:
                linkage_method = "exact_source_event"
            elif (
                ob.structure_leg_id is not None
                and bos.structure_leg_id is not None
                and ob.structure_leg_id == bos.structure_leg_id
            ):
                linkage_method = "same_leg_fallback"
            else:
                continue

            candidates.append((ob, linkage_method, eff_created))

        if not candidates:
            return None

        def ob_rank_key(item):
            cand_ob, method, eff_c = item
            method_rank = 0 if method == "exact_source_event" else 1
            qual_rank = -QUALITY_ORDER.get(cand_ob.quality, 0)
            source_candle_rank = -cand_ob.index
            return (
                method_rank,
                eff_c,
                qual_rank,
                source_candle_rank,
                cand_ob.low,
                cand_ob.high,
                cand_ob.source_event_index,
            )

        sorted_candidates = sorted(candidates, key=ob_rank_key)
        return sorted_candidates[0]

    def _build_candidate(
        self,
        narrative: S05Narrative,
        ob: OrderBlockSnapshot,
        cluster_id: str,
        context: StrategyContext,
    ) -> Optional[CandidateSetup]:
        """
        Build CandidateSetup for a first-retest of Order Block.
        """
        N = context.bar_index
        direction = narrative.direction
        mode = self._config.mode
        bos = narrative.bos

        # 1. Entry price
        if self._config.entry_level == "proximal":
            entry_price = ob.high if direction == "BUY" else ob.low
        else:  # "ce_50"
            entry_price = (ob.high + ob.low) / 2.0

        # 2. Stop loss (OB distal +- buffer)
        if direction == "BUY":
            stop_loss = ob.low - self._config.sl_buffer_price
        else:
            stop_loss = ob.high + self._config.sl_buffer_price

        # 3. Round entry and stop loss
        entry_price = round(entry_price, 3)
        stop_loss = round(stop_loss, 3)

        # 4. Risk check
        risk = (entry_price - stop_loss) if direction == "BUY" else (stop_loss - entry_price)
        if risk <= 0.0:
            return None

        # 5. Target selection
        target_pool: Optional[LiquidityPoolSnapshot] = None
        target_type: str = "fixed_rr"
        take_profit: Optional[float] = None

        # Filter active opposing liquidity pools
        eligible_pools: list[LiquidityPoolSnapshot] = []
        for p in context.active_pools:
            if not p.valid or p.swept:
                continue
            if p.mode != mode:
                continue
            if p.confirmed_at > N:
                continue
            if direction == "BUY":
                if p.kind in ("equal_highs", "swing_high") and p.price > entry_price:
                    eligible_pools.append(p)
            else:
                if p.kind in ("equal_lows", "swing_low") and p.price < entry_price:
                    eligible_pools.append(p)

        if eligible_pools:
            def pool_sort_key(p: LiquidityPoolSnapshot):
                dist = abs(p.price - entry_price)
                indices_sorted = tuple(sorted(p.indices))
                return (dist, p.confirmed_at, p.kind, indices_sorted)

            sorted_pools = sorted(eligible_pools, key=pool_sort_key)
            nearest_pool = sorted_pools[0]

            cand_tp = round(nearest_pool.price, 3)
            cand_reward = (cand_tp - entry_price) if direction == "BUY" else (entry_price - cand_tp)
            if cand_reward > 0:
                cand_rr = round(cand_reward / risk, 2)
                if cand_rr >= self._config.min_rr:
                    target_pool = nearest_pool
                    target_type = "opposing_pool"
                    take_profit = cand_tp

        # Fallback to fixed RR
        if take_profit is None:
            target_type = "fixed_rr"
            if direction == "BUY":
                take_profit = round(entry_price + self._config.fallback_rr * risk, 3)
            else:
                take_profit = round(entry_price - self._config.fallback_rr * risk, 3)

        # 6. Recalculate planned_rr from rounded levels
        reward = (take_profit - entry_price) if direction == "BUY" else (entry_price - take_profit)
        if reward <= 0.0:
            return None

        planned_rr = round(reward / risk, 2)
        if planned_rr < self._config.min_rr:
            return None

        # 7. Geometry validation
        if direction == "BUY" and not (stop_loss < entry_price < take_profit):
            return None
        if direction == "SELL" and not (take_profit < entry_price < stop_loss):
            return None

        # 8. Evidences
        # A. BOS Evidence
        bos_sub_key = f"BOS_{bos.direction}_{bos.broken_swing_index}"
        ev_bos_id = make_evidence_id("structure_event", mode, bos.index, bos_sub_key)
        ev_bos = EvidenceRef(
            evidence_id=ev_bos_id,
            kind="structure_event",
            bar_index=bos.index,
            price=bos.broken_swing_price,
            time=bos.time,
            details={
                "direction": bos.direction,
                "event_type": "BOS",
                "broken_swing_index": bos.broken_swing_index,
                "displacement": bos.displacement,
                "structure_leg_id": bos.structure_leg_id,
            },
        )

        # B. OB Evidence
        eff_created = narrative.effective_created_at if narrative.effective_created_at is not None else (ob.created_at if ob.created_at != -1 else ob.source_event_index)
        ob_sub_key = f"ob_{ob.direction}_{ob.source_event_index}_{ob.index}"
        ev_ob_id = make_evidence_id("order_block", mode, eff_created, ob_sub_key)
        ev_ob = EvidenceRef(
            evidence_id=ev_ob_id,
            kind="order_block",
            bar_index=eff_created,
            price=ob.high if direction == "BUY" else ob.low,
            time=ob.time,
            details={
                "direction": ob.direction,
                "high": ob.high,
                "low": ob.low,
                "source_candle_index": ob.index,
                "source_event_index": ob.source_event_index,
                "quality": ob.quality,
                "structure_leg_id": ob.structure_leg_id,
            },
        )

        # C. Optional Pool Evidence
        if target_pool is not None:
            pool_indices_token = "_".join(str(i) for i in sorted(target_pool.indices))
            pool_sub_key = f"target_pool_{target_pool.kind}_{pool_indices_token}"
            ev_pool_id = make_evidence_id("liquidity_pool", mode, target_pool.confirmed_at, pool_sub_key)
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
            evidences = (ev_bos, ev_ob, ev_pool)
        else:
            evidences = (ev_bos, ev_ob)

        # 9. Setup ID
        setup_id = make_setup_id(self.strategy_id, direction, N, cluster_id)

        # 10. Audit metadata
        metadata: dict[str, Any] = {
            "signal_bar": N,
            "available_from_bar": N + 1,
            "bos_key": [
                narrative.bos_key[0],
                narrative.bos_key[1],
                narrative.bos_key[2],
                narrative.bos_key[3],
                narrative.bos_key[4],
            ],
            "ob_key": (
                list(narrative.ob_key) if narrative.ob_key else None
            ),
            "linkage_method": narrative.linkage_method,
            "structure_leg_id": bos.structure_leg_id,
            "ready_at": narrative.ready_at,
            "expiry_bar": narrative.expiry_bar,
            "first_retest": True,
            "ob_quality": ob.quality,
            "mitigated_at": ob.mitigated_at,
            "retest_count": ob.retest_count,
            "entry_level": self._config.entry_level,
            "stop_anchor": "ob_distal",
            "sl_buffer_price": self._config.sl_buffer_price,
            "target_source": target_type,
            "target_pool_key": (
                [target_pool.mode, target_pool.kind, list(sorted(target_pool.indices))]
                if target_pool
                else None
            ),
            "fixed_rr": (
                self._config.fallback_rr if target_type == "fixed_rr" else None
            ),
            "htf_bias": context.htf_bias.bias if context.htf_bias else None,
            "rr_basis": "structural_pre_fill",
        }

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
            expiry_bar=narrative.expiry_bar if narrative.expiry_bar is not None else N + self._config.max_ob_age_bars,
            target_type=target_type,
            meta=metadata,
        )

    def _evaluate_ltf_confirmation(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
        N = context.bar_index

        # 1. HTF Bias validation
        if context.htf_bias is None or context.htf_bias.bias == "neutral":
            self._macro_state = S05MacroState.WAIT_HTF_BIAS
            self._last_rejection_reason = "missing_htf_bias"
            self._active_htf_ob = None
            for n in self._confirmation_narratives.values():
                n.macro_state = S05MacroState.HTF_OB_INVALIDATED
                n.terminal_reason = "missing_htf_bias"
            self._confirmation_narratives.clear()
            self._last_bar_index = N
            self._last_timestamp = context.timestamp
            self._last_context_payload = context.to_dict()
            self._last_result = ()
            return ()

        bias_val = context.htf_bias.bias
        expected_dir = "BUY" if bias_val == "bullish" else "SELL"
        expected_ob_dir = "bullish" if bias_val == "bullish" else "bearish"

        # 2. Copy active narratives and prune expired clusters
        working_narratives = {
            k: copy.copy(v) for k, v in self._confirmation_narratives.items()
        }
        working_emitted_clusters = {
            cid: exp for cid, exp in self._emitted_clusters.items() if N <= exp
        }

        # 3. Discover active HTF Order Blocks
        eligible_htf_obs: list[Any] = []
        for p in getattr(context, "active_htf_pois", ()):
            p_dir = getattr(p, "direction", "")
            p_type = getattr(p, "poi_type", "")
            if p_dir != expected_ob_dir:
                continue
            if p_type not in ("OB", "order_block", "HTF_OB"):
                continue
            p_status = getattr(p, "status", "active")
            if p_status == "invalidated":
                continue
            src_ev = getattr(p, "source_event", None)
            if src_ev is not None:
                ev_type = getattr(src_ev, "event_type", None)
                if isinstance(src_ev, dict):
                    ev_type = src_ev.get("event_type")
                if ev_type and ev_type != "BOS":
                    continue
            meta_dict = getattr(p, "meta", {})
            if isinstance(meta_dict, dict):
                orig_ev = meta_dict.get("origin_event")
                if isinstance(orig_ev, dict) and orig_ev.get("event_type") and orig_ev.get("event_type") != "BOS":
                    continue
            c_at = getattr(p, "created_at", None)
            if c_at is not None and isinstance(c_at, int) and c_at > N:
                continue
            eligible_htf_obs.append(p)

        for ob in getattr(context, "active_obs", ()):
            if getattr(ob, "direction", "") != expected_ob_dir:
                continue
            if not getattr(ob, "valid", True) or getattr(ob, "invalidated_at", None) is not None:
                continue
            orig_type = getattr(ob, "origin_type", getattr(ob, "source_event_type", ""))
            if orig_type != "BOS":
                continue
            eff_c = getattr(ob, "created_at", getattr(ob, "index", 0))
            if eff_c > N:
                continue
            ob_id = getattr(ob, "poi_id", f"ob_{ob.direction}_{ob.source_event_index}_{ob.index}")
            if not any(getattr(x, "poi_id", None) == ob_id for x in eligible_htf_obs):
                eligible_htf_obs.append(ob)

        if not eligible_htf_obs and not working_narratives:
            self._macro_state = S05MacroState.WAIT_HTF_OB
            self._last_rejection_reason = "missing_htf_ob"
            self._active_htf_ob = None
            self._confirmation_narratives = {}
            self._emitted_clusters = working_emitted_clusters
            self._last_bar_index = N
            self._last_timestamp = context.timestamp
            self._last_context_payload = context.to_dict()
            self._last_result = ()
            return ()

        # 4. Check Touch & Retest of HTF OBs
        has_touched_ob = False
        for ob in eligible_htf_obs:
            ob_top = getattr(ob, "top", getattr(ob, "high", 0.0))
            ob_bottom = getattr(ob, "bottom", getattr(ob, "low", 0.0))
            ob_id = getattr(ob, "poi_id", f"htf_ob_{getattr(ob, 'direction', '')}_{getattr(ob, 'index', 0)}")

            # Invalidation by close through
            if expected_dir == "BUY" and context.close < ob_bottom:
                if ob_id in working_narratives:
                    working_narratives[ob_id].macro_state = S05MacroState.HTF_OB_INVALIDATED
                    working_narratives[ob_id].terminal_reason = "htf_ob_closed_through"
                continue
            if expected_dir == "SELL" and context.close > ob_top:
                if ob_id in working_narratives:
                    working_narratives[ob_id].macro_state = S05MacroState.HTF_OB_INVALIDATED
                    working_narratives[ob_id].terminal_reason = "htf_ob_closed_through"
                continue

            # Overlap check
            is_touch_now = (context.low <= ob_top and context.high >= ob_bottom)
            prev_touch_cnt = int(getattr(ob, "touch_count", 0))
            last_touch_b = getattr(ob, "last_touch_bar", None)
            was_touched = (prev_touch_cnt > 0 and last_touch_b is not None and last_touch_b <= N)

            if is_touch_now or was_touched:
                has_touched_ob = True
                touch_bar = N if is_touch_now and not was_touched else (last_touch_b if last_touch_b is not None else N)
                if ob_id not in working_narratives:
                    working_narratives[ob_id] = S05ConfirmationNarrative(
                        narrative_id=f"narr_{ob_id}_{touch_bar}",
                        direction=expected_dir,
                        htf_ob_id=ob_id,
                        htf_ob=ob,
                        htf_ob_touch_bar=touch_bar,
                        macro_state=S05MacroState.WAIT_LTF_CONFIRMATION,
                        expiry_bar=touch_bar + self._config.max_ob_age_bars,
                    )
                self._active_htf_ob = ob

        if not has_touched_ob and not working_narratives:
            self._macro_state = S05MacroState.WAIT_HTF_OB_RETEST
            self._last_rejection_reason = "price_not_in_htf_ob"
            self._confirmation_narratives = {}
            self._emitted_clusters = working_emitted_clusters
            self._last_bar_index = N
            self._last_timestamp = context.timestamp
            self._last_context_payload = context.to_dict()
            self._last_result = ()
            return ()

        # 5. Evaluate Active Confirmation Narratives
        ready_proposals: list[S05ConfirmationNarrative] = []
        for narr in list(working_narratives.values()):
            if narr.macro_state in (
                S05MacroState.HTF_OB_INVALIDATED,
                S05MacroState.LTF_CONFIRMATION_EXPIRED,
                S05MacroState.LTF_ZONE_INVALIDATED,
                S05MacroState.ENTRY_EXPIRED,
                S05MacroState.OPPOSITE_STRUCTURE_SHIFT,
                S05MacroState.RR_INVALID,
            ):
                continue

            # Expiry check
            if narr.expiry_bar is not None and N > narr.expiry_bar:
                if narr.macro_state == S05MacroState.WAIT_LTF_CONFIRMATION:
                    narr.macro_state = S05MacroState.LTF_CONFIRMATION_EXPIRED
                    narr.terminal_reason = "ltf_confirmation_expired"
                else:
                    narr.macro_state = S05MacroState.ENTRY_EXPIRED
                    narr.terminal_reason = "entry_expired"
                continue

            # Check opposite structure shift in [narr.htf_ob_touch_bar, N]
            has_opposite_shift = any(
                narr.htf_ob_touch_bar <= ev.index <= N
                and ev.mode == self._config.mode
                and ev.event_type in ("BOS", "CHoCH", "MSS")
                and ev.direction != expected_ob_dir
                and getattr(ev, "break_type", "close") == "close"
                for ev in context.recent_structures
            )
            if has_opposite_shift:
                narr.macro_state = S05MacroState.OPPOSITE_STRUCTURE_SHIFT
                narr.terminal_reason = "opposite_structure_shift"
                continue

            # State A: WAIT_LTF_CONFIRMATION
            if narr.macro_state == S05MacroState.WAIT_LTF_CONFIRMATION:
                for ev in context.recent_structures:
                    if ev.mode != self._config.mode:
                        continue
                    if ev.direction != expected_ob_dir:
                        continue
                    matched_type = None
                    for allowed_t in self._config.ltf_confirmation_event_types:
                        if ev.event_type == allowed_t or (allowed_t == "CHoCH" and ev.event_type == "MSS") or (allowed_t == "MSS" and ev.event_type == "CHoCH"):
                            matched_type = allowed_t
                            break
                    if matched_type is None:
                        continue
                    if getattr(ev, "break_type", "close") != "close":
                        continue
                    # Appears strictly AFTER htf_ob_touch_bar
                    if ev.index <= narr.htf_ob_touch_bar:
                        continue
                    if ev.index > N:
                        continue
                    if getattr(ev, "confirmed_swing_at", ev.index) > N:
                        continue
                    if ev.structure_leg_id is None:
                        continue
                    htf_src_ev = getattr(narr.htf_ob, "source_event", None)
                    if htf_src_ev is not None:
                        src_idx = getattr(htf_src_ev, "index", getattr(htf_src_ev, "source_event_index", None))
                        if src_idx is not None and src_idx == ev.index:
                            continue
                    if self._config.require_displacement and not getattr(ev, "displacement", False):
                        continue

                    # Found valid confirmation
                    narr.confirmation = ev
                    narr.confirmation_id = f"{ev.event_type}_{ev.direction}_{ev.index}"
                    narr.confirmation_type = ev.event_type
                    narr.confirmation_bar = ev.index
                    narr.macro_state = S05MacroState.WAIT_LTF_ENTRY_ZONE
                    narr.expiry_bar = ev.index + self._config.ltf_zone_expiry_bars
                    break

            # State B: WAIT_LTF_ENTRY_ZONE
            if narr.macro_state == S05MacroState.WAIT_LTF_ENTRY_ZONE:
                assert narr.confirmation is not None
                valid_fvgs = []
                for fvg in context.active_fvgs:
                    if fvg.direction != expected_ob_dir:
                        continue
                    if fvg.mode != self._config.mode:
                        continue
                    if not getattr(fvg, "valid", True) or getattr(fvg, "filled", False):
                        continue
                    if expected_dir == "BUY" and context.close < fvg.bottom:
                        continue
                    if expected_dir == "SELL" and context.close > fvg.top:
                        continue
                    fvg_created = getattr(fvg, "created_at", getattr(fvg, "index", 0))
                    if fvg_created > N:
                        continue
                    same_leg = (fvg.structure_leg_id and narr.confirmation.structure_leg_id and fvg.structure_leg_id == narr.confirmation.structure_leg_id)
                    linked_source = (getattr(fvg, "source_event_index", None) == narr.confirmation.index or fvg.index == narr.confirmation.index)
                    after_swing = (fvg.index >= narr.confirmation.broken_swing_index)
                    if not (same_leg or linked_source or after_swing):
                        continue
                    valid_fvgs.append(fvg)

                valid_obs = []
                for ob in context.active_obs:
                    if ob.direction != expected_ob_dir:
                        continue
                    if ob.mode != self._config.mode:
                        continue
                    if not getattr(ob, "valid", True) or getattr(ob, "invalidated_at", None) is not None:
                        continue
                    if expected_dir == "BUY" and context.close < ob.low:
                        continue
                    if expected_dir == "SELL" and context.close > ob.high:
                        continue
                    ob_created = getattr(ob, "created_at", getattr(ob, "index", 0))
                    if ob_created > N:
                        continue
                    same_leg = (ob.structure_leg_id and narr.confirmation.structure_leg_id and ob.structure_leg_id == narr.confirmation.structure_leg_id)
                    linked_source = (getattr(ob, "source_event_index", None) == narr.confirmation.index)
                    after_swing = (ob.index >= narr.confirmation.broken_swing_index)
                    if not (same_leg or linked_source or after_swing):
                        continue
                    valid_obs.append(ob)

                candidate_zones: list[tuple[Any, str]] = []
                if self._config.ltf_entry_zone == "fvg":
                    candidate_zones = [(f, "fvg") for f in valid_fvgs]
                elif self._config.ltf_entry_zone == "ob":
                    candidate_zones = [(o, "ob") for o in valid_obs]
                elif self._config.ltf_entry_zone == "confluence":
                    if not valid_fvgs or not valid_obs:
                        continue
                    candidate_zones = [(f, "fvg") for f in valid_fvgs] + [(o, "ob") for o in valid_obs]
                else:  # "either"
                    candidate_zones = [(f, "fvg") for f in valid_fvgs] + [(o, "ob") for o in valid_obs]

                if not candidate_zones:
                    continue

                def zone_rank_key(item):
                    z, z_type = item
                    z_leg = getattr(z, "structure_leg_id", None)
                    conf_leg = narr.confirmation.structure_leg_id
                    same_leg_rank = 0 if (z_leg and conf_leg and z_leg == conf_leg) else 1

                    if z_type == "ob":
                        direct_link = 0 if getattr(z, "source_event_index", None) == narr.confirmation.index else 1
                    else:
                        fvg_src = getattr(z, "source_event_index", None)
                        direct_link = 0 if (fvg_src == narr.confirmation.index or z.index == narr.confirmation.index) else 1

                    z_idx = getattr(z, "created_at", getattr(z, "index", 0))
                    if z_idx == -1:
                        z_idx = getattr(z, "index", 0)
                    newer_rank = -int(z_idx)

                    z_top = getattr(z, "top", getattr(z, "high", 0.0))
                    z_bot = getattr(z, "bottom", getattr(z, "low", 0.0))
                    z_entry = z_top if expected_dir == "BUY" else z_bot
                    dist_rank = abs(z_entry - context.close)

                    z_id = getattr(z, "zone_id", f"{z_type}_{getattr(z, 'index', 0)}")
                    return (same_leg_rank, direct_link, newer_rank, dist_rank, str(z_id))

                sorted_zones = sorted(candidate_zones, key=zone_rank_key)
                w_zone, w_type = sorted_zones[0]
                narr.entry_zone = w_zone
                narr.entry_zone_type = w_type
                narr.entry_zone_id = getattr(w_zone, "zone_id", f"{w_type}_{getattr(w_zone, 'index', 0)}")
                if self._config.ltf_entry_zone == "confluence":
                    narr.entry_zone_type = "confluence"
                    narr.confluence_zone = valid_obs[0] if w_type == "fvg" else valid_fvgs[0]
                narr.macro_state = S05MacroState.WAIT_LTF_RETEST

            # State C: WAIT_LTF_RETEST
            if narr.macro_state == S05MacroState.WAIT_LTF_RETEST:
                assert narr.entry_zone is not None
                z_top = getattr(narr.entry_zone, "top", getattr(narr.entry_zone, "high", 0.0))
                z_bot = getattr(narr.entry_zone, "bottom", getattr(narr.entry_zone, "low", 0.0))

                if expected_dir == "BUY" and context.close < z_bot:
                    narr.macro_state = S05MacroState.LTF_ZONE_INVALIDATED
                    narr.terminal_reason = "zone_invalidated_by_close"
                    continue
                if expected_dir == "SELL" and context.close > z_top:
                    narr.macro_state = S05MacroState.LTF_ZONE_INVALIDATED
                    narr.terminal_reason = "zone_invalidated_by_close"
                    continue

                if N <= narr.confirmation_bar:
                    continue

                overlaps = (context.low <= z_top and context.high >= z_bot)
                if overlaps:
                    narr.retest_bar = N
                    narr.macro_state = S05MacroState.S05_READY
                    ready_proposals.append(narr)

        # 6. Build CandidateSetups
        candidates_for_bar: list[CandidateSetup] = []
        for narr in ready_proposals:
            cand = self._build_confirmation_candidate(narr, context)
            if cand is not None:
                if cand.evidence_cluster_id not in working_emitted_clusters:
                    candidates_for_bar.append(cand)
                    working_emitted_clusters[cand.evidence_cluster_id] = N + self._config.ltf_zone_expiry_bars

        # 7. Update macro state
        invalid_narrative = next(
            (n for n in working_narratives.values() if n.macro_state in (
                S05MacroState.HTF_OB_INVALIDATED,
                S05MacroState.LTF_CONFIRMATION_EXPIRED,
                S05MacroState.LTF_ZONE_INVALIDATED,
                S05MacroState.ENTRY_EXPIRED,
                S05MacroState.OPPOSITE_STRUCTURE_SHIFT,
                S05MacroState.RR_INVALID,
            )),
            None,
        )

        if candidates_for_bar:
            self._macro_state = S05MacroState.S05_READY
        elif any(n.macro_state == S05MacroState.WAIT_LTF_RETEST for n in working_narratives.values()):
            self._macro_state = S05MacroState.WAIT_LTF_RETEST
        elif any(n.macro_state == S05MacroState.WAIT_LTF_ENTRY_ZONE for n in working_narratives.values()):
            self._macro_state = S05MacroState.WAIT_LTF_ENTRY_ZONE
        elif any(n.macro_state == S05MacroState.WAIT_LTF_CONFIRMATION for n in working_narratives.values()):
            self._macro_state = S05MacroState.WAIT_LTF_CONFIRMATION
        elif invalid_narrative is not None:
            self._macro_state = invalid_narrative.macro_state
            self._last_rejection_reason = invalid_narrative.terminal_reason
        elif has_touched_ob:
            self._macro_state = S05MacroState.WAIT_LTF_CONFIRMATION
        elif eligible_htf_obs:
            self._macro_state = S05MacroState.WAIT_HTF_OB_RETEST
        else:
            self._macro_state = S05MacroState.WAIT_HTF_OB

        sorted_candidates = tuple(
            sorted(
                candidates_for_bar,
                key=lambda c: (c.direction, c.evidence_cluster_id, c.setup_id),
            )
        )

        self._confirmation_narratives = {
            k: v for k, v in working_narratives.items()
            if v.macro_state in (
                S05MacroState.WAIT_LTF_CONFIRMATION,
                S05MacroState.WAIT_LTF_ENTRY_ZONE,
                S05MacroState.WAIT_LTF_RETEST,
            )
        }
        self._emitted_clusters = working_emitted_clusters
        self._last_bar_index = N
        self._last_timestamp = context.timestamp
        self._last_context_payload = context.to_dict()
        self._last_result = sorted_candidates

        return sorted_candidates

    def _build_confirmation_candidate(
        self,
        narrative: S05ConfirmationNarrative,
        context: StrategyContext,
    ) -> Optional[CandidateSetup]:
        N = context.bar_index
        expected_dir = narrative.direction
        expected_ob_dir = "bullish" if expected_dir == "BUY" else "bearish"
        assert narrative.entry_zone is not None
        assert narrative.confirmation is not None

        z_top = getattr(narrative.entry_zone, "top", getattr(narrative.entry_zone, "high", 0.0))
        z_bot = getattr(narrative.entry_zone, "bottom", getattr(narrative.entry_zone, "low", 0.0))

        if self._config.entry_level == "proximal":
            entry_price = z_top if expected_dir == "BUY" else z_bot
        else:  # ce_50
            entry_price = (z_top + z_bot) / 2.0

        distal = z_bot if expected_dir == "BUY" else z_top
        if self._config.sl_anchor == "ltf_ob":
            match_ob = next((o for o in getattr(context, "active_obs", ()) if o.direction == expected_ob_dir), None)
            if match_ob is not None:
                distal = match_ob.low if expected_dir == "BUY" else match_ob.high
        elif self._config.sl_anchor == "ltf_fvg":
            match_fvg = next((f for f in getattr(context, "active_fvgs", ()) if f.direction == expected_ob_dir), None)
            if match_fvg is not None:
                distal = match_fvg.bottom if expected_dir == "BUY" else match_fvg.top

        if expected_dir == "BUY":
            stop_loss = distal - self._config.sl_buffer_price
        else:
            stop_loss = distal + self._config.sl_buffer_price

        entry_price = round(entry_price, 3)
        stop_loss = round(stop_loss, 3)

        risk = (entry_price - stop_loss) if expected_dir == "BUY" else (stop_loss - entry_price)
        if risk <= 0.0:
            narrative.macro_state = S05MacroState.RR_INVALID
            narrative.terminal_reason = "risk_non_positive"
            return None

        target_pool: Optional[LiquidityPoolSnapshot] = None
        target_type = "fixed_rr"
        take_profit: Optional[float] = None

        eligible_pools = []
        for p in context.active_pools:
            if not getattr(p, "valid", True) or getattr(p, "swept", False):
                continue
            if p.mode != self._config.mode:
                continue
            if getattr(p, "confirmed_at", 0) > N:
                continue
            if expected_dir == "BUY":
                if p.kind in ("equal_highs", "swing_high") and p.price > entry_price:
                    eligible_pools.append(p)
            else:
                if p.kind in ("equal_lows", "swing_low") and p.price < entry_price:
                    eligible_pools.append(p)

        if eligible_pools:
            def pool_sort_key(p):
                dist = abs(p.price - entry_price)
                indices_sorted = tuple(sorted(p.indices))
                return (dist, p.confirmed_at, p.kind, indices_sorted)

            sorted_pools = sorted(eligible_pools, key=pool_sort_key)
            nearest_pool = sorted_pools[0]
            cand_tp = round(nearest_pool.price, 3)
            cand_rew = (cand_tp - entry_price) if expected_dir == "BUY" else (entry_price - cand_tp)
            if cand_rew > 0:
                cand_rr = round(cand_rew / risk, 2)
                if cand_rr >= self._config.min_rr:
                    target_pool = nearest_pool
                    target_type = "opposing_pool"
                    take_profit = cand_tp

        if take_profit is None:
            target_type = "fixed_rr"
            if expected_dir == "BUY":
                take_profit = round(entry_price + self._config.fallback_rr * risk, 3)
            else:
                take_profit = round(entry_price - self._config.fallback_rr * risk, 3)

        reward = (take_profit - entry_price) if expected_dir == "BUY" else (entry_price - take_profit)
        if reward <= 0.0:
            narrative.macro_state = S05MacroState.RR_INVALID
            narrative.terminal_reason = "reward_non_positive"
            return None

        planned_rr = round(reward / risk, 2)
        if planned_rr < self._config.min_rr:
            narrative.macro_state = S05MacroState.RR_INVALID
            narrative.terminal_reason = "planned_rr_below_minimum"
            return None

        if expected_dir == "BUY" and not (stop_loss < entry_price < take_profit):
            return None
        if expected_dir == "SELL" and not (take_profit < entry_price < stop_loss):
            return None

        # 1. HTF OB Evidence
        ob_top_val = getattr(narrative.htf_ob, "top", getattr(narrative.htf_ob, "high", 0.0))
        ob_bot_val = getattr(narrative.htf_ob, "bottom", getattr(narrative.htf_ob, "low", 0.0))
        htf_kind = "order_block" if isinstance(narrative.htf_ob, (OrderBlockSnapshot, OrderBlock)) else "htf_poi"
        ev_htf_id = make_evidence_id(htf_kind, self._config.mode, narrative.htf_ob_touch_bar, f"htf_{narrative.htf_ob_id}")
        ev_htf = EvidenceRef(
            evidence_id=ev_htf_id,
            kind=htf_kind,
            bar_index=narrative.htf_ob_touch_bar,
            price=ob_top_val if expected_dir == "BUY" else ob_bot_val,
            details={
                "htf_ob_id": str(narrative.htf_ob_id),
                "touch_bar": narrative.htf_ob_touch_bar,
                "top": ob_top_val,
                "bottom": ob_bot_val,
                "direction": expected_ob_dir,
            },
        )

        # 2. LTF Confirmation Evidence
        conf = narrative.confirmation
        ev_conf_id = make_evidence_id("structure_event", self._config.mode, conf.index, f"conf_{conf.direction}_{conf.broken_swing_index}")
        ev_conf = EvidenceRef(
            evidence_id=ev_conf_id,
            kind="structure_event",
            bar_index=conf.index,
            price=conf.broken_swing_price,
            time=getattr(conf, "time", None),
            details={
                "direction": conf.direction,
                "event_type": conf.event_type,
                "broken_swing_index": conf.broken_swing_index,
                "displacement": conf.displacement,
                "structure_leg_id": conf.structure_leg_id,
            },
        )

        # 3. LTF Zone Evidence
        z_is_fvg = isinstance(narrative.entry_zone, (FairValueGapSnapshot, FairValueGap))
        z_kind = "fair_value_gap" if z_is_fvg else "order_block"
        z_idx = getattr(narrative.entry_zone, "created_at", getattr(narrative.entry_zone, "index", 0))
        if z_idx == -1:
            z_idx = getattr(narrative.entry_zone, "index", 0)
        ev_zone_id = make_evidence_id(z_kind, self._config.mode, int(z_idx), f"zone_{narrative.entry_zone_id}")
        ev_zone = EvidenceRef(
            evidence_id=ev_zone_id,
            kind=z_kind,
            bar_index=int(z_idx),
            price=z_top if expected_dir == "BUY" else z_bot,
            details={
                "entry_zone_type": narrative.entry_zone_type,
                "entry_zone_id": narrative.entry_zone_id,
                "top": z_top,
                "bottom": z_bot,
                "direction": expected_ob_dir,
            },
        )

        evidences_list = [ev_htf, ev_conf, ev_zone]
        if narrative.confluence_zone is not None:
            c_is_fvg = isinstance(narrative.confluence_zone, (FairValueGapSnapshot, FairValueGap))
            c_kind = "fair_value_gap" if c_is_fvg else "order_block"
            c_idx = getattr(narrative.confluence_zone, "created_at", getattr(narrative.confluence_zone, "index", 0))
            if c_idx == -1:
                c_idx = getattr(narrative.confluence_zone, "index", 0)
            c_top = getattr(narrative.confluence_zone, "top", getattr(narrative.confluence_zone, "high", 0.0))
            c_bot = getattr(narrative.confluence_zone, "bottom", getattr(narrative.confluence_zone, "low", 0.0))
            c_id = getattr(narrative.confluence_zone, "zone_id", f"{c_kind}_{c_idx}")
            ev_conf_zone_id = make_evidence_id(c_kind, self._config.mode, int(c_idx), f"confluence_{c_id}")
            ev_conf_zone = EvidenceRef(
                evidence_id=ev_conf_zone_id,
                kind=c_kind,
                bar_index=int(c_idx),
                price=c_top if expected_dir == "BUY" else c_bot,
                details={
                    "confluence_zone_id": c_id,
                    "top": c_top,
                    "bottom": c_bot,
                },
            )
            evidences_list.append(ev_conf_zone)

        if target_pool is not None:
            pool_indices_token = "_".join(str(i) for i in sorted(target_pool.indices))
            pool_sub_key = f"target_pool_{target_pool.kind}_{pool_indices_token}"
            ev_pool_id = make_evidence_id("liquidity_pool", self._config.mode, target_pool.confirmed_at, pool_sub_key)
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
            evidences_list.append(ev_pool)

        cluster_id = make_cluster_id(expected_dir, f"conf-{conf.index}", f"zone-{narrative.entry_zone_id}")
        setup_id = make_setup_id(self.strategy_id, expected_dir, N, cluster_id)

        metadata: dict[str, Any] = {
            "htf_bias": context.htf_bias.bias if context.htf_bias else None,
            "htf_ob_id": str(narrative.htf_ob_id),
            "htf_ob_touch_bar": int(narrative.htf_ob_touch_bar),
            "ltf_confirmation_id": str(narrative.confirmation_id),
            "ltf_confirmation_type": str(narrative.confirmation_type),
            "ltf_entry_zone_type": str(narrative.entry_zone_type),
            "ltf_entry_zone_id": str(narrative.entry_zone_id),
            "ltf_zone_touch_bar": int(narrative.retest_bar) if narrative.retest_bar is not None else N,
            "trade_direction": str(expected_dir),
            "signal_bar": N,
            "available_from_bar": N + 1,
            "flow_type": "ltf_confirmation",
            "require_ltf_confirmation": True,
            "entry_level": self._config.entry_level,
            "stop_anchor": self._config.sl_anchor,
            "sl_buffer_price": self._config.sl_buffer_price,
            "target_source": target_type,
            "target_pool_key": (
                [target_pool.mode, target_pool.kind, list(sorted(target_pool.indices))]
                if target_pool
                else None
            ),
            "fixed_rr": (
                self._config.fallback_rr if target_type == "fixed_rr" else None
            ),
            "rr_basis": "structural_pre_fill",
        }

        return CandidateSetup(
            setup_id=setup_id,
            strategy_id=self.strategy_id,
            direction=expected_dir,
            bar_index=N,
            timestamp=context.timestamp,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            planned_rr=planned_rr,
            evidences=tuple(evidences_list),
            evidence_cluster_id=cluster_id,
            expiry_bar=N + self._config.ltf_zone_expiry_bars,
            target_type=target_type,
            meta=metadata,
        )

    def _assert_zero_future_leak(self, context: StrategyContext) -> None:
        """Defensive guard ensuring StrategyContext contains zero future-leaking evidence."""
        N = context.bar_index
        for m in context.recent_structures:
            if m.index > N or m.confirmed_swing_at > N:
                raise StrategyStateError(
                    f"Future leak in structure event: index={m.index}, confirmed_swing_at={m.confirmed_swing_at} > bar_index={N}."
                )
        for ob in context.active_obs:
            eff_created = ob.created_at if ob.created_at != -1 else ob.source_event_index
            if eff_created > N:
                raise StrategyStateError(
                    f"Future leak in order block: effective_created_at={eff_created} > bar_index={N}."
                )
            if ob.mitigated_at is not None and ob.mitigated_at > N:
                raise StrategyStateError(
                    f"Future leak in order block: mitigated_at={ob.mitigated_at} > bar_index={N}."
                )
            if ob.invalidated_at is not None and ob.invalidated_at > N:
                raise StrategyStateError(
                    f"Future leak in order block: invalidated_at={ob.invalidated_at} > bar_index={N}."
                )
        for p in context.active_pools:
            if p.confirmed_at > N or (p.swept_at is not None and p.swept_at > N) or (p.invalidated_at is not None and p.invalidated_at > N):
                raise StrategyStateError(
                    f"Future leak in pool: confirmed_at={p.confirmed_at} > bar_index={N}."
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


__all__ = [
    "S05Config",
    "S05BOSOBRetestStrategy",
    "S05NarrativeStage",
    "S05MacroState",
    "S05Narrative",
    "S05ConfirmationNarrative",
    "BOSKey",
    "OBKey",
]
