"""
smc/engine/strategies/s09_ict_silver_bullet.py
================================================
S09: ICT Silver Bullet Strategy Template.

Implements the time-window qualified Smart Money Concepts strategy:
    Time Window Open (London Open 03:00-04:00, NY AM 10:00-11:00, NY PM 14:00-15:00 NY time)
    -> Liquidity Sweep confirmed inside Window
    -> Market Structure Shift (MSS/CHoCH/BOS) confirmed inside same Window
    -> Displacement FVG linked to same structure leg confirmed inside same Window
    -> Retest of FVG occurring after MSS up to Window End + 15 min Grace period
    -> CandidateSetup emitted at closed bar N (max 1 setup per Window)

Adheres strictly to ADR 16, ADR 20, and ADR 22.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, fields
import datetime
from enum import Enum
import math
from typing import Any, Mapping, Optional, Sequence
import zoneinfo

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


# =============================================================================
# Canonical Window Constants
# =============================================================================

NEW_YORK_TZ_STR = "America/New_York"
NEW_YORK_TZ = zoneinfo.ZoneInfo(NEW_YORK_TZ_STR)

CANONICAL_WINDOWS: tuple[str, ...] = (
    "silver_bullet_london",
    "silver_bullet_ny_am",
    "silver_bullet_ny_pm",
)

WINDOW_SCHEDULE: dict[str, tuple[datetime.time, datetime.time]] = {
    "silver_bullet_london": (datetime.time(3, 0), datetime.time(4, 0)),
    "silver_bullet_ny_am": (datetime.time(10, 0), datetime.time(11, 0)),
    "silver_bullet_ny_pm": (datetime.time(14, 0), datetime.time(15, 0)),
}

WindowKey = tuple[str, str, str]
# Format: (window_name, local_date_iso, utc_start_iso)


# =============================================================================
# Time & Window Bounds Helpers
# =============================================================================

def get_ny_local_date_str(ts: pd.Timestamp) -> str:
    """Returns local calendar date in America/New_York formatted as YYYY-MM-DD."""
    if ts.tzinfo is None:
        raise StrategyValidationError("Timestamp must be timezone-aware.")
    ny_dt = ts.tz_convert(NEW_YORK_TZ)
    return ny_dt.strftime("%Y-%m-%d")


def compute_window_bounds_for_date(
    local_date_str: str,
    window_name: str,
    grace_minutes: int = 15,
) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    """
    Computes (window_start_utc, window_end_utc, grace_expiry_utc) for a given
    local NY calendar date and window name.
    """
    if window_name not in WINDOW_SCHEDULE:
        raise StrategyValidationError(f"Unknown window name: {window_name}")

    start_time, end_time = WINDOW_SCHEDULE[window_name]
    y, m, d = (int(part) for part in local_date_str.split("-"))

    start_ny = datetime.datetime(y, m, d, start_time.hour, start_time.minute, tzinfo=NEW_YORK_TZ)
    end_ny = datetime.datetime(y, m, d, end_time.hour, end_time.minute, tzinfo=NEW_YORK_TZ)

    start_utc = pd.Timestamp(start_ny).tz_convert("UTC")
    end_utc = pd.Timestamp(end_ny).tz_convert("UTC")
    grace_utc = end_utc + pd.Timedelta(minutes=grace_minutes)

    return start_utc, end_utc, grace_utc


def get_window_for_close_time(
    close_time: pd.Timestamp,
    enabled_windows: tuple[str, ...],
    grace_minutes: int = 15,
) -> Optional[tuple[WindowKey, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """
    Finds the canonical window that contains confirmation close_time:
        start_utc <= close_time < end_utc
    Returns (WindowKey, start_utc, end_utc, grace_expiry_utc) or None.
    Only Monday through Friday in New York local time are eligible.
    """
    if close_time.tzinfo is None:
        raise StrategyValidationError("close_time must be timezone-aware.")

    close_ny = close_time.tz_convert(NEW_YORK_TZ)
    # Check weekday: 0=Monday, ..., 4=Friday. 5=Saturday, 6=Sunday.
    if close_ny.weekday() >= 5:
        return None

    local_date_str = close_ny.strftime("%Y-%m-%d")

    for w_name in enabled_windows:
        if w_name not in WINDOW_SCHEDULE:
            continue
        start_utc, end_utc, grace_utc = compute_window_bounds_for_date(
            local_date_str, w_name, grace_minutes
        )
        if start_utc <= close_time < end_utc:
            key: WindowKey = (w_name, local_date_str, start_utc.isoformat())
            return key, start_utc, end_utc, grace_utc

    return None


# =============================================================================
# S09 Configuration
# =============================================================================

@dataclass(frozen=True)
class S09Config:
    """
    Immutable configuration for S09 ICT Silver Bullet Strategy.

    Attributes:
        mode: Detection mode ('internal' or 'swing'). Default 'internal'.
        enabled_windows: Tuple of canonical window names. Default all 3.
        grace_minutes: Grace period in minutes after window end. Must be 15 in Wave 1.
        fvg_to_mss_max_bars: Max bar lag between FVG candle and MSS confirmation. Default 10.
        entry_level: Entry price level ('proximal' or 'ce_50'). Default 'proximal'.
        sl_buffer_price: Price buffer added/subtracted beyond sweep extreme. Default 0.20.
        min_rr: Minimum acceptable planned Reward-to-Risk ratio. Default 1.50.
        fallback_rr: Fallback RR multiplier when no pool available. Default 2.00.
        require_displacement: Whether MSS break must have displacement. Default True.
    """
    mode: str = "internal"
    enabled_windows: tuple[str, ...] = (
        "silver_bullet_london",
        "silver_bullet_ny_am",
        "silver_bullet_ny_pm",
    )
    grace_minutes: int = 15
    fvg_to_mss_max_bars: int = 10
    entry_level: str = "proximal"
    sl_buffer_price: float = 0.20
    min_rr: float = 1.50
    fallback_rr: float = 2.00
    require_displacement: bool = True
    # Temporary Wave 1 compatibility switch. The canonical Silver Bullet
    # window logic remains intact and can be re-enabled later.
    use_time_filter: bool = True

    def __post_init__(self) -> None:
        # 1. mode validation
        if not isinstance(self.mode, str) or self.mode not in {"internal", "swing"}:
            raise StrategyValidationError(
                f"Invalid mode '{self.mode}'. Must be 'internal' or 'swing'."
            )

        # 2. enabled_windows validation
        if not isinstance(self.enabled_windows, (tuple, list)):
            raise StrategyValidationError(
                f"enabled_windows must be a sequence of strings, got {type(self.enabled_windows).__name__}."
            )
        if not self.enabled_windows:
            raise StrategyValidationError("enabled_windows cannot be empty.")

        seen_w = set()
        for w in self.enabled_windows:
            if not isinstance(w, str) or w not in CANONICAL_WINDOWS:
                raise StrategyValidationError(
                    f"Unknown window '{w}'. Allowed: {list(CANONICAL_WINDOWS)}."
                )
            if w in seen_w:
                raise StrategyValidationError(f"Duplicate window '{w}' in enabled_windows.")
            seen_w.add(w)

        # Normalize in canonical schedule order
        normalized_windows = tuple(w for w in CANONICAL_WINDOWS if w in seen_w)
        object.__setattr__(self, "enabled_windows", normalized_windows)

        # 3. grace_minutes validation (strictly 15 in Wave 1)
        if isinstance(self.grace_minutes, bool) or not isinstance(self.grace_minutes, int):
            raise StrategyValidationError(
                f"grace_minutes must be an int, got {type(self.grace_minutes).__name__}."
            )
        if self.grace_minutes != 15:
            raise StrategyValidationError(
                f"grace_minutes must be exactly 15 in Wave 1 (ADR 22), got {self.grace_minutes}."
            )

        # 4. fvg_to_mss_max_bars
        if isinstance(self.fvg_to_mss_max_bars, bool) or not isinstance(self.fvg_to_mss_max_bars, int):
            raise StrategyValidationError(
                f"fvg_to_mss_max_bars must be an int, got {type(self.fvg_to_mss_max_bars).__name__}."
            )
        if self.fvg_to_mss_max_bars <= 0:
            raise StrategyValidationError(
                f"fvg_to_mss_max_bars must be > 0, got {self.fvg_to_mss_max_bars}."
            )

        # 5. entry_level
        if not isinstance(self.entry_level, str) or self.entry_level not in {"proximal", "ce_50"}:
            raise StrategyValidationError(
                f"Invalid entry_level '{self.entry_level}'. Must be 'proximal' or 'ce_50'."
            )

        # 6. sl_buffer_price
        if isinstance(self.sl_buffer_price, bool) or not isinstance(self.sl_buffer_price, (int, float)):
            raise StrategyValidationError(
                f"sl_buffer_price must be a float, got {type(self.sl_buffer_price).__name__}."
            )
        sl_buf = float(self.sl_buffer_price)
        if not math.isfinite(sl_buf) or sl_buf <= 0.0:
            raise StrategyValidationError(
                f"sl_buffer_price must be finite and > 0, got {sl_buf}."
            )
        object.__setattr__(self, "sl_buffer_price", sl_buf)

        # 7. min_rr
        if isinstance(self.min_rr, bool) or not isinstance(self.min_rr, (int, float)):
            raise StrategyValidationError(
                f"min_rr must be a float, got {type(self.min_rr).__name__}."
            )
        m_rr = float(self.min_rr)
        if not math.isfinite(m_rr) or m_rr <= 0.0:
            raise StrategyValidationError(f"min_rr must be finite and > 0, got {m_rr}.")
        object.__setattr__(self, "min_rr", m_rr)

        # 8. fallback_rr
        if isinstance(self.fallback_rr, bool) or not isinstance(self.fallback_rr, (int, float)):
            raise StrategyValidationError(
                f"fallback_rr must be a float, got {type(self.fallback_rr).__name__}."
            )
        fb_rr = float(self.fallback_rr)
        if not math.isfinite(fb_rr) or fb_rr <= 0.0:
            raise StrategyValidationError(
                f"fallback_rr must be finite and > 0, got {fb_rr}."
            )
        if fb_rr < m_rr:
            raise StrategyValidationError(
                f"fallback_rr ({fb_rr}) cannot be less than min_rr ({m_rr})."
            )
        object.__setattr__(self, "fallback_rr", fb_rr)

        # 9. require_displacement
        if not isinstance(self.require_displacement, bool):
            raise StrategyValidationError(
                f"require_displacement must be a bool, got {type(self.require_displacement).__name__}."
            )

        # 10. time filter
        if not isinstance(self.use_time_filter, bool):
            raise StrategyValidationError(
                f"use_time_filter must be a bool, got {type(self.use_time_filter).__name__}."
            )

    def to_dict(self) -> dict[str, Any]:
        """Exact JSON-serializable dictionary representation."""
        return {
            "mode": self.mode,
            "enabled_windows": list(self.enabled_windows),
            "grace_minutes": int(self.grace_minutes),
            "fvg_to_mss_max_bars": int(self.fvg_to_mss_max_bars),
            "entry_level": self.entry_level,
            "sl_buffer_price": float(self.sl_buffer_price),
            "min_rr": float(self.min_rr),
            "fallback_rr": float(self.fallback_rr),
            "require_displacement": bool(self.require_displacement),
            "use_time_filter": bool(self.use_time_filter),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> S09Config:
        """Instantiate S09Config from dictionary with strict schema validation."""
        if not isinstance(data, dict):
            raise StrategyValidationError(
                f"Expected dict for S09Config, got {type(data).__name__}."
            )
        allowed_fields = {f.name for f in fields(cls)}
        unknown = set(data.keys()) - allowed_fields
        if unknown:
            raise StrategyValidationError(f"Unknown fields for S09Config: {sorted(unknown)}")

        parsed_data: dict[str, Any] = dict(data)
        if "enabled_windows" in parsed_data and isinstance(parsed_data["enabled_windows"], list):
            parsed_data["enabled_windows"] = tuple(parsed_data["enabled_windows"])

        try:
            return cls(**parsed_data)
        except (ValueError, TypeError) as exc:
            raise StrategyValidationError(f"Failed to instantiate S09Config: {exc}") from exc


# =============================================================================
# State Machine Models
# =============================================================================

class S09NarrativeStage(str, Enum):
    SWEEP_SEEN = "SWEEP_SEEN"
    FVG_READY = "FVG_READY"
    EMITTED = "EMITTED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


SweepKey = tuple[str, str, int, tuple[int, ...]]
# (direction, pool_kind, sweep_index, sorted_pool_indices)


@dataclass
class S09Narrative:
    """Working narrative representing an evolving Silver Bullet setup."""
    window_key: WindowKey
    window_start_utc: pd.Timestamp
    window_end_utc: pd.Timestamp
    grace_expiry_utc: pd.Timestamp
    sweep_key: SweepKey
    direction: str  # "BUY" or "SELL"
    sweep: LiquiditySweepSnapshot
    stage: S09NarrativeStage
    created_bar: int
    mss: Optional[StructureEventSnapshot] = None
    fvg: Optional[FairValueGapSnapshot] = None
    ready_at: Optional[int] = None
    fvg_filled_internally: bool = False
    fvg_filled_bar: Optional[int] = None
    terminal_reason: Optional[str] = None


# =============================================================================
# Retention Calculation Helper
# =============================================================================

def compute_bar_close_retention_bar(
    current_bar: int,
    config: S09Config,
    active_narratives: Optional[Mapping[Any, S09Narrative]] = None,
) -> int:
    """
    Computes the minimum bar index whose close time must be retained in _bar_close_times.

    Invariants:
    1. An FVG can be paired with an MSS occurring at current_bar N if:
       (N - fvg.index) <= config.fvg_to_mss_max_bars.
       Since fvg.confirmed_at >= fvg.index, fvg.confirmed_at can be as early as:
       N - config.fvg_to_mss_max_bars.
       To guarantee safe headroom for confirmation lag and window matching,
       we look back at least config.fvg_to_mss_max_bars + 10 bars.
    2. If active narratives are in SWEEP_SEEN stage awaiting MSS, we retain bar close times
       covering the earliest created narrative within its active window/grace period.
    3. The retention bound is strictly deterministic and bounded by config and active window grace,
       ensuring O(1) memory over arbitrarily long streams.
    """
    lookback = config.fvg_to_mss_max_bars + 10
    min_bar = current_bar - lookback

    if active_narratives:
        for narr in active_narratives.values():
            if narr.stage == S09NarrativeStage.SWEEP_SEEN:
                sweep_bar = narr.sweep.index if narr.sweep is not None else narr.created_bar
                candidate_min = min(narr.created_bar, sweep_bar)
                if candidate_min < min_bar:
                    min_bar = candidate_min

    return max(0, min_bar)


# =============================================================================
# Strategy Implementation
# =============================================================================

class S09ICTSilverBulletStrategy:
    """
    S09: ICT Silver Bullet Strategy Template.

    Implements StrategyTemplate protocol:
        - strategy_id: str = 'S09'
        - profile: StrategyProfile
        - evaluate(context: StrategyContext) -> tuple[CandidateSetup, ...]
        - reset() -> None
    """

    strategy_id: str = "S09"

    def __init__(self, config: Optional[S09Config] = None) -> None:
        if config is None:
            self._config = S09Config()
        elif not isinstance(config, S09Config):
            raise StrategyValidationError(
                f"Expected S09Config or None, got {type(config).__name__}."
            )
        else:
            self._config = config

        validate_strategy_id(self.strategy_id)

        self.profile: StrategyProfile = StrategyProfile(
            strategy_id=self.strategy_id,
            name="ICT Silver Bullet",
            version="1.0.0",
            style="time_based",
            allowed_directions=("BUY", "SELL"),
            timeframes=("M1", "M5", "M15"),
            max_setup_age_bars=15,
            cooldown_bars=3,
            min_rr=self._config.min_rr,
            params=self._config.to_dict(),
        )

        # Persistent state
        self._narratives: dict[tuple[WindowKey, SweepKey], S09Narrative] = {}
        self._consumed_windows: dict[WindowKey, pd.Timestamp] = {}  # window_key -> grace_expiry_utc
        self._emitted_clusters: dict[str, pd.Timestamp] = {}  # cluster_id -> grace_expiry_utc
        self._bar_close_times: dict[int, pd.Timestamp] = {}  # bar_index -> bar_close_time

        # Monotonic and idempotency tracking
        self._last_bar_index: Optional[int] = None
        self._last_timestamp: Optional[pd.Timestamp] = None
        self._last_bar_close_time: Optional[pd.Timestamp] = None
        self._last_context_payload: Optional[dict[str, Any]] = None
        self._last_result: tuple[CandidateSetup, ...] = ()

    @property
    def config(self) -> S09Config:
        return self._config

    def reset(self) -> None:
        """Resets all internal narrative and tracking state to initial conditions."""
        self._narratives.clear()
        self._consumed_windows.clear()
        self._emitted_clusters.clear()
        self._bar_close_times.clear()
        self._last_bar_index = None
        self._last_timestamp = None
        self._last_bar_close_time = None
        self._last_context_payload = None
        self._last_result = ()

    def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]:
        """
        Evaluates the StrategyContext as-of closed bar N.

        Returns deterministic tuple of CandidateSetup instances emitted at bar N.
        """
        # 1. Preflight context validation
        if not isinstance(context, StrategyContext):
            raise StrategyValidationError(
                f"Expected StrategyContext, got {type(context).__name__}."
            )

        if context.timestamp.tzinfo is None or context.bar_close_time.tzinfo is None:
            raise StrategyValidationError(
                "StrategyContext timestamps must be timezone-aware."
            )

        if context.bar_close_time < context.timestamp:
            raise StrategyValidationError(
                f"bar_close_time ({context.bar_close_time}) cannot be before timestamp ({context.timestamp})."
            )

        N = context.bar_index
        current_ts = context.timestamp
        current_bct = context.bar_close_time

        # 2. Monotonic advance & Idempotent retry guard
        if self._last_bar_index is not None:
            if N == self._last_bar_index:
                # Same-bar evaluation: check idempotency
                current_payload = context.to_dict()
                if current_payload == self._last_context_payload:
                    return self._last_result
                raise StrategyStateError(
                    f"Conflicting evaluation payload for already evaluated bar {N}."
                )

            if N < self._last_bar_index or N != self._last_bar_index + 1:
                raise StrategyStateError(
                    f"Non-monotonic bar sequence: expected bar {self._last_bar_index + 1}, got {N}."
                )

            if current_ts <= self._last_timestamp or current_bct <= self._last_bar_close_time:
                raise StrategyStateError(
                    f"Non-increasing timestamps at bar {N}: ts={current_ts}, bct={current_bct}."
                )

        # 3. Defensive zero-future-leak check
        self._assert_zero_future_leak(context)

        # 4. Create working copies for atomic state mutation
        working_narratives = copy.deepcopy(self._narratives)
        working_consumed_windows = dict(self._consumed_windows)
        working_emitted_clusters = dict(self._emitted_clusters)
        working_bar_close_times = dict(self._bar_close_times)

        # Record current bar's close time
        working_bar_close_times[N] = current_bct

        # Prune consumed windows and emitted clusters whose grace period has expired
        working_consumed_windows = {
            w_key: grace_exp
            for w_key, grace_exp in working_consumed_windows.items()
            if current_bct <= grace_exp
        }
        working_emitted_clusters = {
            c_id: grace_exp
            for c_id, grace_exp in working_emitted_clusters.items()
            if current_bct <= grace_exp
        }

        # Bounded pruning for working_bar_close_times:
        min_needed_bar = compute_bar_close_retention_bar(
            current_bar=N,
            config=self._config,
            active_narratives=working_narratives,
        )
        working_bar_close_times = {
            b_idx: b_time
            for b_idx, b_time in working_bar_close_times.items()
            if b_idx >= min_needed_bar
        }

        # 5. Check if bar N confirmation falls inside an enabled window.
        # The canonical time-filtered path is intentionally preserved. The
        # temporary unrestricted path only removes session-hour gating and
        # assigns each incoming bar its own broad, non-time-gated window.
        if self._config.use_time_filter:
            current_window_info = get_window_for_close_time(
                current_bct,
                self._config.enabled_windows,
                self._config.grace_minutes,
            )
        else:
            # Use the current New York calendar day as a schema-compatible
            # all-day bucket. This removes the intraday Silver Bullet hours
            # while preserving the downstream window key/date contract.
            unrestricted_start = (
                current_bct.tz_convert(NEW_YORK_TZ)
                .normalize()
                .tz_convert("UTC")
            )
            unrestricted_end = unrestricted_start + pd.Timedelta(days=1)
            unrestricted_key: WindowKey = (
                # Keep the canonical window-name token for downstream
                # EligibilityGate schema compatibility; the flag above
                # disables all session-hour semantics.
                "silver_bullet_london",
                unrestricted_start.tz_convert(NEW_YORK_TZ).strftime("%Y-%m-%d"),
                unrestricted_start.isoformat(),
            )
            current_window_info = (
                unrestricted_key,
                unrestricted_start,
                unrestricted_end,
                unrestricted_end + pd.Timedelta(minutes=15),
            )

        # 6. Ingest new liquidity sweeps if inside window
        if current_window_info is not None:
            w_key, w_start, w_end, w_grace = current_window_info
            if w_key not in working_consumed_windows:
                self._ingest_sweeps(
                    context,
                    w_key,
                    w_start,
                    w_end,
                    w_grace,
                    working_narratives,
                )

        # 7. Progress active narratives
        candidates_by_window: dict[WindowKey, list[tuple[tuple, CandidateSetup, S09Narrative]]] = {}

        # Canonical sort of active narratives for deterministic evaluation order
        sorted_narrative_keys = sorted(
            working_narratives.keys(),
            key=lambda k: (k[0], k[1][2], k[1][0], k[1][1], k[1][3]),
        )

        for comp_key in sorted_narrative_keys:
            narrative = working_narratives[comp_key]
            if narrative.stage not in (S09NarrativeStage.SWEEP_SEEN, S09NarrativeStage.FVG_READY):
                continue

            # Check grace expiry
            if current_bct > narrative.grace_expiry_utc:
                narrative.stage = S09NarrativeStage.EXPIRED
                narrative.terminal_reason = "window_grace_expired"
                continue

            # Check if window already consumed
            if narrative.window_key in working_consumed_windows:
                narrative.stage = S09NarrativeStage.INVALIDATED
                narrative.terminal_reason = "window_already_consumed"
                continue

            # Check HTF bias
            if context.htf_bias is None:
                # Missing HTF bias fails closed
                narrative.stage = S09NarrativeStage.INVALIDATED
                narrative.terminal_reason = "missing_htf_bias"
                continue

            bias = context.htf_bias.bias
            if (narrative.direction == "BUY" and bias == "bearish") or (
                narrative.direction == "SELL" and bias == "bullish"
            ):
                narrative.stage = S09NarrativeStage.INVALIDATED
                narrative.terminal_reason = "htf_bias_mismatch"
                continue

            # Check sweep extreme close violation
            if N > narrative.sweep.index:
                if (narrative.direction == "BUY" and context.close < narrative.sweep.price_wick) or (
                    narrative.direction == "SELL" and context.close > narrative.sweep.price_wick
                ):
                    narrative.stage = S09NarrativeStage.INVALIDATED
                    narrative.terminal_reason = "sweep_extreme_violated"
                    continue

            # Stage 1: SWEEP_SEEN -> Look for current-bar MSS and FVG pair
            if narrative.stage == S09NarrativeStage.SWEEP_SEEN:
                self._match_mss_and_fvg(
                    context,
                    narrative,
                    working_bar_close_times,
                )

            # Stage 2: FVG_READY -> Check for retest signal
            if narrative.stage == S09NarrativeStage.FVG_READY:
                # Retest must be strictly AFTER MSS confirmation bar
                if N <= narrative.mss.index:
                    continue

                # Check opposite structure event after MSS
                if self._has_opposite_structure_after_mss(narrative, context):
                    narrative.stage = S09NarrativeStage.INVALIDATED
                    narrative.terminal_reason = "opposite_structure_after_mss"
                    continue

                # Track internal full-fill at current bar N
                fvg = narrative.fvg
                if narrative.direction == "BUY":
                    if context.low <= fvg.bottom:
                        narrative.fvg_filled_internally = True
                        narrative.fvg_filled_bar = N
                else:
                    if context.high >= fvg.top:
                        narrative.fvg_filled_internally = True
                        narrative.fvg_filled_bar = N

                # Prior full-fill invalidation (filled at bar < N)
                fvg_prior_filled = False
                if fvg.filled_at is not None and fvg.filled_at < N:
                    fvg_prior_filled = True
                else:
                    for ctx_fvg in context.active_fvgs:
                        if (
                            ctx_fvg.index == fvg.index
                            and ctx_fvg.mode == fvg.mode
                            and ctx_fvg.direction == fvg.direction
                        ):
                            if ctx_fvg.filled_at is not None and ctx_fvg.filled_at < N:
                                fvg_prior_filled = True
                                break

                if (
                    fvg_prior_filled
                    or (
                        narrative.fvg_filled_internally
                        and narrative.fvg_filled_bar is not None
                        and narrative.fvg_filled_bar < N
                    )
                ):
                    narrative.stage = S09NarrativeStage.INVALIDATED
                    narrative.terminal_reason = "fvg_filled_prior_to_retest"
                    continue

                # Close-through invalidation check at bar N
                if (narrative.direction == "BUY" and context.close < fvg.bottom) or (
                    narrative.direction == "SELL" and context.close > fvg.top
                ):
                    narrative.stage = S09NarrativeStage.INVALIDATED
                    narrative.terminal_reason = "fvg_close_through"
                    continue

                # Check zone overlap (retest touch)
                overlaps = (
                    (context.low <= fvg.top and context.high >= fvg.bottom)
                    if narrative.direction == "BUY"
                    else (context.high >= fvg.bottom and context.low <= fvg.top)
                )
                if not overlaps:
                    continue

                # Zone touched and respected! Build candidate proposal
                cand = self._build_candidate(context, narrative, working_emitted_clusters)
                if cand is not None:
                    prop_rank = self._proposal_rank_key(narrative, cand)
                    if narrative.window_key not in candidates_by_window:
                        candidates_by_window[narrative.window_key] = []
                    candidates_by_window[narrative.window_key].append(
                        (prop_rank, cand, narrative)
                    )

        # 8. Deterministic One-Setup-Per-Window Selection
        emitted_candidates: list[CandidateSetup] = []

        for w_key in sorted(candidates_by_window.keys()):
            proposals = candidates_by_window[w_key]
            # Sort proposals deterministically
            proposals.sort(key=lambda x: x[0])

            # Select the top winning candidate that successfully emits
            winner_cand: Optional[CandidateSetup] = None
            winner_narrative: Optional[S09Narrative] = None

            for _, cand, narr in proposals:
                if cand.evidence_cluster_id in working_emitted_clusters:
                    continue
                winner_cand = cand
                winner_narrative = narr
                break

            if winner_cand is not None and winner_narrative is not None:
                emitted_candidates.append(winner_cand)
                working_consumed_windows[w_key] = winner_narrative.grace_expiry_utc
                working_emitted_clusters[winner_cand.evidence_cluster_id] = winner_narrative.grace_expiry_utc

                winner_narrative.stage = S09NarrativeStage.EMITTED
                winner_narrative.terminal_reason = "candidate_emitted"

                # Invalidate all other active narratives in the same window
                for other_comp_key, other_narr in working_narratives.items():
                    if (
                        other_narr.window_key == w_key
                        and other_narr is not winner_narrative
                        and other_narr.stage in (S09NarrativeStage.SWEEP_SEEN, S09NarrativeStage.FVG_READY)
                    ):
                        other_narr.stage = S09NarrativeStage.INVALIDATED
                        other_narr.terminal_reason = "window_already_consumed"

        # 9. Prune terminal narratives
        active_narratives = {
            k: v
            for k, v in working_narratives.items()
            if v.stage in (S09NarrativeStage.SWEEP_SEEN, S09NarrativeStage.FVG_READY)
        }

        # 10. Canonical candidate sorting
        sorted_candidates = tuple(
            sorted(
                emitted_candidates,
                key=lambda c: (
                    c.strategy_id,
                    c.direction,
                    c.entry_price,
                    c.stop_loss,
                    c.setup_id,
                ),
            )
        )

        # 11. Atomic state commit
        self._narratives = active_narratives
        self._consumed_windows = working_consumed_windows
        self._emitted_clusters = working_emitted_clusters
        self._bar_close_times = working_bar_close_times
        self._last_bar_index = N
        self._last_timestamp = current_ts
        self._last_bar_close_time = current_bct
        self._last_context_payload = context.to_dict()
        self._last_result = sorted_candidates

        return sorted_candidates

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _assert_zero_future_leak(self, context: StrategyContext) -> None:
        """Defensive guard ensuring StrategyContext contains zero future-leaking evidence."""
        N = context.bar_index
        close_time = context.bar_close_time

        def _ensure_comparable(t1: pd.Timestamp, t2: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
            if t1.tzinfo is None and t2.tzinfo is not None:
                t1 = t1.tz_localize(t2.tzinfo)
            elif t1.tzinfo is not None and t2.tzinfo is None:
                t2 = t2.tz_localize(t1.tzinfo)
            return t1, t2

        for sw in context.recent_sweeps:
            if sw.index > N or sw.confirmed_at > N or sw.swept_at > N:
                raise StrategyStateError(
                    f"Future leak detected in LiquiditySweep: index={sw.index} > N={N}"
                )

        for st in context.recent_structures:
            if st.index > N or st.confirmed_swing_at > N:
                raise StrategyStateError(
                    f"Future leak detected in StructureEvent: index={st.index}, confirmed_swing_at={st.confirmed_swing_at} > N={N}"
                )

        for fvg in context.active_fvgs:
            if fvg.index > N or fvg.confirmed_at > N:
                raise StrategyStateError(
                    f"Future leak detected in FairValueGap: index={fvg.index} > N={N}"
                )
            if fvg.filled_at is not None and fvg.filled_at > N:
                raise StrategyStateError(
                    f"Future leak detected in FVG filled_at={fvg.filled_at} > N={N}"
                )

        for p in context.active_pools:
            if p.confirmed_at > N:
                raise StrategyStateError(
                    f"Future leak detected in LiquidityPool: confirmed_at={p.confirmed_at} > N={N}"
                )
            if p.swept_at is not None and p.swept_at > N:
                raise StrategyStateError(
                    f"Future leak detected in LiquidityPool swept_at={p.swept_at} > N={N}"
                )
            if getattr(p, "invalidated_at", None) is not None and p.invalidated_at > N:
                raise StrategyStateError(
                    f"Future leak detected in LiquidityPool invalidated_at={p.invalidated_at} > N={N}"
                )

        if context.htf_bias is not None:
            b = context.htf_bias
            if b.source_event_index is not None and b.source_event_index > N:
                raise StrategyStateError(
                    f"Future leak detected in HTF bias: source_event_index={b.source_event_index} > N={N}"
                )
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
            sd_ts = context.session_decision.timestamp
            sd_ts, ref = _ensure_comparable(sd_ts, close_time)
            if sd_ts > ref:
                raise StrategyStateError(
                    f"Future leak in session_decision.timestamp ({context.session_decision.timestamp}) > bar_close_time ({close_time})."
                )

    def _ingest_sweeps(
        self,
        context: StrategyContext,
        w_key: WindowKey,
        w_start: pd.Timestamp,
        w_end: pd.Timestamp,
        w_grace: pd.Timestamp,
        working_narratives: dict[tuple[WindowKey, SweepKey], S09Narrative],
    ) -> None:
        """Ingests new valid liquidity sweeps confirming at bar N inside the active window."""
        N = context.bar_index
        for sw in context.recent_sweeps:
            if sw.index != N or sw.confirmed_at != N or sw.swept_at != N:
                continue
            if not sw.valid or sw.mode != self._config.mode:
                continue

            # Determine direction from sweep
            direction: Optional[str] = None
            if sw.direction == "bullish" and sw.pool_kind in {"equal_lows", "swing_low"}:
                direction = "BUY"
            elif sw.direction == "bearish" and sw.pool_kind in {"equal_highs", "swing_high"}:
                direction = "SELL"

            if direction is None:
                continue

            # HTF Bias hard gate at admission
            if context.htf_bias is None:
                continue
            bias = context.htf_bias.bias
            if (direction == "BUY" and bias == "bearish") or (
                direction == "SELL" and bias == "bullish"
            ):
                continue

            sweep_key: SweepKey = (
                sw.direction,
                sw.pool_kind,
                sw.index,
                tuple(sorted(sw.pool_indices)),
            )
            composite_key = (w_key, sweep_key)

            if composite_key not in working_narratives:
                working_narratives[composite_key] = S09Narrative(
                    window_key=w_key,
                    window_start_utc=w_start,
                    window_end_utc=w_end,
                    grace_expiry_utc=w_grace,
                    sweep_key=sweep_key,
                    direction=direction,
                    sweep=sw,
                    stage=S09NarrativeStage.SWEEP_SEEN,
                    created_bar=N,
                )

    def _match_mss_and_fvg(
        self,
        context: StrategyContext,
        narrative: S09Narrative,
        bar_close_times: dict[int, pd.Timestamp],
    ) -> None:
        """Finds canonical MSS at bar N and linked FVG confirmed in same window."""
        N = context.bar_index

        aligned_dir = "bullish" if narrative.direction in {"BUY", "bullish"} else "bearish"

        # MSS must be confirmed at bar N
        eligible_mss: list[StructureEventSnapshot] = []
        for st in context.recent_structures:
            if st.index != N:
                continue
            if st.confirmed_swing_at > N:
                continue
            if st.mode != self._config.mode or st.direction != aligned_dir:
                continue
            if st.break_type != "close":
                continue
            if st.structure_leg_id is None:
                continue
            if self._config.require_displacement and not st.displacement:
                continue
            if st.event_type not in {"CHoCH", "BOS"}:
                continue

            # MSS confirmation close time must be inside the same window
            if not (narrative.window_start_utc <= context.bar_close_time < narrative.window_end_utc):
                continue

            eligible_mss.append(st)

        if not eligible_mss:
            return

        # Find linked FVG candidates
        valid_pairs: list[tuple[tuple, StructureEventSnapshot, FairValueGapSnapshot]] = []

        for mss in eligible_mss:
            for fvg in context.active_fvgs:
                if fvg.mode != self._config.mode or fvg.direction != aligned_dir:
                    continue
                if fvg.structure_leg_id is None or fvg.structure_leg_id != mss.structure_leg_id:
                    continue
                if not (narrative.sweep.index <= fvg.index < mss.index):
                    continue
                if fvg.confirmed_at > mss.index:
                    continue
                if (mss.index - fvg.index) > self._config.fvg_to_mss_max_bars:
                    continue

                # FVG confirmation close time must be in same window
                fvg_close = bar_close_times.get(fvg.confirmed_at)
                if fvg_close is None:
                    # Missing bar-close mapping fails closed
                    continue
                if not (narrative.window_start_utc <= fvg_close < narrative.window_end_utc):
                    continue

                # FVG must not be filled prior to or at MSS
                if fvg.filled_at is not None and fvg.filled_at <= mss.index:
                    continue

                # No opposite structure event between [fvg.index, mss.index] inclusive
                if self._has_opposite_structure_in_interval(
                    context, narrative.direction, fvg.index, mss.index
                ):
                    continue

                # Canonical tie-break rank for (MSS, FVG) pair
                pair_rank = (
                    mss.index,
                    0 if mss.event_type == "CHoCH" else 1,
                    mss.broken_swing_index or 0,
                    str(mss.structure_leg_id),
                    -fvg.index,
                    -fvg.confirmed_at,
                    fvg.top,
                    fvg.bottom,
                )
                valid_pairs.append((pair_rank, mss, fvg))

        if not valid_pairs:
            return

        valid_pairs.sort(key=lambda p: p[0])
        best_pair = valid_pairs[0]
        narrative.mss = best_pair[1]
        narrative.fvg = best_pair[2]
        narrative.stage = S09NarrativeStage.FVG_READY
        narrative.ready_at = N

    def _has_opposite_structure_in_interval(
        self,
        context: StrategyContext,
        direction: str,
        start_bar: int,
        end_bar: int,
    ) -> bool:
        """Checks if any opposite structure event exists in [start_bar, end_bar]."""
        opp_dir = "bearish" if direction in {"BUY", "bullish"} else "bullish"
        for st in context.recent_structures:
            if start_bar <= st.index <= end_bar and st.mode == self._config.mode:
                if st.direction == opp_dir:
                    return True
        return False

    def _has_opposite_structure_after_mss(
        self,
        narrative: S09Narrative,
        context: StrategyContext,
    ) -> bool:
        """Checks if any opposite structure event exists after MSS: mss.index < event.index <= N."""
        if narrative.mss is None:
            return False
        mss_idx = narrative.mss.index
        N = context.bar_index
        opp_dir = "bearish" if narrative.direction in {"BUY", "bullish"} else "bullish"
        for st in context.recent_structures:
            if mss_idx < st.index <= N and st.mode == self._config.mode:
                if st.direction == opp_dir:
                    return True
        return False

    def _build_candidate(
        self,
        context: StrategyContext,
        narrative: S09Narrative,
        working_emitted_clusters: dict[str, pd.Timestamp],
    ) -> Optional[CandidateSetup]:
        """Constructs CandidateSetup from a retested narrative."""
        N = context.bar_index
        fvg = narrative.fvg
        sweep = narrative.sweep
        mss = narrative.mss
        direction = narrative.direction

        if fvg is None or mss is None:
            return None

        # 1. Entry price
        if self._config.entry_level == "proximal":
            raw_entry = fvg.top if direction == "BUY" else fvg.bottom
        else:  # ce_50
            raw_entry = (fvg.top + fvg.bottom) / 2.0

        # 2. Stop loss price: sweep extreme ± buffer
        if direction == "BUY":
            raw_sl = sweep.price_wick - self._config.sl_buffer_price
        else:
            raw_sl = sweep.price_wick + self._config.sl_buffer_price

        # 3. Target selection from opposing active pools
        target_pool: Optional[LiquidityPoolSnapshot] = None
        raw_tp: float
        target_type = "fixed_rr"

        eligible_pools: list[LiquidityPoolSnapshot] = []
        for p in context.active_pools:
            if p.valid and not p.swept and p.confirmed_at <= N and p.mode == self._config.mode:
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
                return None
            risk = entry_price - stop_loss
            reward = take_profit - entry_price
        else:
            if not (take_profit < entry_price < stop_loss):
                return None
            risk = stop_loss - entry_price
            reward = entry_price - take_profit

        if risk <= 0.0:
            return None

        # 6. Final 2-decimal planned RR calculation
        planned_rr = round(reward / risk, 2)
        if planned_rr < self._config.min_rr:
            return None

        # 7. Cluster ID & duplicate emission check (shared format with S01)
        leg_component = f"leg-{self._config.mode}-{direction}-{mss.broken_swing_index}"
        zone_component = f"fvg-{self._config.mode}-{direction}-{fvg.index}"
        cluster_id = make_cluster_id(direction, leg_component, zone_component)

        if cluster_id in working_emitted_clusters:
            return None

        setup_id = make_setup_id(self.strategy_id, direction, N, cluster_id)

        # 8. Evidence references in canonical order: Sweep -> MSS -> FVG [-> Pool]
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
                "direction": sweep.direction,
                "pool_kind": sweep.pool_kind,
                "swept_at": sweep.swept_at,
                "sweep_type": sweep.sweep_type,
            },
        )

        sub_key_mss = f"{mss.event_type.lower()}_{mss.direction}_{mss.broken_swing_index}"
        ev_mss_id = make_evidence_id(
            "structure_event",
            self._config.mode,
            mss.index,
            sub_key_mss,
        )
        ev_mss = EvidenceRef(
            evidence_id=ev_mss_id,
            kind="structure_event",
            bar_index=mss.index,
            price=mss.broken_swing_price or (context.close if direction == "BUY" else context.close),
            time=mss.time,
            details={
                "event_type": mss.event_type,
                "direction": mss.direction,
                "structure_leg_id": mss.structure_leg_id,
                "displacement": mss.displacement,
            },
        )

        sub_key_fvg = f"fvg_{fvg.direction}_{fvg.index}"
        ev_fvg_id = make_evidence_id(
            "fair_value_gap",
            self._config.mode,
            fvg.index,
            sub_key_fvg,
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
                "confirmed_at": fvg.confirmed_at,
                "structure_leg_id": fvg.structure_leg_id,
            },
        )

        evidences: list[EvidenceRef] = [ev_sweep, ev_mss, ev_fvg]

        if target_pool is not None:
            pool_indices_token = "_".join(str(i) for i in sorted(target_pool.indices)) or "none"
            sub_key_pool = f"pool_{target_pool.kind}_{pool_indices_token}"
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
                    "indices": list(target_pool.indices),
                },
            )
            evidences.append(ev_pool)

        # 9. Rich metadata for auditability
        meta: dict[str, Any] = {
            "signal_bar": N,
            "available_from_bar": N + 1,
            "window_key": list(narrative.window_key),
            "window_name": narrative.window_key[0],
            "local_date": narrative.window_key[1],
            "window_start_utc": narrative.window_start_utc.isoformat(),
            "window_end_utc": narrative.window_end_utc.isoformat(),
            "grace_expiry_utc": narrative.grace_expiry_utc.isoformat(),
            "signal_bar_close_time": context.bar_close_time.isoformat(),
            "sweep_index": sweep.index,
            "sweep_price_wick": sweep.price_wick,
            "mss_index": mss.index,
            "mss_type": mss.event_type,
            "fvg_index": fvg.index,
            "fvg_top": fvg.top,
            "fvg_bottom": fvg.bottom,
            "structure_leg_id": mss.structure_leg_id,
            "entry_policy": self._config.entry_level,
            "sl_buffer_price": self._config.sl_buffer_price,
            "target_type": target_type,
            "htf_bias_at_signal": context.htf_bias.bias if context.htf_bias is not None else "neutral",
            "session_time_basis": "bar_close_time",
            "time_filter_disabled": not self._config.use_time_filter,
        }

        return CandidateSetup(
            setup_id=setup_id,
            strategy_id=self.strategy_id,
            direction=direction,  # type: ignore[arg-type]
            bar_index=N,
            timestamp=context.timestamp,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            planned_rr=planned_rr,
            evidences=tuple(evidences),
            evidence_cluster_id=cluster_id,
            expiry_bar=N,
            target_type=target_type,  # type: ignore[arg-type]
            quality_scores={"base_score": 100.0},
            meta=meta,
        )

    def _proposal_rank_key(
        self,
        narrative: S09Narrative,
        candidate: CandidateSetup,
    ) -> tuple:
        """Deterministic tie-break rank for competing proposals in the same window."""
        sweep = narrative.sweep
        mss = narrative.mss
        fvg = narrative.fvg
        return (
            sweep.index,  # Earlier sweep preferred
            0 if getattr(sweep, "sweep_type", "clean") == "clean" else 1,
            sweep.pool_kind,
            tuple(sorted(sweep.pool_indices)),
            mss.index if mss else 0,
            0 if mss and mss.event_type == "CHoCH" else 1,
            fvg.index if fvg else 0,
            candidate.evidence_cluster_id,
        )


__all__ = [
    "S09Config",
    "S09ICTSilverBulletStrategy",
    "S09NarrativeStage",
    "S09Narrative",
    "WindowKey",
    "SweepKey",
    "NEW_YORK_TZ",
    "WINDOW_SCHEDULE",
    "CANONICAL_WINDOWS",
    "compute_window_bounds_for_date",
    "get_ny_local_date_str",
    "get_window_for_close_time",
    "compute_bar_close_retention_bar",
]
