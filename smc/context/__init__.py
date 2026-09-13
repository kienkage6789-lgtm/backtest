"""
smc/context package
===================
Context Layer for Smart Money Concepts (SMC).

Includes:
1. KillZone & Session Filtering (London, New York, Asian, Custom, Overnight).
2. Higher Timeframe (HTF) Bias Adapter (zero-lookahead as-of timestamp mapping).
"""

from smc.models import SessionWindow, SessionDecision, Signal, BiasState

from smc.context.session import (
    LONDON_KILLZONE,
    NEWYORK_KILLZONE,
    ASIAN_RANGE,
    DEFAULT_SESSIONS,
    evaluate_session_window,
    evaluate_sessions,
    evaluate_sessions_batch,
    check_killzone_signal,
    SessionFilter,
)

from smc.context.htf_bias import (
    get_htf_bias_at,
    map_htf_bias_to_ltf,
    HTFBiasTracker,
)

from smc.context.htf_poi import (
    HTFPOITracker,
)

__all__ = [
    "SessionWindow",
    "SessionDecision",
    "Signal",
    "BiasState",
    "LONDON_KILLZONE",
    "NEWYORK_KILLZONE",
    "ASIAN_RANGE",
    "DEFAULT_SESSIONS",
    "evaluate_session_window",
    "evaluate_sessions",
    "evaluate_sessions_batch",
    "check_killzone_signal",
    "SessionFilter",
    "get_htf_bias_at",
    "map_htf_bias_to_ltf",
    "HTFBiasTracker",
    "HTFPOITracker",
]
