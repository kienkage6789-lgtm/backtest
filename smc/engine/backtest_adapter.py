"""
smc/engine/backtest_adapter.py
==============================
Bar-by-bar SMC Coordinator, Cooldown-after-fill, and HTF as-of Timeline (T53.9.3).

Coordinates the canonical multi-strategy execution pipeline:
Close N:
  Context -> Strategy Registry -> Regime -> Eligibility -> Confluence -> Selector -> PendingExecutionIntent
Open N+1:
  Intent -> Fill Gate -> Position Policy -> Execution Kernel -> Cooldown if filled
Intrabar N:
  -> Dynamic SL/TP -> Execution events
Close N:
  -> Telemetry -> New Decision
Mark-to-market:
  -> Valuation & Equity

Enforces zero-lookahead, exact timestamp boundaries, cash-basis RR,
deterministic cooldown, and batch/incremental/replay-prefix parity.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field
import datetime
import json
import math
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence, Union

import numpy as np
import pandas as pd

from engine.execution_kernel import (
    ExecutionBar,
    ExecutionKernel,
    KernelTransition,
    OpenInstruction,
    PositionState,
    _deep_freeze,
    _deep_thaw,
)
from smc.context.htf_bias import _get_effective_time, _parse_timezone_aware_timestamp
from smc.engine.confluence import ConfluenceBatch, build_confluence_batch
from smc.engine.context import (
    ContextBuilderConfig,
    StrategyContext,
    StrategyContextBuilder,
    SUPPORTED_TIMEFRAMES,
)
from smc.engine.eligibility import EligibilityGate
from smc.engine.errors import (
    StrategyRegistryError,
    StrategyStateError,
    StrategyValidationError,
    StrictModelTypeError,
)
from smc.engine.execution import (
    CooldownBook,
    ExecutionConfig,
    ExecutionEvent,
    FillValidationResult,
    PendingExecutionIntent,
    make_execution_event_id,
    validate_fill,
)
from smc.engine.models import (
    CandidateSetup,
    MarketRegime,
    SelectionDecision,
    StrategyEvaluation,
    _freeze,
    _unfreeze,
    _validate_base_token,
    _validate_finite_float,
    _validate_non_negative_int,
    _validate_str,
)
from smc.engine.protocol import StrategyTemplate
from smc.engine.regime import MarketRegimeClassifier, RegimeClassifierConfig
from smc.engine.registry import StrategyRegistry, StrategyRegistryConfig
from smc.engine.selector import (
    ClusterScorecard,
    DeterministicStrategySelector,
    SelectorConfig,
    SelectorOutput,
)
from smc.engine.strategies import (
    S01Config,
    S01ICT2022Strategy,
    S05Config,
    S05BOSOBRetestStrategy,
    S09Config,
    S09ICTSilverBulletStrategy,
    SMCSupertrendFVGMSSConfig,
    SMCSupertrendFVGMSSStrategy,
)
from smc.engine.telemetry import SelectionAuditRecord
from smc.models import StructureEvent

CoordinatorMode = Literal["smc_wave1", "smc_s01", "smc_s05", "smc_s09", "smc_st_fvg_mss", "smc_confluence"]

VALID_COORDINATOR_MODES: frozenset[str] = frozenset({
    "smc_wave1",
    "smc_s01",
    "smc_s05",
    "smc_s09",
    "smc_st_fvg_mss",
    "smc_confluence",
})

REQUIRED_BAR_COLUMNS: frozenset[str] = frozenset({"open", "high", "low", "close"})


# =============================================================================
# HTFTimeline: Explicit Higher-Timeframe As-Of Provider
# =============================================================================

class HTFTimeline:
    """
    Deterministic Higher-Timeframe (HTF) as-of event timeline provider.

    Contracts:
    - Only events with `effective_time <= bar_close_time` are emitted.
    - Exact boundary `effective_time == bar_close_time` is accepted.
    - Zero future lookahead: future events are held until their effective time.
    - Deterministic sorting: (effective_time, event.index, mode, event_type, direction).
    - Identity deduplication: identical payloads deduplicated; differing payloads raise ValueError.
    - Reset capability for batch/incremental parity.
    """

    def __init__(self, events: Optional[Sequence[StructureEvent]] = None) -> None:
        self._all_events: tuple[StructureEvent, ...] = self._validate_and_sort(events or ())
        self._cursor: int = 0
        self._emitted_keys: set[tuple] = set()

    @staticmethod
    def get_effective_time(event: StructureEvent) -> pd.Timestamp:
        """Extract timezone-aware UTC effective timestamp of an HTF event."""
        return _get_effective_time(event)

    @classmethod
    def _validate_and_sort(cls, events: Sequence[StructureEvent]) -> tuple[StructureEvent, ...]:
        seen_payloads: dict[tuple, StructureEvent] = {}
        for ev in events:
            if not isinstance(ev, StructureEvent):
                raise TypeError(f"Expected StructureEvent, got {type(ev).__name__}")
            eff_time = cls.get_effective_time(ev)
            key = (eff_time, int(ev.index), str(ev.mode), str(ev.event_type), str(ev.direction))
            if key in seen_payloads:
                existing = seen_payloads[key]
                if existing.to_dict() != ev.to_dict():
                    raise ValueError(
                        f"Conflicting duplicate HTF events with identical key {key} but differing payloads:\n"
                        f"Existing: {existing.to_dict()}\n"
                        f"Incoming: {ev.to_dict()}"
                    )
                continue
            seen_payloads[key] = ev

        sorted_events = sorted(
            seen_payloads.values(),
            key=lambda e: (
                cls.get_effective_time(e),
                int(e.index),
                str(e.mode),
                str(e.event_type),
                str(e.direction),
            ),
        )
        return tuple(sorted_events)

    def get_events_as_of(self, bar_close_time: pd.Timestamp) -> tuple[StructureEvent, ...]:
        """
        Return newly effective HTF events up to and including bar_close_time.
        Advances cursor monotonically so each event is emitted exactly once.
        """
        if not isinstance(bar_close_time, pd.Timestamp):
            bar_close_time = pd.Timestamp(bar_close_time)
        if bar_close_time.tzinfo is None:
            raise ValueError(f"bar_close_time must be timezone-aware, got {bar_close_time}")
        bar_close_time_utc = bar_close_time.tz_convert("UTC")

        newly_effective: list[StructureEvent] = []
        while self._cursor < len(self._all_events):
            ev = self._all_events[self._cursor]
            eff_time = self.get_effective_time(ev)
            if eff_time <= bar_close_time_utc:
                key = (eff_time, int(ev.index), str(ev.mode), str(ev.event_type), str(ev.direction))
                if key not in self._emitted_keys:
                    self._emitted_keys.add(key)
                    newly_effective.append(ev)
                self._cursor += 1
            else:
                break

        return tuple(newly_effective)

    def reset(self) -> None:
        """Reset timeline cursor and emitted keys to initial state."""
        self._cursor = 0
        self._emitted_keys.clear()


# =============================================================================
# Coordinator Step & Run Output Containers
# =============================================================================

@dataclass(frozen=True)
class StepResult:
    """Immutable result of processing a single bar."""
    bar_index: int
    timestamp: pd.Timestamp
    decision: SelectionDecision
    pending_intent: Optional[PendingExecutionIntent]
    execution_events: tuple[ExecutionEvent, ...]
    kernel_transition_open: KernelTransition
    kernel_transition_intrabar: KernelTransition
    equity: float
    balance: float
    position: Optional[PositionState]
    scorecards: tuple[ClusterScorecard, ...]
    audit_record: Optional[SelectionAuditRecord]


@dataclass(frozen=True)
class CoordinatorResult:
    """Immutable comprehensive result of backtest execution."""
    decisions: tuple[SelectionDecision, ...]
    pending_intents: tuple[PendingExecutionIntent, ...]
    execution_events: tuple[ExecutionEvent, ...]
    trades: tuple[dict[str, Any], ...]
    markers: tuple[dict[str, Any], ...]
    equity_curve: tuple[dict[str, Any], ...]
    final_equity: float
    final_balance: float
    max_drawdown: float
    max_drawdown_pct: float
    total_trades: int
    win_trades: int
    loss_trades: int
    win_rate: float
    profit_factor: float
    cooldown_snapshot: dict[str, dict[str, int]]
    audit_records: tuple[SelectionAuditRecord, ...]
    scorecards: tuple[ClusterScorecard, ...]

    def to_dict(self) -> dict[str, Any]:
        """Convert result to JSON-safe dictionary."""
        return {
            "decisions": [d.to_dict() for d in self.decisions],
            "pending_intents": [p.to_dict() for p in self.pending_intents],
            "execution_events": [e.to_dict() for e in self.execution_events],
            "trades": list(self.trades),
            "markers": list(self.markers),
            "equity_curve": list(self.equity_curve),
            "final_equity": float(self.final_equity),
            "final_balance": float(self.final_balance),
            "max_drawdown": float(self.max_drawdown),
            "max_drawdown_pct": float(self.max_drawdown_pct),
            "total_trades": int(self.total_trades),
            "win_trades": int(self.win_trades),
            "loss_trades": int(self.loss_trades),
            "win_rate": float(self.win_rate),
            "profit_factor": float(self.profit_factor),
            "cooldown_snapshot": copy.deepcopy(self.cooldown_snapshot),
            "audit_records": [a.to_dict() for a in self.audit_records],
        }

# =============================================================================
# Diagnostic Rejection Taxonomy Mapping (22 Codes)
# =============================================================================

DIAGNOSTIC_REJECTION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "missing_htf_bias": {
        "en": "HTF structure bias is missing or unconfirmed at this bar.",
        "vi": "Chưa có hoặc chưa xác nhận HTF bias tại nến này.",
    },
    "htf_bias_mismatch": {
        "en": "HTF bias opposes candidate direction.",
        "vi": "HTF bias ngược hướng với candidate setup.",
    },
    "invalid_sweep": {
        "en": "Liquidity sweep is invalid, unlinked, or stale.",
        "vi": "Sweep thanh khoản không hợp lệ, không liên kết hoặc quá cũ.",
    },
    "sweep_not_confirmed": {
        "en": "Liquidity sweep confirmation bar is beyond current bar.",
        "vi": "Sweep chưa được xác nhận tại nến hiện tại.",
    },
    "wrong_structure_direction": {
        "en": "Structure break direction does not match candidate trade direction.",
        "vi": "Hướng phá vỡ cấu trúc không phù hợp với hướng lệnh.",
    },
    "wrong_regime": {
        "en": "Market regime suitability score is 0 in 30-cell matrix.",
        "vi": "Regime thị trường không phù hợp trong ma trận 30 ô (điểm 0).",
    },
    "mss_before_sweep": {
        "en": "MSS event occurred before or at the liquidity sweep bar.",
        "vi": "MSS xuất hiện trước hoặc cùng nến với Liquidity Sweep.",
    },
    "missing_displacement": {
        "en": "Structure event lacks required impulse displacement candle.",
        "vi": "Sự kiện cấu trúc thiếu nến displacement theo yêu cầu.",
    },
    "fvg_wrong_structure_leg": {
        "en": "Fair Value Gap does not belong to the same structure leg as MSS.",
        "vi": "FVG không thuộc cùng structure leg với MSS.",
    },
    "fvg_too_far_from_mss": {
        "en": "Fair Value Gap distance to MSS exceeds maximum allowed bars.",
        "vi": "Khoảng cách FVG tới MSS vượt quá số nến tối đa cho phép.",
    },
    "fvg_filled_before_retest": {
        "en": "FVG was fully filled or invalidated before price reached retest entry.",
        "vi": "FVG đã bị lấp đầy hoặc vô hiệu hóa trước khi retest vào lệnh.",
    },
    "opposite_structure_detected": {
        "en": "Opposite structure shift occurred before entry, invalidating narrative.",
        "vi": "Xuất hiện cấu trúc đảo chiều ngược lại trước khi vào lệnh.",
    },
    "outside_session": {
        "en": "Bar is outside canonical strategy session window.",
        "vi": "Nến nằm ngoài khung giờ phiên giao dịch cho phép.",
    },
    "grace_expired": {
        "en": "Session grace period window (15 minutes) has expired.",
        "vi": "Thời gian ân hạn phiên (15 phút) đã hết hiệu lực.",
    },
    "ob_too_old": {
        "en": "Order Block age in bars exceeds maximum permitted lookback.",
        "vi": "Order Block quá tuổi (vượt quá số nến tồn tại tối đa).",
    },
    "ob_already_retested": {
        "en": "Order Block was already retested; only first retest is eligible.",
        "vi": "Order Block đã được retest trước đó; chỉ chấp nhận first retest.",
    },
    "invalid_entry_geometry": {
        "en": "Entry/SL/TP geometry is inverted or violates price ordering.",
        "vi": "Hình học Entry/SL/TP bị sai vị trí hoặc vi phạm thứ tự giá.",
    },
    "rr_below_minimum": {
        "en": "Planned risk/reward ratio is below strategy minimum requirement.",
        "vi": "Tỷ lệ R:R dự kiến thấp hơn ngưỡng tối thiểu yêu cầu.",
    },
    "selector_rejected": {
        "en": "Candidate was rejected by selector in favor of a higher-scoring setup.",
        "vi": "Candidate bị selector loại do điểm số thấp hơn setup được chọn.",
    },
    "cooldown_active": {
        "en": "Strategy cooldown timer is currently active for this direction.",
        "vi": "Thời gian cooldown của chiến lược vẫn đang có hiệu lực.",
    },
    "position_already_open": {
        "en": "A position in the same direction is already open in portfolio.",
        "vi": "Đang có vị thế cùng hướng mở trong tài khoản.",
    },
    "execution_no_fill": {
        "en": "Order was not filled at open of next bar or expired without touch.",
        "vi": "Lệnh không khớp tại giá mở cửa nến kế tiếp hoặc hết hạn.",
    },
}


def map_diagnostic_rejection_code(
    raw_reason: str,
    candidate: Optional[CandidateSetup] = None,
    context: Optional[StrategyContext] = None,
    details: Optional[dict[str, Any]] = None,
) -> str:
    """
    Standardize any raw rejection reason string into one of the 22 canonical diagnostic codes.
    """
    if raw_reason in DIAGNOSTIC_REJECTION_DESCRIPTIONS:
        return raw_reason

    # HTF bias checks
    if raw_reason == "missing_required_evidence" and context is not None and context.htf_bias is None:
        return "missing_htf_bias"
    if raw_reason == "htf_bias_mismatch":
        return "htf_bias_mismatch"

    # Regime & direction
    if raw_reason == "wrong_regime":
        return "wrong_regime"
    if raw_reason in ("conflicting_direction", "wrong_structure_direction"):
        return "wrong_structure_direction"

    # Event ordering & timing
    if raw_reason == "invalid_event_order":
        return "mss_before_sweep"
    if raw_reason == "stale_liquidity_sweep":
        return "invalid_sweep"
    if raw_reason == "opposite_structure_shift":
        return "opposite_structure_detected"

    # FVG checks
    if raw_reason in ("fvg_invalidated_by_close", "invalid_fvg"):
        return "fvg_filled_before_retest"

    # OB checks
    if raw_reason == "invalid_order_block":
        return "ob_already_retested"

    # Session checks
    if raw_reason == "outside_session":
        return "outside_session"
    if raw_reason in ("expired_setup", "no_next_bar"):
        return "grace_expired" if (candidate and candidate.strategy_id == "S09") else "execution_no_fill"

    # RR & Geometry
    if raw_reason in ("insufficient_rr", "insufficient_rr_at_fill"):
        return "rr_below_minimum"
    if raw_reason in ("geometry_violation_at_fill", "invalid_geometry"):
        return "invalid_entry_geometry"

    # Cooldown & Selector
    if raw_reason == "cooldown_active":
        return "cooldown_active"
    if raw_reason in ("insufficient_score", "no_eligible_setup", "cluster_competition"):
        return "selector_rejected"

    # Position
    if raw_reason in ("position_already_open_same_direction", "position_already_open"):
        return "position_already_open"

    return raw_reason


# =============================================================================
# SMCBacktestCoordinator: Bar-by-bar Coordinator
# =============================================================================

class SMCBacktestCoordinator:
    """
    Deterministic SMC Bar-by-bar Backtest Coordinator.

    Manages the full lifecycle across:
    - ContextBuilder (LTF candle ingestion, feature tracking)
    - HTFTimeline (zero-lookahead HTF event delivery)
    - MarketRegimeClassifier (5 mutually exclusive regimes)
    - StrategyRegistry (S01, S05, S09 strategy evaluation)
    - EligibilityGate (matrix, evidence, session, cooldown checks)
    - EvidenceDeduplicator & DirectionConflictDetector (confluence batch)
    - DeterministicStrategySelector (ownership, scoring, decision)
    - PendingExecutionIntent (deep-immutable intent outside decision)
    - Fill Gate (geometry, cash-basis RR, price adjustment)
    - Position Policy (flat open, same-direction skip, atomic reversal)
    - ExecutionKernel (order matching, dynamic SL/TP, PnL accounting)
    - CooldownBook (post-fill cooldown enforcement)
    """

    def __init__(
        self,
        execution_config: Optional[ExecutionConfig] = None,
        context_config: Optional[ContextBuilderConfig] = None,
        selector_config: Optional[SelectorConfig] = None,
        regime_config: Optional[RegimeClassifierConfig] = None,
        htf_events: Optional[Sequence[StructureEvent]] = None,
        cooldown_bars: int = 3,
        s01_stale_sweep_max_bars: int = 24,
        mode: CoordinatorMode = "smc_wave1",
        strategies: Optional[Sequence[StrategyTemplate]] = None,
        initial_capital: float = 10000.0,
    ) -> None:
        if mode not in VALID_COORDINATOR_MODES:
            raise ValueError(f"Invalid mode '{mode}'. Must be one of {sorted(VALID_COORDINATOR_MODES)}")

        self.mode: CoordinatorMode = mode
        self.cooldown_bars: int = _validate_non_negative_int(cooldown_bars, "cooldown_bars")
        self.s01_stale_sweep_max_bars: int = _validate_non_negative_int(
            s01_stale_sweep_max_bars, "s01_stale_sweep_max_bars"
        )
        if self.s01_stale_sweep_max_bars <= 0:
            raise ValueError("s01_stale_sweep_max_bars must be > 0")
        self._initial_capital: float = float(initial_capital)
        self.execution_config: ExecutionConfig = execution_config or ExecutionConfig()
        self.context_config: ContextBuilderConfig = context_config or ContextBuilderConfig()
        self.selector_config: SelectorConfig = selector_config or SelectorConfig()
        self.regime_config: RegimeClassifierConfig = regime_config or RegimeClassifierConfig()

        # Initialize strategies according to mode
        if strategies is not None:
            strat_list = list(strategies)
        else:
            strat_list = [
                S01ICT2022Strategy(
                    S01Config(require_displacement=False, sweep_to_mss_max_bars=24)
                ),
                S05BOSOBRetestStrategy(
                    S05Config(
                        require_displacement=False,
                        max_ob_age_bars=75,
                        min_rr=1.0,
                        fallback_rr=2.0,
                    )
                ),
                # Temporary: keep S09 logic but disable session-hour gating.
                S09ICTSilverBulletStrategy(
                    S09Config(use_time_filter=False, require_displacement=False)
                ),
                SMCSupertrendFVGMSSStrategy(
                    SMCSupertrendFVGMSSConfig()
                ),
            ]

        if mode in ("smc_wave1", "smc_confluence"):
            enabled_ids: Optional[tuple[str, ...]] = ("S01", "S05", "S09")
        elif mode == "smc_s01":
            enabled_ids = ("S01",)
        elif mode == "smc_s05":
            enabled_ids = ("S05",)
        elif mode == "smc_s09":
            enabled_ids = ("S09",)
        elif mode == "smc_st_fvg_mss":
            enabled_ids = ("smc_st_fvg_mss",)

        self.strategy_registry_config = StrategyRegistryConfig(enabled_strategy_ids=enabled_ids)
        self.strategy_registry = StrategyRegistry(strat_list, config=self.strategy_registry_config)

        # Components
        self.htf_timeline = HTFTimeline(htf_events)
        self.context_builder = StrategyContextBuilder(config=self.context_config)
        self.regime_classifier = MarketRegimeClassifier(config=self.regime_config)
        self.eligibility_gate = EligibilityGate(
            s01_stale_sweep_max_bars=self.s01_stale_sweep_max_bars
        )
        self.selector = DeterministicStrategySelector(config=self.selector_config)
        self.cooldown_book = CooldownBook()

        # Platform execution kernel
        self.kernel = ExecutionKernel(
            initial_capital=self._initial_capital,
            lot_size=self.execution_config.lot_size,
            contract_size=self.execution_config.contract_size,
            spread_val=self.execution_config.spread,
            commission_per_side=self.execution_config.commission_per_side,
            validation_mode="wave1",
        )

        # Private coordinator state
        self._pending_intent: Optional[PendingExecutionIntent] = None
        self._last_bar_index: Optional[int] = None
        self._last_candle_payload: Optional[dict[str, Any]] = None
        self._last_step_result: Optional[StepResult] = None
        self._last_context: Optional[StrategyContext] = None
        self._last_evals: tuple[StrategyEvaluation, ...] = ()
        self._last_confluence_batch: Optional[ConfluenceBatch] = None
        self._last_cands_by_strat: dict[str, tuple[CandidateSetup, ...]] = {}

        # Historical logs
        self._all_decisions: list[SelectionDecision] = []
        self._all_intents: list[PendingExecutionIntent] = []
        self._all_events: list[ExecutionEvent] = []
        self._all_scorecards: list[ClusterScorecard] = []
        self._all_audit_records: list[SelectionAuditRecord] = []
        self._equity_curve: list[dict[str, Any]] = []

    @property
    def initial_capital(self) -> float:
        return self._initial_capital

    def reset(self) -> None:
        """Fully reset coordinator and all underlying pipeline components to initial clean state."""
        self.context_builder.reset()
        self.htf_timeline.reset()
        self.regime_classifier.reset()
        self.strategy_registry.reset_all()
        self.cooldown_book.reset()

        self.kernel = ExecutionKernel(
            initial_capital=self._initial_capital,
            lot_size=self.execution_config.lot_size,
            contract_size=self.execution_config.contract_size,
            spread_val=self.execution_config.spread,
            commission_per_side=self.execution_config.commission_per_side,
            validation_mode="wave1",
        )

        self._pending_intent = None
        self._last_bar_index = None
        self._last_candle_payload = None
        self._last_step_result = None
        self._last_context = None
        self._last_evals = ()
        self._last_confluence_batch = None
        self._last_cands_by_strat = {}

        self._all_decisions.clear()
        self._all_intents.clear()
        self._all_events.clear()
        self._all_scorecards.clear()
        self._all_audit_records.clear()
        self._equity_curve.clear()

    def step(
        self,
        candle: Union[Mapping[str, Any], pd.Series],
        is_last_bar: bool = False,
        current_htf_events: Optional[Sequence[StructureEvent]] = None,
    ) -> StepResult:
        """
        Process a single bar through the canonical phases:
        1. Open phase: fill pending intent created at Close i-1
        2. Intrabar phase: dynamic SL/TP evaluation via kernel
        3. Close phase: HTF timeline -> context -> regime -> registry -> eligibility -> confluence -> selector
        4. Mark-to-market: floating valuation and equity update
        """
        c_dict = candle.to_dict() if isinstance(candle, pd.Series) else dict(candle)
        if isinstance(candle, pd.Series) and "time" not in c_dict and isinstance(candle.name, (pd.Timestamp, datetime.datetime, str)):
            c_dict["time"] = candle.name

        for req in REQUIRED_BAR_COLUMNS:
            if req not in c_dict:
                raise KeyError(f"Candle missing required field '{req}'")

        if "bar_index" not in c_dict:
            if "index" in c_dict:
                c_dict["bar_index"] = c_dict["index"]
            else:
                raise KeyError("Candle missing 'bar_index'")

        bar_idx = _validate_non_negative_int(c_dict["bar_index"], "bar_index")

        raw_ts = c_dict.get("time", c_dict.get("timestamp"))
        if raw_ts is None:
            raise KeyError("Candle missing timestamp ('time' or 'timestamp')")
        ts = _parse_timezone_aware_timestamp(raw_ts, name="candle timestamp")

        c_open = _validate_finite_float(c_dict["open"], "open", min_val=0.00001)
        c_high = _validate_finite_float(c_dict["high"], "high", min_val=0.00001)
        c_low = _validate_finite_float(c_dict["low"], "low", min_val=0.00001)
        c_close = _validate_finite_float(c_dict["close"], "close", min_val=0.00001)
        c_vol = _validate_finite_float(c_dict.get("volume", c_dict.get("tick_volume", 0.0)), "volume", min_val=0.0)

        # Idempotency and retry check
        curr_payload = {
            "bar_index": bar_idx,
            "time": ts.isoformat(),
            "open": round(c_open, 5),
            "high": round(c_high, 5),
            "low": round(c_low, 5),
            "close": round(c_close, 5),
            "volume": round(c_vol, 5),
            "is_last_bar": bool(is_last_bar),
        }

        if self._last_bar_index is not None:
            if bar_idx == self._last_bar_index:
                if self._last_candle_payload == curr_payload:
                    assert self._last_step_result is not None
                    return self._last_step_result
                raise StrategyStateError(
                    f"Conflicting evaluation on same bar {bar_idx} with differing payload."
                )
            elif bar_idx < self._last_bar_index:
                raise StrategyStateError(
                    f"Non-monotonic bar_index {bar_idx} < last {self._last_bar_index}"
                )
            elif bar_idx > self._last_bar_index + 1:
                raise StrategyStateError(
                    f"Non-consecutive bar_index {bar_idx} > last + 1 ({self._last_bar_index + 1})"
                )

        step_events: list[ExecutionEvent] = []

        # Prepare ExecutionBar for kernel
        ts_sec = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(pd.Timestamp(ts).value // 10**9)
        exec_bar = ExecutionBar(
            bar_index=bar_idx,
            timestamp=max(0, ts_sec),
            time_value=ts.isoformat(),
            open=c_open,
            high=c_high,
            low=c_low,
            close=c_close,
        )

        # ---------------------------------------------------------------------
        # 1. Open Phase
        # ---------------------------------------------------------------------
        intent_to_fill = self._pending_intent
        self._pending_intent = None
        open_transition = KernelTransition(
            status="NO_ACTION",
            position_before=self.kernel.position,
            position_after=self.kernel.position,
            closed_trade=None,
            markers=(),
            realized_net_pnl=0.0,
        )

        if intent_to_fill is not None:
            val_res = validate_fill(intent_to_fill, c_open, self.execution_config)

            if not val_res.is_valid:
                if val_res.reason == "short_disabled":
                    # ORDER_SKIPPED / short_disabled
                    evt = ExecutionEvent(
                        event_version="1.0.0",
                        event_id=make_execution_event_id(bar_idx, "ORDER_SKIPPED", intent_to_fill.strategy_id, intent_to_fill.setup_id),
                        event_type="ORDER_SKIPPED",
                        reason="short_disabled",
                        symbol=intent_to_fill.symbol,
                        timeframe=intent_to_fill.timeframe,
                        bar_index=bar_idx,
                        bar_time=ts,
                        signal_bar_index=intent_to_fill.signal_bar_index,
                        signal_bar_time=intent_to_fill.signal_bar_time,
                        decision_id=intent_to_fill.decision_id,
                        strategy_id=intent_to_fill.strategy_id,
                        setup_id=intent_to_fill.setup_id,
                        direction=intent_to_fill.direction,
                        planned_entry=intent_to_fill.planned_entry,
                        planned_sl=intent_to_fill.planned_sl,
                        planned_tp=intent_to_fill.planned_tp,
                        cluster_id=intent_to_fill.cluster_id,
                        meta={
                            "supporting_strategy_ids": list(intent_to_fill.supporting_strategy_ids),
                            "evidence_ids": list(intent_to_fill.evidence_ids),
                            "regime": intent_to_fill.regime,
                            "session": intent_to_fill.session,
                            "selector_score": intent_to_fill.selector_score,
                        },
                    )
                    step_events.append(evt)
                    self._all_events.append(evt)
                else:
                    # ORDER_REJECTED / geometry_violation_at_fill or insufficient_rr_at_fill
                    evt = ExecutionEvent(
                        event_version="1.0.0",
                        event_id=make_execution_event_id(bar_idx, "ORDER_REJECTED", intent_to_fill.strategy_id, intent_to_fill.setup_id),
                        event_type="ORDER_REJECTED",
                        reason=val_res.reason,
                        symbol=intent_to_fill.symbol,
                        timeframe=intent_to_fill.timeframe,
                        bar_index=bar_idx,
                        bar_time=ts,
                        signal_bar_index=intent_to_fill.signal_bar_index,
                        signal_bar_time=intent_to_fill.signal_bar_time,
                        decision_id=intent_to_fill.decision_id,
                        strategy_id=intent_to_fill.strategy_id,
                        setup_id=intent_to_fill.setup_id,
                        direction=intent_to_fill.direction,
                        planned_entry=intent_to_fill.planned_entry,
                        planned_sl=intent_to_fill.planned_sl,
                        planned_tp=intent_to_fill.planned_tp,
                        actual_entry=val_res.actual_entry,
                        actual_sl=val_res.actual_sl,
                        actual_tp=val_res.actual_tp,
                        risk_cash=val_res.risk_cash,
                        reward_cash=val_res.reward_cash,
                        effective_rr=val_res.effective_rr,
                        cluster_id=intent_to_fill.cluster_id,
                        meta={
                            "supporting_strategy_ids": list(intent_to_fill.supporting_strategy_ids),
                            "evidence_ids": list(intent_to_fill.evidence_ids),
                            "regime": intent_to_fill.regime,
                            "session": intent_to_fill.session,
                            "selector_score": intent_to_fill.selector_score,
                        },
                    )
                    step_events.append(evt)
                    self._all_events.append(evt)

            else:
                # Valid fill candidate: evaluate position policy
                current_pos = self.kernel.position

                if current_pos is not None and current_pos.direction == intent_to_fill.direction:
                    # Same direction: ORDER_SKIPPED / position_already_open_same_direction
                    evt = ExecutionEvent(
                        event_version="1.0.0",
                        event_id=make_execution_event_id(bar_idx, "ORDER_SKIPPED", intent_to_fill.strategy_id, intent_to_fill.setup_id),
                        event_type="ORDER_SKIPPED",
                        reason="position_already_open_same_direction",
                        symbol=intent_to_fill.symbol,
                        timeframe=intent_to_fill.timeframe,
                        bar_index=bar_idx,
                        bar_time=ts,
                        signal_bar_index=intent_to_fill.signal_bar_index,
                        signal_bar_time=intent_to_fill.signal_bar_time,
                        decision_id=intent_to_fill.decision_id,
                        strategy_id=intent_to_fill.strategy_id,
                        setup_id=intent_to_fill.setup_id,
                        direction=intent_to_fill.direction,
                        planned_entry=intent_to_fill.planned_entry,
                        planned_sl=intent_to_fill.planned_sl,
                        planned_tp=intent_to_fill.planned_tp,
                        cluster_id=intent_to_fill.cluster_id,
                        meta={
                            "supporting_strategy_ids": list(intent_to_fill.supporting_strategy_ids),
                            "evidence_ids": list(intent_to_fill.evidence_ids),
                            "regime": intent_to_fill.regime,
                            "session": intent_to_fill.session,
                            "selector_score": intent_to_fill.selector_score,
                        },
                    )
                    step_events.append(evt)
                    self._all_events.append(evt)

                else:
                    # Prepare position metadata for execution kernel
                    pos_meta = {
                        "symbol": intent_to_fill.symbol,
                        "timeframe": intent_to_fill.timeframe,
                        "decision_id": intent_to_fill.decision_id,
                        "signal_bar_index": intent_to_fill.signal_bar_index,
                        "signal_bar_time": intent_to_fill.signal_bar_time.isoformat(),
                        "strategy_id": intent_to_fill.strategy_id,
                        "setup_id": intent_to_fill.setup_id,
                        "direction": intent_to_fill.direction,
                        "cluster_id": intent_to_fill.cluster_id,
                        "planned_entry": intent_to_fill.planned_entry,
                        "planned_sl": intent_to_fill.planned_sl,
                        "planned_tp": intent_to_fill.planned_tp,
                        "actual_entry": val_res.actual_entry,
                        "actual_sl": val_res.actual_sl,
                        "actual_tp": val_res.actual_tp,
                        "risk_cash": val_res.risk_cash,
                        "reward_cash": val_res.reward_cash,
                        "effective_rr": val_res.effective_rr,
                        "supporting_strategy_ids": list(intent_to_fill.supporting_strategy_ids),
                        "evidence_ids": list(intent_to_fill.evidence_ids),
                        "regime": intent_to_fill.regime,
                        "session": intent_to_fill.session,
                    }

                    instruction = OpenInstruction(
                        action="OPEN_OR_REVERSE",
                        direction=intent_to_fill.direction,
                        entry_price=val_res.actual_entry,
                        sl_price=val_res.actual_sl,
                        tp_price=val_res.actual_tp,
                        source="wave1",
                        metadata=pos_meta,
                    )

                    old_pos = self.kernel.position
                    open_transition = self.kernel.process_open(exec_bar, instruction)

                    if open_transition.status == "REVERSED":
                        # 1. Emit POSITION_CLOSED / opposite_signal for old position
                        old_meta = _deep_thaw(old_pos.metadata) if old_pos is not None else {}
                        ct = open_transition.closed_trade
                        assert ct is not None

                        pos_closed_evt = ExecutionEvent(
                            event_version="1.0.0",
                            event_id=make_execution_event_id(
                                bar_idx,
                                "POSITION_CLOSED",
                                old_meta.get("strategy_id", "UNKNOWN"),
                                old_meta.get("setup_id", "UNKNOWN"),
                            ),
                            event_type="POSITION_CLOSED",
                            reason="opposite_signal",
                            symbol=old_meta.get("symbol", intent_to_fill.symbol),
                            timeframe=old_meta.get("timeframe", intent_to_fill.timeframe),
                            bar_index=bar_idx,
                            bar_time=ts,
                            signal_bar_index=int(old_meta.get("signal_bar_index", bar_idx - 1)),
                            signal_bar_time=pd.Timestamp(old_meta.get("signal_bar_time", ts)),
                            decision_id=str(old_meta.get("decision_id", "")),
                            strategy_id=str(old_meta.get("strategy_id", "UNKNOWN")),
                            setup_id=str(old_meta.get("setup_id", "UNKNOWN")),
                            direction=old_pos.direction,
                            planned_entry=float(old_meta.get("planned_entry", old_pos.entry_price)),
                            planned_sl=float(old_meta.get("planned_sl", old_pos.sl_price or 0.0)),
                            planned_tp=float(old_meta.get("planned_tp", old_pos.tp_price or 0.0)),
                            actual_entry=float(old_meta.get("actual_entry", old_pos.entry_price)),
                            actual_sl=float(old_meta.get("actual_sl", old_pos.sl_price or 0.0)),
                            actual_tp=float(old_meta.get("actual_tp", old_pos.tp_price or 0.0)),
                            risk_cash=float(old_meta.get("risk_cash", 0.0)),
                            reward_cash=float(old_meta.get("reward_cash", 0.0)),
                            effective_rr=float(old_meta.get("effective_rr", 0.0)),
                            exit_price=float(ct["exit_price"]),
                            gross_pnl=float(round(
                                (ct["exit_price"] - old_pos.entry_price) * old_pos.multiplier
                                if old_pos.direction == "BUY"
                                else (old_pos.entry_price - ct["exit_price"]) * old_pos.multiplier,
                                2,
                            )),
                            net_pnl=float(ct["pnl"]),
                            cluster_id=old_meta.get("cluster_id"),
                            meta={
                                "trade_id": ct["trade_id"],
                                "exit_reason": ct["exit_reason"],
                            },
                        )
                        step_events.append(pos_closed_evt)
                        self._all_events.append(pos_closed_evt)

                        # 2. Record cooldown for the new owner
                        self.cooldown_book.record_fill(
                            intent_to_fill.strategy_id,
                            intent_to_fill.direction,
                            bar_idx,
                            cooldown_bars=self.cooldown_bars,
                        )

                        # 3. Emit ORDER_FILLED / fill_ok for the new order
                        fill_evt = ExecutionEvent(
                            event_version="1.0.0",
                            event_id=make_execution_event_id(bar_idx, "ORDER_FILLED", intent_to_fill.strategy_id, intent_to_fill.setup_id),
                            event_type="ORDER_FILLED",
                            reason="fill_ok",
                            symbol=intent_to_fill.symbol,
                            timeframe=intent_to_fill.timeframe,
                            bar_index=bar_idx,
                            bar_time=ts,
                            signal_bar_index=intent_to_fill.signal_bar_index,
                            signal_bar_time=intent_to_fill.signal_bar_time,
                            decision_id=intent_to_fill.decision_id,
                            strategy_id=intent_to_fill.strategy_id,
                            setup_id=intent_to_fill.setup_id,
                            direction=intent_to_fill.direction,
                            planned_entry=intent_to_fill.planned_entry,
                            planned_sl=intent_to_fill.planned_sl,
                            planned_tp=intent_to_fill.planned_tp,
                            actual_entry=val_res.actual_entry,
                            actual_sl=val_res.actual_sl,
                            actual_tp=val_res.actual_tp,
                            risk_cash=val_res.risk_cash,
                            reward_cash=val_res.reward_cash,
                            effective_rr=val_res.effective_rr,
                            cluster_id=intent_to_fill.cluster_id,
                            meta={
                                "supporting_strategy_ids": list(intent_to_fill.supporting_strategy_ids),
                                "evidence_ids": list(intent_to_fill.evidence_ids),
                                "regime": intent_to_fill.regime,
                                "session": intent_to_fill.session,
                                "selector_score": intent_to_fill.selector_score,
                            },
                        )
                        step_events.append(fill_evt)
                        self._all_events.append(fill_evt)

                    elif open_transition.status == "OPENED":
                        # Record cooldown for the owner
                        self.cooldown_book.record_fill(
                            intent_to_fill.strategy_id,
                            intent_to_fill.direction,
                            bar_idx,
                            cooldown_bars=self.cooldown_bars,
                        )

                        fill_evt = ExecutionEvent(
                            event_version="1.0.0",
                            event_id=make_execution_event_id(bar_idx, "ORDER_FILLED", intent_to_fill.strategy_id, intent_to_fill.setup_id),
                            event_type="ORDER_FILLED",
                            reason="fill_ok",
                            symbol=intent_to_fill.symbol,
                            timeframe=intent_to_fill.timeframe,
                            bar_index=bar_idx,
                            bar_time=ts,
                            signal_bar_index=intent_to_fill.signal_bar_index,
                            signal_bar_time=intent_to_fill.signal_bar_time,
                            decision_id=intent_to_fill.decision_id,
                            strategy_id=intent_to_fill.strategy_id,
                            setup_id=intent_to_fill.setup_id,
                            direction=intent_to_fill.direction,
                            planned_entry=intent_to_fill.planned_entry,
                            planned_sl=intent_to_fill.planned_sl,
                            planned_tp=intent_to_fill.planned_tp,
                            actual_entry=val_res.actual_entry,
                            actual_sl=val_res.actual_sl,
                            actual_tp=val_res.actual_tp,
                            risk_cash=val_res.risk_cash,
                            reward_cash=val_res.reward_cash,
                            effective_rr=val_res.effective_rr,
                            cluster_id=intent_to_fill.cluster_id,
                            meta={
                                "supporting_strategy_ids": list(intent_to_fill.supporting_strategy_ids),
                                "evidence_ids": list(intent_to_fill.evidence_ids),
                                "regime": intent_to_fill.regime,
                                "session": intent_to_fill.session,
                                "selector_score": intent_to_fill.selector_score,
                            },
                        )
                        step_events.append(fill_evt)
                        self._all_events.append(fill_evt)

        # ---------------------------------------------------------------------
        # 2. Intrabar Phase
        # ---------------------------------------------------------------------
        active_pos_before_intrabar = self.kernel.position
        intrabar_transition = self.kernel.process_intrabar(exec_bar)

        if intrabar_transition.status in ("STOPPED", "TARGETED"):
            assert active_pos_before_intrabar is not None
            pos_meta = _deep_thaw(active_pos_before_intrabar.metadata)
            ct = intrabar_transition.closed_trade
            assert ct is not None

            reason = "stop_loss" if intrabar_transition.status == "STOPPED" else "take_profit"
            pos_closed_evt = ExecutionEvent(
                event_version="1.0.0",
                event_id=make_execution_event_id(
                    bar_idx,
                    "POSITION_CLOSED",
                    pos_meta.get("strategy_id", "UNKNOWN"),
                    pos_meta.get("setup_id", "UNKNOWN"),
                ),
                event_type="POSITION_CLOSED",
                reason=reason,
                symbol=pos_meta.get("symbol", self.context_config.symbol),
                timeframe=pos_meta.get("timeframe", self.context_config.timeframe),
                bar_index=bar_idx,
                bar_time=ts,
                signal_bar_index=int(pos_meta.get("signal_bar_index", bar_idx - 1)),
                signal_bar_time=pd.Timestamp(pos_meta.get("signal_bar_time", ts)),
                decision_id=str(pos_meta.get("decision_id", "")),
                strategy_id=str(pos_meta.get("strategy_id", "UNKNOWN")),
                setup_id=str(pos_meta.get("setup_id", "UNKNOWN")),
                direction=active_pos_before_intrabar.direction,
                planned_entry=float(pos_meta.get("planned_entry", active_pos_before_intrabar.entry_price)),
                planned_sl=float(pos_meta.get("planned_sl", active_pos_before_intrabar.sl_price or 0.0)),
                planned_tp=float(pos_meta.get("planned_tp", active_pos_before_intrabar.tp_price or 0.0)),
                actual_entry=float(pos_meta.get("actual_entry", active_pos_before_intrabar.entry_price)),
                actual_sl=float(pos_meta.get("actual_sl", active_pos_before_intrabar.sl_price or 0.0)),
                actual_tp=float(pos_meta.get("actual_tp", active_pos_before_intrabar.tp_price or 0.0)),
                risk_cash=float(pos_meta.get("risk_cash", 0.0)),
                reward_cash=float(pos_meta.get("reward_cash", 0.0)),
                effective_rr=float(pos_meta.get("effective_rr", 0.0)),
                exit_price=float(ct["exit_price"]),
                gross_pnl=float(round(
                    (ct["exit_price"] - active_pos_before_intrabar.entry_price) * active_pos_before_intrabar.multiplier
                    if active_pos_before_intrabar.direction == "BUY"
                    else (active_pos_before_intrabar.entry_price - ct["exit_price"]) * active_pos_before_intrabar.multiplier,
                    2,
                )),
                net_pnl=float(ct["pnl"]),
                cluster_id=pos_meta.get("cluster_id"),
                meta={
                    "trade_id": ct["trade_id"],
                    "exit_reason": ct["exit_reason"],
                },
            )
            step_events.append(pos_closed_evt)
            self._all_events.append(pos_closed_evt)

        # ---------------------------------------------------------------------
        # 3. Close Phase
        # ---------------------------------------------------------------------
        tf_delta = SUPPORTED_TIMEFRAMES.get(self.context_config.timeframe, pd.Timedelta(minutes=15))
        bar_close_time = ts + tf_delta

        # HTF timeline as-of update
        newly_effective_htf = self.htf_timeline.get_events_as_of(bar_close_time)
        if current_htf_events:
            combined_htf = list(newly_effective_htf) + list(current_htf_events)
        else:
            combined_htf = list(newly_effective_htf)

        # StrategyContext update
        context = self.context_builder.update(
            candle,
            candle_closed=True,
            new_htf_events=combined_htf if combined_htf else None,
        )

        # Regime classification
        regime = self.regime_classifier.update(context)

        # Strategy evaluation via registry
        cands_by_strat = self.strategy_registry.evaluate_enabled(context)

        # Eligibility Gate with cooldown check
        evals = self.eligibility_gate.evaluate_registry_output(
            cands_by_strat,
            context,
            regime,
            self.strategy_registry.profiles,
            cooldown_book=self.cooldown_book,
        )

        # Confluence batch assembly
        confluence_batch = build_confluence_batch(evals, regime, context)

        # Deterministic Selector
        selector_output = self.selector.select(confluence_batch, context)
        decision = selector_output.decision
        audit_rec = selector_output.audit_record
        scorecards = selector_output.scorecards

        self._all_decisions.append(decision)
        if audit_rec is not None:
            self._all_audit_records.append(audit_rec)
        self._all_scorecards.extend(scorecards)

        self._last_context = context
        self._last_evals = evals
        self._last_confluence_batch = confluence_batch
        self._last_cands_by_strat = dict(cands_by_strat)

        # Pending intent generation or final bar handling
        if decision.action == "SELECT":
            assert decision.selected_setup is not None
            assert decision.primary_strategy_id is not None
            setup = decision.selected_setup

            if is_last_bar:
                # Last bar: cannot fill at next open -> cancel
                evt = ExecutionEvent(
                    event_version="1.0.0",
                    event_id=make_execution_event_id(bar_idx, "ORDER_CANCELLED", decision.primary_strategy_id, setup.setup_id),
                    event_type="ORDER_CANCELLED",
                    reason="no_next_bar",
                    symbol=context.symbol,
                    timeframe=context.timeframe,
                    bar_index=bar_idx,
                    bar_time=context.bar_close_time,
                    signal_bar_index=bar_idx,
                    signal_bar_time=context.bar_close_time,
                    decision_id=decision.decision_id,
                    strategy_id=decision.primary_strategy_id,
                    setup_id=setup.setup_id,
                    direction=setup.direction,
                    planned_entry=setup.entry_price,
                    planned_sl=setup.stop_loss,
                    planned_tp=setup.take_profit,
                    cluster_id=setup.evidence_cluster_id,
                    meta={
                        "supporting_strategy_ids": list(decision.supporting_strategy_ids),
                        "evidence_ids": [e.evidence_id for e in setup.evidences] if hasattr(setup, "evidences") else [],
                        "regime": regime.regime,
                    },
                )
                step_events.append(evt)
                self._all_events.append(evt)
                self._pending_intent = None

            else:
                # Valid setup with next bar available: emit ORDER_SELECTED and create PendingExecutionIntent
                evt = ExecutionEvent(
                    event_version="1.0.0",
                    event_id=make_execution_event_id(bar_idx, "ORDER_SELECTED", decision.primary_strategy_id, setup.setup_id),
                    event_type="ORDER_SELECTED",
                    reason="ok",
                    symbol=context.symbol,
                    timeframe=context.timeframe,
                    bar_index=bar_idx,
                    bar_time=context.bar_close_time,
                    signal_bar_index=bar_idx,
                    signal_bar_time=context.bar_close_time,
                    decision_id=decision.decision_id,
                    strategy_id=decision.primary_strategy_id,
                    setup_id=setup.setup_id,
                    direction=setup.direction,
                    planned_entry=setup.entry_price,
                    planned_sl=setup.stop_loss,
                    planned_tp=setup.take_profit,
                    cluster_id=setup.evidence_cluster_id,
                    meta={
                        "supporting_strategy_ids": list(decision.supporting_strategy_ids),
                        "evidence_ids": [e.evidence_id for e in setup.evidences] if hasattr(setup, "evidences") else [],
                        "regime": regime.regime,
                    },
                )
                step_events.append(evt)
                self._all_events.append(evt)

                new_intent = PendingExecutionIntent.from_selection_decision(
                    decision,
                    symbol=context.symbol,
                    timeframe=context.timeframe,
                    min_rr=self.execution_config.min_rr_fallback,
                    meta={
                        "regime": regime.regime,
                        "session": context.session_decision.session_name if context.session_decision else None,
                    },
                )
                self._pending_intent = new_intent
                self._all_intents.append(new_intent)

        # Forced close on last candle if a position is still open
        if is_last_bar and self.kernel.position is not None:
            open_pos_last = self.kernel.position
            pos_meta_last = _deep_thaw(open_pos_last.metadata)
            force_transition = self.kernel.force_close(exec_bar)
            ct_force = force_transition.closed_trade
            assert ct_force is not None

            pos_closed_evt = ExecutionEvent(
                event_version="1.0.0",
                event_id=make_execution_event_id(
                    bar_idx,
                    "POSITION_CLOSED",
                    pos_meta_last.get("strategy_id", "UNKNOWN"),
                    pos_meta_last.get("setup_id", "UNKNOWN"),
                ),
                event_type="POSITION_CLOSED",
                reason="forced_close",
                symbol=pos_meta_last.get("symbol", self.context_config.symbol),
                timeframe=pos_meta_last.get("timeframe", self.context_config.timeframe),
                bar_index=bar_idx,
                bar_time=ts,
                signal_bar_index=int(pos_meta_last.get("signal_bar_index", bar_idx - 1)),
                signal_bar_time=pd.Timestamp(pos_meta_last.get("signal_bar_time", ts)),
                decision_id=str(pos_meta_last.get("decision_id", "")),
                strategy_id=str(pos_meta_last.get("strategy_id", "UNKNOWN")),
                setup_id=str(pos_meta_last.get("setup_id", "UNKNOWN")),
                direction=open_pos_last.direction,
                planned_entry=float(pos_meta_last.get("planned_entry", open_pos_last.entry_price)),
                planned_sl=float(pos_meta_last.get("planned_sl", open_pos_last.sl_price or 0.0)),
                planned_tp=float(pos_meta_last.get("planned_tp", open_pos_last.tp_price or 0.0)),
                actual_entry=float(pos_meta_last.get("actual_entry", open_pos_last.entry_price)),
                actual_sl=float(pos_meta_last.get("actual_sl", open_pos_last.sl_price or 0.0)),
                actual_tp=float(pos_meta_last.get("actual_tp", open_pos_last.tp_price or 0.0)),
                risk_cash=float(pos_meta_last.get("risk_cash", 0.0)),
                reward_cash=float(pos_meta_last.get("reward_cash", 0.0)),
                effective_rr=float(pos_meta_last.get("effective_rr", 0.0)),
                exit_price=float(ct_force["exit_price"]),
                gross_pnl=float(round(
                    (ct_force["exit_price"] - open_pos_last.entry_price) * open_pos_last.multiplier
                    if open_pos_last.direction == "BUY"
                    else (open_pos_last.entry_price - ct_force["exit_price"]) * open_pos_last.multiplier,
                    2,
                )),
                net_pnl=float(ct_force["pnl"]),
                cluster_id=pos_meta_last.get("cluster_id"),
                meta={
                    "trade_id": ct_force["trade_id"],
                    "exit_reason": ct_force["exit_reason"],
                },
            )
            step_events.append(pos_closed_evt)
            self._all_events.append(pos_closed_evt)

        # ---------------------------------------------------------------------
        # 4. Mark-to-Market Valuation
        # ---------------------------------------------------------------------
        equity = self.kernel.mark_to_market(c_close)
        balance = self.kernel.balance

        eq_rec = {
            "bar_index": bar_idx,
            "time": ts.isoformat(),
            "close": c_close,
            "equity": round(equity, 2),
            "balance": round(balance, 2),
        }
        self._equity_curve.append(eq_rec)

        res = StepResult(
            bar_index=bar_idx,
            timestamp=ts,
            decision=decision,
            pending_intent=self._pending_intent,
            execution_events=tuple(step_events),
            kernel_transition_open=open_transition,
            kernel_transition_intrabar=intrabar_transition,
            equity=equity,
            balance=balance,
            position=self.kernel.position,
            scorecards=scorecards,
            audit_record=audit_rec,
        )

        self._last_bar_index = bar_idx
        self._last_candle_payload = curr_payload
        self._last_step_result = res

        return res

    def run(
        self,
        df: pd.DataFrame,
        mode: Optional[CoordinatorMode] = None,
        htf_events: Optional[Sequence[StructureEvent]] = None,
        start_idx: int = 0,
    ) -> CoordinatorResult:
        """
        Execute full deterministic backtest across an OHLCV DataFrame.

        Supports warm-up isolation via `start_idx`: bars before start_idx build context,
        but emit zero trades and are excluded from final trade logs and metrics.
        """
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            raise ValueError("Candle dataframe is empty or not a DataFrame.")

        if len(df) < 2:
            raise ValueError("At least 2 bars are required for execution backtest.")

        # If mode or htf_events explicitly given to run, re-initialize coordinator config
        if mode is not None and mode != self.mode:
            self.mode = mode
            if mode in ("smc_wave1", "smc_confluence"):
                enabled_ids: Optional[tuple[str, ...]] = ("S01", "S05", "S09")
            elif mode == "smc_s01":
                enabled_ids = ("S01",)
            elif mode == "smc_s05":
                enabled_ids = ("S05",)
            elif mode == "smc_s09":
                enabled_ids = ("S09",)
            elif mode == "smc_st_fvg_mss":
                enabled_ids = ("smc_st_fvg_mss",)
            else:
                raise ValueError(f"Invalid mode '{mode}'")
            self.strategy_registry_config = StrategyRegistryConfig(enabled_strategy_ids=enabled_ids)
            strat_list = [
                S01ICT2022Strategy(
                    S01Config(require_displacement=False, sweep_to_mss_max_bars=24)
                ),
                S05BOSOBRetestStrategy(
                    S05Config(
                        require_displacement=False,
                        max_ob_age_bars=75,
                        min_rr=1.0,
                        fallback_rr=2.0,
                    )
                ),
                # Temporary: keep S09 logic but disable session-hour gating.
                S09ICTSilverBulletStrategy(
                    S09Config(use_time_filter=False, require_displacement=False)
                ),
                SMCSupertrendFVGMSSStrategy(
                    SMCSupertrendFVGMSSConfig()
                ),
            ]
            self.strategy_registry = StrategyRegistry(strat_list, config=self.strategy_registry_config)

        if htf_events is not None:
            self.htf_timeline = HTFTimeline(htf_events)

        self.reset()

        if "bar_index" not in df.columns:
            df = df.copy()
            if "index" in df.columns:
                df["bar_index"] = df["index"]
            else:
                df["bar_index"] = list(range(len(df)))

        n_bars = len(df)
        start_i = max(0, min(int(start_idx), n_bars - 1))

        for idx in range(n_bars):
            row = df.iloc[idx]
            is_last = (idx == n_bars - 1)
            
            # During warm-up phase (idx < start_i), suppress pending intents so no trades open
            if idx < start_i:
                self.pending_intent = None

            self.step(row, is_last_bar=is_last)

            # At the boundary between warm-up and analysis range (idx == start_i - 1 or entering start_i)
            if start_i > 0 and idx == start_i - 1:
                self.pending_intent = None
                # Reset execution kernel and output logs so pre-analysis activity is zeroed
                self.kernel = ExecutionKernel(
                    initial_capital=self.initial_capital,
                    lot_size=self.execution_config.lot_size,
                    contract_size=self.execution_config.contract_size,
                    spread_val=self.execution_config.spread_points / 100.0 if hasattr(self.execution_config, "spread_points") else 0.20,
                    commission_per_side=self.execution_config.commission_per_lot * self.execution_config.lot_size if hasattr(self.execution_config, "commission_per_lot") else 0.5,
                    validation_mode="wave1",
                )
                self._equity_curve = []
                self._all_decisions = []
                self._all_intents = []
                self._all_events = []
                self._all_audit_records = []

        # Performance summary metrics
        trades = self.kernel.trades
        total_trades = len(trades)
        wins = [t for t in trades if t.get("pnl", 0.0) > 0]
        losses = [t for t in trades if t.get("pnl", 0.0) < 0]
        win_trades = len(wins)
        loss_trades = len(losses)
        win_rate = round((win_trades / total_trades * 100.0), 2) if total_trades > 0 else 0.0

        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

        # Max drawdown computation
        peak = self.kernel.initial_capital
        max_dd = 0.0
        max_dd_pct = 0.0
        for pt in self._equity_curve:
            eq = pt["equity"]
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd
            if peak > 0:
                dd_pct = (dd / peak) * 100.0
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct

        return CoordinatorResult(
            decisions=tuple(self._all_decisions),
            pending_intents=tuple(self._all_intents),
            execution_events=tuple(self._all_events),
            trades=tuple(trades),
            markers=tuple(self.kernel.markers),
            equity_curve=tuple(self._equity_curve),
            final_equity=round(self._equity_curve[-1]["equity"], 2) if self._equity_curve else self.kernel.initial_capital,
            final_balance=round(self.kernel.balance, 2),
            max_drawdown=round(max_dd, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            total_trades=total_trades,
            win_trades=win_trades,
            loss_trades=loss_trades,
            win_rate=win_rate,
            profit_factor=profit_factor,
            cooldown_snapshot=self.cooldown_book.snapshot(),
            audit_records=tuple(self._all_audit_records),
            scorecards=tuple(self._all_scorecards),
        )

    def build_replay_timeline(
        self,
        df: pd.DataFrame,
        htf_events: Optional[Sequence[StructureEvent]] = None,
        start_idx: int = 0,
        end_idx: Optional[int] = None,
        strategy_filter: Optional[str] = None,
        max_bars: int = 5000,
    ) -> dict[str, Any]:
        """
        Build a chronologically ordered, zero-lookahead replay timeline.

        Executes the backtest sequentially, emitting the complete snapshot of:
        - OHLCV candle at bar N
        - HTF bias state as-of bar N close
        - newly confirmed market structure / liquidity / zones at bar N
        - active pools, active FVGs, active OBs
        - candidate setups generated by strategies at bar N
        - candidate eligibility evaluation with 22-code diagnostic rejection reasons
        - confluence batch & deterministic selector decision
        - portfolio position state and execution events
        - navigation bookmarks (candidates, rejections, fills, events)
        """
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            raise ValueError("Candle dataframe is empty or not a DataFrame.")

        if len(df) < 2:
            raise ValueError("At least 2 bars are required for replay timeline.")

        if strategy_filter is not None and strategy_filter in VALID_COORDINATOR_MODES and strategy_filter != self.mode:
            self.mode = strategy_filter  # type: ignore[assignment]
            if self.mode in ("smc_wave1", "smc_confluence"):
                enabled_ids: Optional[tuple[str, ...]] = ("S01", "S05", "S09")
            elif self.mode == "smc_s01":
                enabled_ids = ("S01",)
            elif self.mode == "smc_s05":
                enabled_ids = ("S05",)
            elif self.mode == "smc_s09":
                enabled_ids = ("S09",)
            elif self.mode == "smc_st_fvg_mss":
                enabled_ids = ("smc_st_fvg_mss",)
            else:
                enabled_ids = ("S01", "S05", "S09")

            self.strategy_registry_config = StrategyRegistryConfig(enabled_strategy_ids=enabled_ids)
            strat_list = [
                S01ICT2022Strategy(
                    S01Config(require_displacement=False, sweep_to_mss_max_bars=24)
                ),
                S05BOSOBRetestStrategy(
                    S05Config(
                        require_displacement=False,
                        max_ob_age_bars=75,
                        min_rr=1.0,
                        fallback_rr=2.0,
                    )
                ),
                S09ICTSilverBulletStrategy(
                    S09Config(use_time_filter=False, require_displacement=False)
                ),
            ]
            self.strategy_registry = StrategyRegistry(strat_list, config=self.strategy_registry_config)

        if htf_events is not None:
            self.htf_timeline = HTFTimeline(htf_events)

        self.reset()

        df_run = df.copy()
        if "bar_index" not in df_run.columns:
            if "index" in df_run.columns:
                df_run["bar_index"] = df_run["index"]
            else:
                df_run["bar_index"] = list(range(len(df_run)))

        # Convert time to datetime if necessary
        if "time" in df_run.columns and len(df_run) > 0:
            sample_t = df_run["time"].iloc[0]
            if isinstance(sample_t, (int, float, np.integer, np.floating)):
                if sample_t > 100_000_000_000:
                    df_run["time"] = pd.to_datetime(df_run["time"], unit="ms", utc=True)
                elif sample_t > 100_000_000:
                    df_run["time"] = pd.to_datetime(df_run["time"], unit="s", utc=True)
            elif not isinstance(sample_t, pd.Timestamp) or sample_t.tzinfo is None:
                t_series = pd.to_datetime(df_run["time"])
                if t_series.dt.tz is None:
                    df_run["time"] = t_series.dt.tz_localize("UTC")
                else:
                    df_run["time"] = t_series.dt.tz_convert("UTC")

        n_bars = len(df_run)
        start_i = max(0, int(start_idx))
        target_end_i = min(n_bars, int(end_idx)) if end_idx is not None else n_bars
        timeline_end_i = min(target_end_i, start_i + max_bars)

        timeline_bars: list[dict[str, Any]] = []
        bookmark_candidates: list[int] = []
        bookmark_rejections: list[int] = []
        bookmark_fills: list[int] = []
        bookmark_events: list[int] = []

        total_candidates_count = 0
        total_eligible_count = 0
        total_rejected_count = 0

        for idx in range(target_end_i):
            row = df_run.iloc[idx]
            is_last = (idx == n_bars - 1)

            # During warm-up phase (idx < start_i), suppress pending intents so no trades open
            if idx < start_i:
                self._pending_intent = None

            step_res = self.step(row, is_last_bar=is_last)

            # At boundary before analysis range starts, reset execution kernel and logs
            if start_i > 0 and idx == start_i - 1:
                self._pending_intent = None
                self.kernel = ExecutionKernel(
                    initial_capital=self.initial_capital,
                    lot_size=self.execution_config.lot_size,
                    contract_size=self.execution_config.contract_size,
                    spread_val=self.execution_config.spread_points / 100.0 if hasattr(self.execution_config, "spread_points") else 0.20,
                    commission_per_side=self.execution_config.commission_per_lot * self.execution_config.lot_size if hasattr(self.execution_config, "commission_per_lot") else 0.5,
                    validation_mode="wave1",
                )
                self._equity_curve = []
                self._all_decisions = []
                self._all_intents = []
                self._all_events = []
                self._all_audit_records = []
                continue

            # Only serialize bars within [start_i, timeline_end_i)
            if idx < start_i or idx >= timeline_end_i:
                continue

            bar_idx = int(step_res.bar_index)
            ts = step_res.timestamp
            ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            c_open = float(row["open"])
            c_high = float(row["high"])
            c_low = float(row["low"])
            c_close = float(row["close"])
            c_vol = float(row.get("volume", row.get("tick_volume", 0.0)))

            ctx = self._last_context
            evals = self._last_evals
            cb = self._last_confluence_batch
            decision = step_res.decision

            # 1. HTF Bias
            htf_bias_dict = None
            if ctx is not None and ctx.htf_bias is not None:
                hb = ctx.htf_bias
                htf_bias_dict = {
                    "bias": str(hb.bias),
                    "confidence": float(getattr(hb, "confidence", 1.0)),
                    "effective_time": hb.as_of.isoformat() if hasattr(hb, "as_of") and hb.as_of is not None else None,
                    "source_event_type": str(hb.source_event_type) if hb.source_event_type else None,
                    "source_event_index": int(hb.source_event_index) if hb.source_event_index is not None else None,
                    "source_event_direction": str(hb.source_event_direction) if hb.source_event_direction else None,
                    "reason": getattr(hb, "reason", None),
                    "pending_reversal": getattr(hb, "pending_reversal", None),
                    "pending_reversal_event_type": getattr(hb, "pending_reversal_event_type", None),
                    "pending_reversal_event_index": getattr(hb, "pending_reversal_event_index", None),
                    "pending_reversal_event_time": hb.pending_reversal_event_time.isoformat() if getattr(hb, "pending_reversal_event_time", None) is not None else None,
                    "confirmed_by_bos": bool(getattr(hb, "confirmed_by_bos", False)),
                }

            # 2. Newly confirmed events at this exact bar
            new_sweeps = []
            new_structures = []
            new_fvgs = []
            new_obs = []

            if ctx is not None:
                for s in ctx.recent_sweeps:
                    if int(s.confirmed_at) == bar_idx:
                        new_sweeps.append(s.to_dict() if hasattr(s, "to_dict") else {
                            "index": int(s.index),
                            "direction": str(s.direction),
                            "pool_kind": str(s.pool_kind),
                            "pool_price": float(s.pool_price),
                            "price_wick": float(s.price_wick),
                            "close_price": float(s.close_price),
                            "confirmed_at": int(s.confirmed_at),
                            "valid": bool(getattr(s, "valid", True)),
                        })

                for st in ctx.recent_structures:
                    if int(st.index) == bar_idx:
                        new_structures.append({
                            "index": int(st.index),
                            "time": st.time.isoformat() if hasattr(st.time, "isoformat") else str(st.time),
                            "event_type": str(st.event_type),
                            "direction": str(st.direction),
                            "broken_swing_index": int(st.broken_swing_index),
                            "broken_swing_price": float(st.broken_swing_price),
                            "close_price": float(st.close_price),
                            "displacement": bool(st.displacement),
                            "mode": str(st.mode),
                            "structure_leg_id": st.structure_leg_id,
                        })

                for f in ctx.active_fvgs:
                    if int(f.confirmed_at) == bar_idx:
                        new_fvgs.append({
                            "index": int(f.index),
                            "time": f.time.isoformat() if hasattr(f.time, "isoformat") else str(f.time),
                            "direction": str(f.direction),
                            "top": float(f.top),
                            "bottom": float(f.bottom),
                            "confirmed_at": int(f.confirmed_at),
                            "structure_leg_id": f.structure_leg_id,
                        })

                for o in ctx.active_obs:
                    ob_conf = int(getattr(o, "created_at", getattr(o, "source_event_index", getattr(o, "confirmed_at", o.index))))
                    if ob_conf == bar_idx:
                        ob_d = o.to_dict() if hasattr(o, "to_dict") else {
                            "index": int(o.index),
                            "direction": str(o.direction),
                            "quality": str(o.quality),
                        }
                        ob_d["top"] = float(getattr(o, "high", getattr(o, "top", 0.0)))
                        ob_d["bottom"] = float(getattr(o, "low", getattr(o, "bottom", 0.0)))
                        ob_d["confirmed_at"] = ob_conf
                        new_obs.append(ob_d)

            # 3. Active state
            active_fvgs = []
            active_obs = []
            active_pools = []
            recent_swings = []

            if ctx is not None:
                for f in ctx.active_fvgs:
                    if int(f.confirmed_at) <= bar_idx:
                        is_filled = bool(f.filled and f.filled_at is not None and f.filled_at <= bar_idx)
                        active_fvgs.append({
                            "index": int(f.index),
                            "time": f.time.isoformat() if hasattr(f.time, "isoformat") else str(f.time),
                            "direction": str(f.direction),
                            "top": float(f.top),
                            "bottom": float(f.bottom),
                            "confirmed_at": int(f.confirmed_at),
                            "filled": is_filled,
                            "filled_at": int(f.filled_at) if is_filled else None,
                            "structure_leg_id": f.structure_leg_id,
                        })

                for o in ctx.active_obs:
                    ob_conf = int(getattr(o, "created_at", getattr(o, "source_event_index", getattr(o, "confirmed_at", o.index))))
                    if ob_conf <= bar_idx:
                        ob_d = o.to_dict() if hasattr(o, "to_dict") else {
                            "index": int(o.index),
                            "direction": str(o.direction),
                            "quality": str(o.quality),
                        }
                        ob_d["top"] = float(getattr(o, "high", getattr(o, "top", 0.0)))
                        ob_d["bottom"] = float(getattr(o, "low", getattr(o, "bottom", 0.0)))
                        ob_d["confirmed_at"] = ob_conf
                        ob_d["age_bars"] = bar_idx - int(o.index)
                        active_obs.append(ob_d)

                for p in ctx.active_pools:
                    if int(p.created_at) <= bar_idx and getattr(p, "valid", True):
                        active_pools.append(p.to_dict() if hasattr(p, "to_dict") else {
                            "kind": str(p.kind),
                            "price": float(p.price),
                            "created_at": int(p.created_at),
                            "confirmed_at": int(p.confirmed_at),
                            "swept": bool(p.swept),
                            "valid": bool(p.valid),
                        })

                for s in ctx.recent_swings[-15:]:
                    if int(s.confirmed_at) <= bar_idx:
                        recent_swings.append({
                            "index": int(s.index),
                            "time": s.time.isoformat() if hasattr(s.time, "isoformat") else str(s.time),
                            "price": float(s.price),
                            "kind": str(s.kind),
                            "mode": str(s.mode),
                            "confirmed_at": int(s.confirmed_at),
                        })

            # 4. Candidates & Evaluations
            cand_list: list[dict[str, Any]] = []
            has_rejection = False

            if evals:
                for ev in evals:
                    c = ev.candidate
                    total_candidates_count += 1

                    if decision.action == "SELECT" and decision.selected_setup and decision.selected_setup.setup_id == c.setup_id:
                        c_status = "selected"
                        total_eligible_count += 1
                    elif ev.status == "ELIGIBLE":
                        c_status = "eligible"
                        total_eligible_count += 1
                    else:
                        c_status = "rejected"
                        total_rejected_count += 1
                        has_rejection = True

                    diag_reasons = [
                        map_diagnostic_rejection_code(r, candidate=c, context=ctx, details=ev.details)
                        for r in ev.rejection_reasons
                    ]
                    if c_status == "rejected" and not diag_reasons:
                        diag_reasons = ["selector_rejected"]

                    cand_dict = {
                        "setup_id": str(c.setup_id),
                        "strategy_id": str(c.strategy_id),
                        "direction": str(c.direction),
                        "entry_price": float(c.entry_price),
                        "stop_loss": float(c.stop_loss),
                        "take_profit": float(c.take_profit),
                        "planned_rr": float(c.planned_rr),
                        "cluster_id": str(c.evidence_cluster_id),
                        "bar_index": int(c.bar_index),
                        "expiry_bar": int(c.expiry_bar),
                        "status": c_status,
                        "rejection_reasons": list(ev.rejection_reasons),
                        "diagnostic_reasons": diag_reasons,
                        "diagnostic_details": [
                            {
                                "code": code,
                                "description_en": DIAGNOSTIC_REJECTION_DESCRIPTIONS.get(code, {}).get("en", code),
                                "description_vi": DIAGNOSTIC_REJECTION_DESCRIPTIONS.get(code, {}).get("vi", code),
                            }
                            for code in diag_reasons
                        ],
                        "evidences": [
                            e.to_dict() if hasattr(e, "to_dict") else dict(e)
                            for e in getattr(c, "evidences", ())
                        ],
                    }
                    cand_list.append(cand_dict)

            # 5. Confluence
            confluence_info = None
            if cb is not None:
                strats_by_dir: dict[str, list[str]] = {}
                for ev in evals:
                    strats_by_dir.setdefault(ev.candidate.direction, []).append(ev.candidate.strategy_id)

                aligned = []
                for d, s_list in strats_by_dir.items():
                    if len(set(s_list)) >= 2:
                        aligned = list(sorted(set(s_list)))
                        break

                confluence_info = {
                    "cluster_count": len(getattr(cb, "all_clusters", getattr(cb, "eligible_clusters", ()))),
                    "direction_conflict": bool(cb.direction_conflict is not None),
                    "aligned_strategies": aligned,
                    "agreement_count": len(aligned),
                }

            # 6. Selector Output
            selector_info = {
                "action": str(decision.action),
                "primary_strategy_id": str(decision.primary_strategy_id) if decision.primary_strategy_id else None,
                "selected_setup_id": str(decision.selected_setup.setup_id) if decision.selected_setup else None,
                "rejection_reason": str(getattr(decision, "reason", getattr(decision, "rejection_reason", "ok"))),
            }

            # 7. Portfolio Position
            pos_dict = None
            if self.kernel.position is not None:
                kp = self.kernel.position
                lot_size = float(getattr(kp, "size", getattr(kp, "lot_size", kp.metadata.get("lot_size", getattr(self.execution_config, "lot_size", 0.1)))))
                entry_bar = int(getattr(kp, "entry_bar", kp.metadata.get("entry_bar", kp.entry_timestamp)))
                pos_dict = {
                    "direction": str(kp.direction),
                    "entry_price": float(kp.entry_price),
                    "sl_price": float(kp.sl_price) if kp.sl_price is not None else None,
                    "tp_price": float(kp.tp_price) if kp.tp_price is not None else None,
                    "size": lot_size,
                    "entry_bar": entry_bar,
                    "duration_bars": max(0, bar_idx - entry_bar),
                    "unrealized_pnl": round(
                        (c_close - kp.entry_price) * kp.multiplier if kp.direction == "BUY"
                        else (kp.entry_price - c_close) * kp.multiplier,
                        2
                    ),
                }

            # 8. Execution Events
            exec_events = [e.to_dict() for e in step_res.execution_events]
            has_fill_event = any(e["event_type"] in ("ORDER_FILLED", "POSITION_CLOSED") for e in exec_events)

            # Bookmarks
            if cand_list:
                bookmark_candidates.append(bar_idx)
            if has_rejection:
                bookmark_rejections.append(bar_idx)
            if has_fill_event:
                bookmark_fills.append(bar_idx)
            if new_sweeps or new_structures or new_fvgs or new_obs:
                bookmark_events.append(bar_idx)

            # S1 Strategy Diagnostics & HTF POI
            s01_state = "WAIT_HTF_BIAS"
            s01_rej = None
            s01_poi = None
            try:
                s01_inst = self.strategy_registry.get_strategy("S01")
                if s01_inst is not None and hasattr(s01_inst, "current_state"):
                    s01_state = s01_inst.current_state.value if hasattr(s01_inst.current_state, "value") else str(s01_inst.current_state)
                    s01_rej = getattr(s01_inst, "last_rejection_reason", None)
                    s01_poi = getattr(s01_inst, "active_poi", None)
            except Exception:
                pass

            # S05 Strategy Diagnostics
            s05_state = "WAIT_HTF_BIAS"
            s05_rej = None
            s05_ob = None
            try:
                s05_inst = self.strategy_registry.get_strategy("S05")
                if s05_inst is not None and hasattr(s05_inst, "current_state"):
                    s05_state = s05_inst.current_state.value if hasattr(s05_inst.current_state, "value") else str(s05_inst.current_state)
                    s05_rej = getattr(s05_inst, "last_rejection_reason", None)
                    s05_ob = getattr(s05_inst, "active_htf_ob", None)
            except Exception:
                pass

            active_pois = getattr(ctx, "active_htf_pois", ()) if ctx else ()
            primary_poi = s01_poi or (active_pois[0] if active_pois else None)
            poi_dict = primary_poi.to_dict() if primary_poi and hasattr(primary_poi, "to_dict") else None
            poi_status = getattr(primary_poi, "status", "NONE").upper() if primary_poi else "NONE"
            poi_touch = "NONE"
            if primary_poi is not None:
                p_top = getattr(primary_poi, "top", 0.0)
                p_bot = getattr(primary_poi, "bottom", 0.0)
                p_dir = getattr(primary_poi, "direction", "")
                if c_low <= p_top and c_high >= p_bot:
                    if (p_dir == "bullish" and c_open > p_top and c_close > p_top) or (p_dir == "bearish" and c_open < p_bot and c_close < p_bot):
                        poi_touch = "WICK_TOUCH"
                    else:
                        poi_touch = "BODY_ENTRY"
                elif getattr(primary_poi, "touch_count", 0) > 0:
                    poi_touch = "BODY_ENTRY"

            bar_payload = {
                "bar_index": bar_idx,
                "time": ts_str,
                "datetime_str": ts_str.replace("T", " ")[:19] if "T" in ts_str else str(ts_str)[:19],
                "is_warmup": (idx < start_i),
                "is_analysis": (idx >= start_i),
                "is_displayed": True,
                "open": c_open,
                "high": c_high,
                "low": c_low,
                "close": c_close,
                "volume": c_vol,
                "htf_bias": htf_bias_dict,
                "new_events": {
                    "sweeps": new_sweeps,
                    "structures": new_structures,
                    "fvgs": new_fvgs,
                    "obs": new_obs,
                },
                "active_state": {
                    "htf_bias": (htf_bias_dict.get("bias", "NEUTRAL").upper()) if htf_bias_dict else "NEUTRAL",
                    "htf_bias_status": (
                        "REVERSAL_PENDING" if (htf_bias_dict and (htf_bias_dict.get("pending_reversal") or htf_bias_dict.get("reason") == "choch_reversal_pending"))
                        else ("CONFIRMED" if (htf_bias_dict and htf_bias_dict.get("bias") in {"bullish", "bearish", "BULLISH", "BEARISH"}) else "UNCONFIRMED")
                    ),
                    "htf_pending_reversal": (htf_bias_dict.get("pending_reversal").upper()) if (htf_bias_dict and htf_bias_dict.get("pending_reversal")) else "NONE",
                    "htf_choch_pending": (f"#{htf_bias_dict.get('pending_reversal_event_index')}") if (htf_bias_dict and htf_bias_dict.get("pending_reversal_event_index") is not None) else "NONE",
                    "htf_source_event": (f"{htf_bias_dict.get('source_event_type', 'BOS')} #{htf_bias_dict.get('source_event_index')}") if htf_bias_dict and htf_bias_dict.get("source_event_index") is not None else None,
                    "htf_bias_effective_time": htf_bias_dict.get("effective_time") if htf_bias_dict else None,
                    "htf_poi": poi_dict,
                    "poi_status": poi_status,
                    "poi_touch": poi_touch,
                    "s1_state": s01_state,
                    "s05_state": s05_state,
                    "s05_rejection_reason": s05_rej,
                    "s05_ob": poi_dict if s05_ob else None,
                    "rejection_reason": s01_rej or s05_rej or selector_info.get("rejection_reason"),
                    "active_htf_pois": [p.to_dict() if hasattr(p, "to_dict") else dict(p) for p in active_pois],
                    "fvgs": active_fvgs,
                    "obs": active_obs,
                    "pools": active_pools,
                    "liquidity_pools": active_pools,
                    "swings": recent_swings,
                    "recent_swings": recent_swings,
                },
                "candidates": cand_list,
                "candidate_setups": cand_list,
                "confluence": confluence_info,
                "selector": selector_info,
                "cooldown": self.cooldown_book.snapshot(),
                "position": pos_dict,
                "execution_events": exec_events,
                "portfolio": {
                    "equity": round(step_res.equity, 2),
                    "cash": round(step_res.balance, 2),
                    "active_position": pos_dict,
                    "fills_this_bar": [e for e in exec_events if e.get("event_type") == "ORDER_FILLED"],
                    "exits_this_bar": [e for e in exec_events if e.get("event_type") == "POSITION_CLOSED"],
                },
                "equity": round(step_res.equity, 2),
                "balance": round(step_res.balance, 2),
            }
            timeline_bars.append(bar_payload)

        # Performance summary metrics
        trades = self.kernel.trades
        total_trades = len(trades)
        wins = [t for t in trades if t.get("pnl", 0.0) > 0]
        losses = [t for t in trades if t.get("pnl", 0.0) < 0]
        win_trades = len(wins)
        loss_trades = len(losses)
        win_rate = round((win_trades / total_trades * 100.0), 2) if total_trades > 0 else 0.0
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        total_fills_count = sum(len(b["portfolio"]["fills_this_bar"]) for b in timeline_bars)

        return {
            "status": "success",
            "symbol": str(self.context_config.symbol),
            "timeframe": str(self.context_config.timeframe),
            "strategy": str(self.mode),
            "total_bars": len(timeline_bars),
            "start_bar_index": start_i,
            "end_bar_index": timeline_end_i - 1 if timeline_bars else start_i,
            "bookmarks": {
                "candidates": bookmark_candidates,
                "rejections": bookmark_rejections,
                "fills": bookmark_fills,
                "events": bookmark_events,
            },
            "timeline": timeline_bars,
            "summary": {
                "total_bars": len(timeline_bars),
                "total_candidates": total_candidates_count,
                "total_eligible": total_eligible_count,
                "total_rejected": total_rejected_count,
                "total_trades": total_trades,
                "total_fills": total_fills_count,
                "win_trades": win_trades,
                "loss_trades": loss_trades,
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "final_balance": round(self.kernel.balance, 2),
                "final_equity": round(self.kernel.mark_to_market(float(df_run['close'].iloc[target_end_i - 1])), 2) if target_end_i > 0 else self._initial_capital,
            },
        }


# =============================================================================
# parse_htf_event_payload: JSON dict → StructureEvent
# =============================================================================

def parse_htf_event_payload(payload: dict) -> "StructureEvent":
    """
    Convert a raw JSON dict (from API request payload) into a validated StructureEvent.

    Required keys: index, time, event_type, direction, broken_swing_price, close_price.
    Optional keys: broken_swing_index, displacement, mode, confirmed_swing_at,
                   body_size, atr_value, break_type, structure_leg_id.

    Timestamps must be timezone-aware or convertible with UTC offset.
    NaN/Inf numeric values are rejected.
    """
    import math as _math
    from smc.models import StructureEvent as _StructureEvent

    if not isinstance(payload, dict):
        raise TypeError(f"Expected dict payload, got {type(payload).__name__}")

    # --- Required fields ---
    required = ("index", "time", "event_type", "direction", "broken_swing_price", "close_price")
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"HTF event dict missing required keys: {missing}")

    # index
    try:
        idx = int(payload["index"])
    except (ValueError, TypeError) as e:
        raise ValueError(f"HTF event 'index' must be an integer: {payload['index']!r}") from e
    if idx < 0:
        raise ValueError(f"HTF event 'index' must be non-negative, got: {idx}")

    # time — parse timezone-aware
    raw_time = payload["time"]
    try:
        ts = _parse_timezone_aware_timestamp(raw_time, name="htf_event.time")
    except Exception as e:
        raise ValueError(f"HTF event 'time' is not a valid timezone-aware timestamp: {raw_time!r} — {e}") from e

    # event_type
    event_type = str(payload["event_type"])
    if event_type not in ("BOS", "CHoCH"):
        raise ValueError(f"HTF event 'event_type' must be 'BOS' or 'CHoCH', got: {event_type!r}")

    # direction
    direction = str(payload["direction"])
    if direction not in ("bullish", "bearish"):
        raise ValueError(f"HTF event 'direction' must be 'bullish' or 'bearish', got: {direction!r}")

    # broken_swing_price
    try:
        bsp = float(payload["broken_swing_price"])
    except (ValueError, TypeError) as e:
        raise ValueError(f"HTF event 'broken_swing_price' must be numeric: {payload['broken_swing_price']!r}") from e
    if not _math.isfinite(bsp):
        raise ValueError(f"HTF event 'broken_swing_price' must be finite, got: {bsp}")

    # close_price
    try:
        cp = float(payload["close_price"])
    except (ValueError, TypeError) as e:
        raise ValueError(f"HTF event 'close_price' must be numeric: {payload['close_price']!r}") from e
    if not _math.isfinite(cp):
        raise ValueError(f"HTF event 'close_price' must be finite, got: {cp}")

    # --- Optional fields ---
    broken_swing_index = int(payload.get("broken_swing_index", 0))
    displacement = bool(payload.get("displacement", False))
    mode_val = str(payload.get("mode", "swing"))
    if mode_val not in ("swing", "internal"):
        raise ValueError(f"HTF event 'mode' must be 'swing' or 'internal', got: {mode_val!r}")
    confirmed_swing_at = int(payload.get("confirmed_swing_at", 0))

    body_size_raw = payload.get("body_size", 0.0)
    try:
        body_size = float(body_size_raw)
    except (ValueError, TypeError):
        body_size = 0.0
    if not _math.isfinite(body_size):
        raise ValueError(f"HTF event 'body_size' must be finite, got: {body_size}")

    atr_raw = payload.get("atr_value", 0.0)
    try:
        atr_value = float(atr_raw)
    except (ValueError, TypeError):
        atr_value = 0.0
    if not _math.isfinite(atr_value):
        raise ValueError(f"HTF event 'atr_value' must be finite, got: {atr_value}")

    break_type = str(payload.get("break_type", "close"))
    structure_leg_id = payload.get("structure_leg_id", None)
    if structure_leg_id is not None:
        structure_leg_id = str(structure_leg_id)

    return _StructureEvent(
        index=idx,
        time=ts,
        event_type=event_type,  # type: ignore[arg-type]
        direction=direction,    # type: ignore[arg-type]
        broken_swing_index=broken_swing_index,
        broken_swing_price=bsp,
        close_price=cp,
        displacement=displacement,
        mode=mode_val,          # type: ignore[arg-type]
        confirmed_swing_at=confirmed_swing_at,
        body_size=body_size,
        atr_value=atr_value,
        break_type=break_type,
        structure_leg_id=structure_leg_id,
    )


# Aliases for architectural clarity
SMCBacktestAdapter = SMCBacktestCoordinator

__all__ = [
    "HTFTimeline",
    "StepResult",
    "CoordinatorResult",
    "CoordinatorMode",
    "VALID_COORDINATOR_MODES",
    "DIAGNOSTIC_REJECTION_DESCRIPTIONS",
    "map_diagnostic_rejection_code",
    "SMCBacktestCoordinator",
    "SMCBacktestAdapter",
    "parse_htf_event_payload",
]
