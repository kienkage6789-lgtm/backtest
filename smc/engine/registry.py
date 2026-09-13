"""
smc/engine/registry.py
======================
Deterministic, immutable StrategyRegistry and StrategyRegistryConfig for SMC Strategy Templates (T53.3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Optional

from smc.engine.errors import (
    DuplicateStrategyError,
    InvalidStrategyOutputError,
    StrategyRegistryError,
    StrategyStateError,
    StrategyValidationError,
    UnknownStrategyError,
)
from smc.engine.models import CandidateSetup, StrategyContext, StrategyProfile
from smc.engine.protocol import (
    StrategyTemplate,
    validate_strategy_id,
    validate_strategy_template,
)


@dataclass(frozen=True)
class StrategyRegistryConfig:
    """
    Immutable configuration for StrategyRegistry controlling enabled/disabled strategies.

    Attributes:
        enabled_strategy_ids:
            - None: All registered strategies are enabled (default).
            - (): Empty tuple disables all strategies.
            - tuple[str, ...]: Strictly only the specified strategy IDs are enabled.
    """

    enabled_strategy_ids: Optional[tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if self.enabled_strategy_ids is not None:
            if isinstance(self.enabled_strategy_ids, (str, bytes)):
                raise StrategyValidationError(
                    "enabled_strategy_ids must be an iterable of strings, not a single string."
                )
            try:
                raw_ids = list(self.enabled_strategy_ids)
            except TypeError:
                raise StrategyValidationError(
                    f"enabled_strategy_ids must be an iterable, got {type(self.enabled_strategy_ids).__name__}."
                )

            seen: set[str] = set()
            validated: list[str] = []
            for sid in raw_ids:
                valid_id = validate_strategy_id(sid)
                if valid_id in seen:
                    raise DuplicateStrategyError(
                        f"Duplicate strategy_id in enabled_strategy_ids: '{valid_id}'."
                    )
                seen.add(valid_id)
                validated.append(valid_id)

            object.__setattr__(self, "enabled_strategy_ids", tuple(sorted(validated)))

    def to_dict(self) -> dict[str, Any]:
        """Convert config to JSON-safe dictionary."""
        return {
            "enabled_strategy_ids": (
                list(self.enabled_strategy_ids)
                if self.enabled_strategy_ids is not None
                else None
            )
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyRegistryConfig:
        """
        Construct StrategyRegistryConfig from dictionary with strict field validation.

        Raises:
            StrategyValidationError: If data is invalid or contains unexpected fields.
        """
        if not isinstance(data, dict):
            raise StrategyValidationError(
                f"Expected dict for StrategyRegistryConfig, got {type(data).__name__}."
            )
        allowed = {"enabled_strategy_ids"}
        extra = set(data.keys()) - allowed
        if extra:
            raise StrategyValidationError(f"Unexpected config fields: {sorted(extra)}.")

        val = data.get("enabled_strategy_ids")
        if val is not None:
            if isinstance(val, (str, bytes)):
                raise StrategyValidationError(
                    "enabled_strategy_ids must be an iterable of strings, not a single string."
                )
            try:
                val = tuple(val)
            except TypeError:
                raise StrategyValidationError(
                    f"enabled_strategy_ids must be an iterable, got {type(val).__name__}."
                )
        return cls(enabled_strategy_ids=val)


def _validate_strategy_output(
    expected_strategy_id: str,
    expected_profile: StrategyProfile,
    context: StrategyContext,
    output: Any,
) -> tuple[CandidateSetup, ...]:
    """
    Validate that strategy evaluate() output strictly satisfies all output guards.

    Validation rules:
    1. Output must be a tuple (rejects list, generator, None, dict, set, etc.).
    2. Empty tuple is valid.
    3. Every item must be an instance of CandidateSetup.
    4. candidate.strategy_id == expected_strategy_id.
    5. candidate.direction in expected_profile.allowed_directions.
    6. candidate.bar_index == context.bar_index.
    7. candidate.timestamp == context.timestamp.
    """
    if not isinstance(output, tuple):
        raise InvalidStrategyOutputError(
            f"Strategy '{expected_strategy_id}' evaluate() must return a tuple, "
            f"got {type(output).__name__}."
        )

    if len(output) == 0:
        return ()

    for i, item in enumerate(output):
        if not isinstance(item, CandidateSetup):
            raise InvalidStrategyOutputError(
                f"Strategy '{expected_strategy_id}' output item {i} is not a CandidateSetup, "
                f"got {type(item).__name__}."
            )
        if item.strategy_id != expected_strategy_id:
            raise InvalidStrategyOutputError(
                f"Candidate strategy_id '{item.strategy_id}' does not match "
                f"running strategy '{expected_strategy_id}'."
            )
        if item.direction not in expected_profile.allowed_directions:
            raise InvalidStrategyOutputError(
                f"Candidate direction '{item.direction}' not permitted by "
                f"profile allowed_directions {expected_profile.allowed_directions}."
            )
        if item.bar_index != context.bar_index:
            raise InvalidStrategyOutputError(
                f"Candidate bar_index {item.bar_index} does not match "
                f"context.bar_index {context.bar_index}."
            )
        if item.timestamp != context.timestamp:
            raise InvalidStrategyOutputError(
                f"Candidate timestamp {item.timestamp} does not match "
                f"context.timestamp {context.timestamp}."
            )

    return output


def _evaluate_strategy(
    strategy: StrategyTemplate,
    expected_strategy_id: str,
    expected_profile: StrategyProfile,
    context: StrategyContext,
) -> tuple[CandidateSetup, ...]:
    """Internal evaluation helper called only by evaluate_enabled."""
    raw_output = strategy.evaluate(context)

    if (
        getattr(strategy, "strategy_id", None) != expected_strategy_id
        or getattr(strategy, "profile", None) != expected_profile
    ):
        raise StrategyStateError(
            f"Strategy '{expected_strategy_id}' mutated its identity "
            "or profile during evaluation."
        )

    return _validate_strategy_output(
        expected_strategy_id,
        expected_profile,
        context,
        raw_output,
    )


class StrategyRegistry:
    """
    Deterministic, immutable registry of SMC StrategyTemplate instances.

    Guarantees:
    - Registered strategies are fixed at initialization; immutable registry structure.
    - Canonical deterministic order sorted by strategy_id ascending.
    - Average O(1) strategy lookup.
    - Zero mutation of inputs or context.
    - Exactly-once evaluation dispatch per accepted unique closed bar.
    - Idempotent retry on duplicate context delivery via last-bar cache.
    - State machine lifecycle reset via reset_all().
    - Safe poisoned-state recovery contract.
    """

    def __init__(
        self,
        strategies: Iterable[StrategyTemplate],
        config: Optional[StrategyRegistryConfig] = None,
    ) -> None:
        if isinstance(strategies, (str, bytes)):
            raise StrategyValidationError(
                "strategies must be an iterable of StrategyTemplate, not a string."
            )
        try:
            raw_strategies = list(strategies)
        except TypeError:
            raise StrategyValidationError(
                f"strategies must be an iterable, got {type(strategies).__name__}."
            )

        seen_ids: set[str] = set()
        for s in raw_strategies:
            validate_strategy_template(s)
            if s.strategy_id in seen_ids:
                raise DuplicateStrategyError(
                    f"Duplicate strategy_id '{s.strategy_id}' registered."
                )
            seen_ids.add(s.strategy_id)

        sorted_strategies = sorted(raw_strategies, key=lambda s: s.strategy_id)
        self._strategies: Mapping[str, StrategyTemplate] = MappingProxyType(
            {s.strategy_id: s for s in sorted_strategies}
        )
        self._registered_ids: tuple[str, ...] = tuple(self._strategies.keys())

        if config is None:
            config = StrategyRegistryConfig(enabled_strategy_ids=None)
        elif not isinstance(config, StrategyRegistryConfig):
            raise StrategyValidationError(
                f"Expected StrategyRegistryConfig, got {type(config).__name__}."
            )

        if config.enabled_strategy_ids is not None:
            for sid in config.enabled_strategy_ids:
                if sid not in self._strategies:
                    raise UnknownStrategyError(
                        f"Config references unknown strategy_id '{sid}'."
                    )
            self._enabled_ids: tuple[str, ...] = tuple(
                sid for sid in self._registered_ids if sid in config.enabled_strategy_ids
            )
        else:
            self._enabled_ids = self._registered_ids

        self._disabled_ids: tuple[str, ...] = tuple(
            sid for sid in self._registered_ids if sid not in self._enabled_ids
        )
        self._enabled_strategies: tuple[StrategyTemplate, ...] = tuple(
            self._strategies[sid] for sid in self._enabled_ids
        )
        self._profiles: Mapping[str, StrategyProfile] = MappingProxyType(
            {sid: self._strategies[sid].profile for sid in self._registered_ids}
        )
        self._config: StrategyRegistryConfig = config

        # Private dispatch state
        self._last_bar_index: Optional[int] = None
        self._last_timestamp: Any = None
        self._last_context_payload: Optional[dict[str, Any]] = None
        self._last_result: Optional[Mapping[str, tuple[CandidateSetup, ...]]] = None
        self._poisoned: bool = False

    @property
    def config(self) -> StrategyRegistryConfig:
        """Return the immutable StrategyRegistryConfig."""
        return self._config

    @property
    def profiles(self) -> Mapping[str, StrategyProfile]:
        """Return an immutable mapping of registered strategy IDs to their StrategyProfiles."""
        return self._profiles

    @property
    def registered_strategy_ids(self) -> tuple[str, ...]:
        """Return all registered strategy IDs in canonical order."""
        return self._registered_ids

    @property
    def enabled_strategy_ids(self) -> tuple[str, ...]:
        """Return all enabled strategy IDs in canonical order."""
        return self._enabled_ids

    @property
    def disabled_strategy_ids(self) -> tuple[str, ...]:
        """Return all disabled strategy IDs in canonical order."""
        return self._disabled_ids

    def is_registered(self, strategy_id: str) -> bool:
        """Check whether strategy_id is registered in this registry."""
        return strategy_id in self._strategies

    def is_enabled(self, strategy_id: str) -> bool:
        """
        Check whether strategy_id is enabled.

        Raises:
            UnknownStrategyError: If strategy_id is not registered.
        """
        if strategy_id not in self._strategies:
            raise UnknownStrategyError(f"Unknown strategy_id '{strategy_id}'.")
        return strategy_id in self._enabled_ids

    def get_profile(self, strategy_id: str) -> StrategyProfile:
        """
        Retrieve the immutable StrategyProfile for a strategy.

        Raises:
            UnknownStrategyError: If strategy_id is not registered.
        """
        if strategy_id not in self._strategies:
            raise UnknownStrategyError(f"Unknown strategy_id '{strategy_id}'.")
        return self._profiles[strategy_id]

    def reset_all(self) -> None:
        """
        Reset all registered strategies (both enabled and disabled) in canonical order.
        Clears dispatch caches and recovers registry from poisoned state upon success.

        Raises:
            Exception: Propagates any exception from strategy.reset() without swallowing.
        """
        self._poisoned = True
        try:
            for s in self._strategies.values():
                s.reset()
        except Exception:
            raise

        self._last_bar_index = None
        self._last_timestamp = None
        self._last_context_payload = None
        self._last_result = None
        self._poisoned = False

    def evaluate_enabled(
        self,
        context: StrategyContext,
    ) -> Mapping[str, tuple[CandidateSetup, ...]]:
        """
        Evaluate all enabled strategies on the closed bar StrategyContext exactly once.

        Returns:
            A read-only Mapping of strategy_id -> tuple[CandidateSetup, ...] in canonical order.

        Raises:
            StrategyValidationError: If context is not a StrategyContext.
            StrategyStateError: If context dispatch is non-monotonic, conflicting, or registry is poisoned.
            InvalidStrategyOutputError: If any strategy evaluate() returns invalid output.
            Exception: Any internal strategy exception propagates and marks registry poisoned.
        """
        if not isinstance(context, StrategyContext):
            raise StrategyValidationError(
                f"Expected StrategyContext, got {type(context).__name__}."
            )

        if self._poisoned:
            raise StrategyStateError(
                "StrategyRegistry is poisoned; reset successfully or rebuild before evaluation."
            )

        # Monotonicity & idempotent retry check
        if self._last_bar_index is not None:
            if context.bar_index == self._last_bar_index:
                if (
                    context.timestamp == self._last_timestamp
                    and context.to_dict() == self._last_context_payload
                ):
                    assert self._last_result is not None
                    return self._last_result
                raise StrategyStateError(
                    "Context dispatch conflicts with the cached bar for the same bar_index."
                )
            elif context.bar_index < self._last_bar_index:
                raise StrategyStateError(
                    f"Context bar_index ({context.bar_index}) must not decrease (last was {self._last_bar_index})."
                )
            else:  # context.bar_index > self._last_bar_index
                if context.timestamp <= self._last_timestamp:
                    raise StrategyStateError(
                        f"Context timestamp ({context.timestamp}) must strictly increase on bar advance (last was {self._last_timestamp})."
                    )

        # Preflight integrity check across all enabled strategies prior to any evaluation
        for sid in self._enabled_ids:
            strat = self._strategies[sid]
            expected_prof = self._profiles[sid]
            if getattr(strat, "strategy_id", None) != sid or getattr(strat, "profile", None) != expected_prof:
                self._poisoned = True
                raise StrategyStateError(
                    f"Strategy '{sid}' identity or profile was mutated prior to evaluation."
                )

        results: dict[str, tuple[CandidateSetup, ...]] = {}
        try:
            for sid in self._enabled_ids:
                strat = self._strategies[sid]
                expected_prof = self._profiles[sid]
                candidates = _evaluate_strategy(strat, sid, expected_prof, context)
                results[sid] = candidates
        except Exception:
            self._poisoned = True
            raise

        frozen_results = MappingProxyType(results)
        self._last_bar_index = context.bar_index
        self._last_timestamp = context.timestamp
        self._last_context_payload = context.to_dict()
        self._last_result = frozen_results

        return frozen_results


__all__ = [
    "StrategyRegistryConfig",
    "StrategyRegistry",
]
