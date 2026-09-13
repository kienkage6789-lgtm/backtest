"""
smc/engine/errors.py
====================
Domain exception hierarchy for the SMC Strategy Template Protocol & Deterministic Registry (T53.3).

This module is a leaf dependency:
- It does NOT import protocol.py, registry.py, or models.py.
- All domain exceptions derive via single-inheritance from StrategyRegistryError.
"""

from __future__ import annotations


class StrategyRegistryError(Exception):
    """Base exception for all strategy registry and template protocol errors."""
    pass


class StrategyValidationError(StrategyRegistryError):
    """Raised when a strategy template, strategy_id, or configuration fails contract validation."""
    pass


class DuplicateStrategyError(StrategyRegistryError):
    """Raised when duplicate strategy IDs are registered or configured."""
    pass


class UnknownStrategyError(StrategyRegistryError):
    """Raised when querying a strategy ID that does not exist in the registry."""
    pass


class StrategyStateError(StrategyRegistryError):
    """Raised when context dispatch is non-monotonic, conflicting, or registry is in a poisoned state."""
    pass


class InvalidStrategyOutputError(StrategyRegistryError):
    """Raised when strategy evaluate() returns an invalid container or non-compliant candidate setups."""
    pass


class StrictModelTypeError(TypeError, ValueError):
    """Exception raised when an invalid type is passed to a strict model field."""
    pass


__all__ = [
    "StrategyRegistryError",
    "StrategyValidationError",
    "DuplicateStrategyError",
    "UnknownStrategyError",
    "StrategyStateError",
    "InvalidStrategyOutputError",
    "StrictModelTypeError",
]
