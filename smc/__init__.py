"""
Smart Money Concepts (SMC) Module
"""

__version__ = "0.2.0"

from smc.models import SwingPoint, StructureEvent, FairValueGap, OrderBlock
from smc.strategy import run_smc_strategy, SMCStrategyConfig, SMCStrategyResult, SMCFunnelStats
