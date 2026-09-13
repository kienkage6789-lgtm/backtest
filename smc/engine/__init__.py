"""
smc/engine
==========
Domain package for the Multi-Strategy Confluence & Selection Engine (T53).
"""

from .errors import (
    DuplicateStrategyError,
    InvalidStrategyOutputError,
    StrategyRegistryError,
    StrategyStateError,
    StrategyValidationError,
    UnknownStrategyError,
)
from .models import (
    BiasStateSnapshot,
    CandidateSetup,
    EvidenceRef,
    FairValueGapSnapshot,
    HTFPOISnapshot,
    LiquidityPoolSnapshot,
    LiquiditySweepSnapshot,
    MarketRegime,
    OrderBlockSnapshot,
    SelectionDecision,
    SessionDecisionSnapshot,
    StrategyContext,
    StrategyEvaluation,
    StrategyProfile,
    StrictModelTypeError,
    StructureEventSnapshot,
    SwingPointSnapshot,
    make_cluster_id,
    make_decision_id,
    make_evidence_id,
    make_setup_id,
)
from .context import (
    SUPPORTED_TIMEFRAMES,
    ContextBuilderConfig,
    StrategyContextBuilder,
    build_strategy_contexts,
    validate_as_of_evidence,
)
from .protocol import (
    StrategyTemplate,
    validate_strategy_id,
    validate_strategy_template,
)
from .registry import (
    StrategyRegistry,
    StrategyRegistryConfig,
)
from .strategies import (
    S01Config,
    S01ICT2022Strategy,
    S05Config,
    S05BOSOBRetestStrategy,
    S09Config,
    S09ICTSilverBulletStrategy,
    SMCSupertrendFVGMSSConfig,
    SMCSupertrendFVGMSSStrategy,
    SMCSupertrendMacroState,
)
from .regime import (
    RegimeClassifierConfig,
    MarketRegimeClassifier,
    classify_market_regimes,
)
from .eligibility import (
    REGIME_MATRIX,
    get_regime_matrix_score,
    EligibilityGate,
)
from .confluence import (
    EvidenceCluster,
    DirectionConflict,
    ConfluenceBatch,
    EvidenceDeduplicator,
    DirectionConflictDetector,
    build_confluence_batch,
)
from .selector import (
    SelectorConfig,
    ClusterScorecard,
    SelectorOutput,
    DeterministicStrategySelector,
    select_strategy,
    compute_regime_score,
    compute_setup_score,
    compute_context_score,
    compute_execution_score,
    compute_total_score,
)
from .telemetry import (
    SelectionAuditRecord,
    aggregate_selection_telemetry,
)
from .execution import (
    ExecutionConfig,
    PendingExecutionIntent,
    FillValidationResult,
    ExecutionEvent,
    CooldownBook,
    CooldownInterval,
    validate_fill,
    make_execution_event_id,
    make_execution_intent_id,
)
from .backtest_adapter import (
    HTFTimeline,
    StepResult,
    CoordinatorResult,
    CoordinatorMode,
    VALID_COORDINATOR_MODES,
    SMCBacktestCoordinator,
    SMCBacktestAdapter,
)

__all__ = [
    # Domain models & snapshots (T53.1)
    "EvidenceRef",
    "StrategyContext",
    "StrategyProfile",
    "CandidateSetup",
    "MarketRegime",
    "StrategyEvaluation",
    "SelectionDecision",
    "SwingPointSnapshot",
    "StructureEventSnapshot",
    "FairValueGapSnapshot",
    "OrderBlockSnapshot",
    "LiquidityPoolSnapshot",
    "LiquiditySweepSnapshot",
    "SessionDecisionSnapshot",
    "BiasStateSnapshot",
    "HTFPOISnapshot",
    "StrictModelTypeError",
    "make_evidence_id",
    "make_cluster_id",
    "make_setup_id",
    "make_decision_id",
    # Context Builder (T53.2)
    "ContextBuilderConfig",
    "StrategyContextBuilder",
    "build_strategy_contexts",
    "SUPPORTED_TIMEFRAMES",
    "validate_as_of_evidence",
    # Protocol & Registry (T53.3)
    "StrategyTemplate",
    "validate_strategy_id",
    "validate_strategy_template",
    "StrategyRegistryConfig",
    "StrategyRegistry",
    "StrategyRegistryError",
    "StrategyValidationError",
    "DuplicateStrategyError",
    "UnknownStrategyError",
    "StrategyStateError",
    "InvalidStrategyOutputError",
    # Strategy Templates (T53.4, T53.5, T53.6)
    "S01Config",
    "S01ICT2022Strategy",
    "S05Config",
    "S05BOSOBRetestStrategy",
    "S09Config",
    "S09ICTSilverBulletStrategy",
    "SMCSupertrendFVGMSSConfig",
    "SMCSupertrendFVGMSSStrategy",
    "SMCSupertrendMacroState",
    # Regime, Gate & Confluence (T53.7)
    "RegimeClassifierConfig",
    "MarketRegimeClassifier",
    "classify_market_regimes",
    "REGIME_MATRIX",
    "get_regime_matrix_score",
    "EligibilityGate",
    "EvidenceCluster",
    "DirectionConflict",
    "ConfluenceBatch",
    "EvidenceDeduplicator",
    "DirectionConflictDetector",
    "build_confluence_batch",
    # Selector & Telemetry (T53.8)
    "SelectorConfig",
    "ClusterScorecard",
    "SelectorOutput",
    "DeterministicStrategySelector",
    "select_strategy",
    "compute_regime_score",
    "compute_setup_score",
    "compute_context_score",
    "compute_execution_score",
    "compute_total_score",
    "SelectionAuditRecord",
    "aggregate_selection_telemetry",
    # Execution & Backtest Integration (T53.9)
    "ExecutionConfig",
    "PendingExecutionIntent",
    "FillValidationResult",
    "ExecutionEvent",
    "CooldownBook",
    "CooldownInterval",
    "validate_fill",
    "make_execution_event_id",
    "make_execution_intent_id",
    # Backtest Coordinator & Adapter (T53.9.3)
    "HTFTimeline",
    "StepResult",
    "CoordinatorResult",
    "CoordinatorMode",
    "VALID_COORDINATOR_MODES",
    "SMCBacktestCoordinator",
    "SMCBacktestAdapter",
]
