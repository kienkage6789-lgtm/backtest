"""
Smart Money Concepts (SMC) Module
"""

__version__ = "0.2.0"

from smc.models import (
    SwingPoint, StructureEvent, FairValueGap, OrderBlock,
    LiquidityPool, LiquiditySweep, Signal, SessionWindow,
    SessionDecision, BiasState, HTFPOI,
)
from smc.liquidity import detect_liquidity_pools, detect_liquidity_sweeps, detect_liquidity, LiquidityTracker
from smc.context import (
    SessionFilter, HTFBiasTracker, map_htf_bias_to_ltf,
    evaluate_sessions_batch, evaluate_sessions, check_killzone_signal,
    LONDON_KILLZONE, NEWYORK_KILLZONE, ASIAN_RANGE, DEFAULT_SESSIONS,
)
from smc.strategy import run_smc_strategy, SMCStrategyConfig, SMCStrategyResult, SMCFunnelStats
