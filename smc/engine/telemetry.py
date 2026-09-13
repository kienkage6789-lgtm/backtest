"""
smc.engine.telemetry
====================
Deterministic Selection Telemetry & Audit Engine for the SMC Strategy Framework (T53.8).

Responsibilities:
1. SelectionAuditRecord: Frozen, deeply immutable, JSON-round-trip per-bar audit record.
2. aggregate_selection_telemetry: Pure aggregation function with deduplication,
   anti-double-counting, and comprehensive metric rollups.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional, Sequence

import pandas as pd

from smc.engine.errors import StrictModelTypeError
from smc.engine.models import (
    _freeze,
    _parse_timestamp,
    _unfreeze,
    _validate_base_token,
    _validate_finite_float,
    _validate_non_negative_int,
    _validate_str,
)

if TYPE_CHECKING:
    from smc.engine.selector import ClusterScorecard


@dataclass(frozen=True)
class SelectionAuditRecord:
    """
    Immutable, JSON-round-trip audit record capturing selection facts for a single bar.
    """
    record_version: str
    symbol: str
    timeframe: str
    bar_index: int
    timestamp: pd.Timestamp
    decision_id: str
    action: Literal["SELECT", "NO_TRADE"]
    reason: str
    primary_strategy_id: Optional[str] = None
    primary_setup_id: Optional[str] = None
    direction: Optional[Literal["BUY", "SELL"]] = None
    total_score: Optional[float] = None
    score_gap: Optional[float] = None
    cluster_id: Optional[str] = None
    supporting_strategy_ids: tuple[str, ...] = field(default_factory=tuple)
    cluster_scorecards: tuple[Any, ...] = field(default_factory=tuple)
    gate_reason_counts: Mapping[str, int] = field(default_factory=dict)
    evaluated_count: int = 0
    eligible_count: int = 0
    rejected_count: int = 0
    direction_conflict_present: bool = False
    best_buy_score: Optional[float] = None
    best_sell_score: Optional[float] = None
    regime: Optional[str] = None
    selector_version: str = "selector-v1"
    minimum_total_score: float = 60.0
    minimum_direction_gap: float = 15.0
    scoring_weights: tuple[float, float, float, float] = (0.25, 0.35, 0.25, 0.15)
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from smc.engine.models import make_decision_id

        v = _validate_str(self.record_version, "record_version")
        object.__setattr__(self, "record_version", v)

        sym = _validate_str(self.symbol, "symbol")
        object.__setattr__(self, "symbol", sym)

        tf = _validate_str(self.timeframe, "timeframe")
        object.__setattr__(self, "timeframe", tf)

        b_idx = _validate_non_negative_int(self.bar_index, "bar_index")
        object.__setattr__(self, "bar_index", b_idx)

        ts = _parse_timestamp(self.timestamp, "timestamp")
        object.__setattr__(self, "timestamp", ts)

        if self.action not in {"SELECT", "NO_TRADE"}:
            raise ValueError(f"Invalid action '{self.action}'. Must be 'SELECT' or 'NO_TRADE'.")

        rea = _validate_str(self.reason, "reason")
        valid_reasons = {"ok", "no_eligible_setup", "insufficient_score", "conflicting_direction"}
        if rea not in valid_reasons:
            raise ValueError(f"Invalid reason '{rea}'. Must be one of {sorted(valid_reasons)}.")
        object.__setattr__(self, "reason", rea)

        dec_id = _validate_str(self.decision_id, "decision_id")
        expected_dec_id = make_decision_id(b_idx, self.action, self.primary_strategy_id)
        if dec_id != expected_dec_id:
            raise ValueError(
                f"decision_id '{dec_id}' does not match canonical decision ID '{expected_dec_id}'"
            )
        object.__setattr__(self, "decision_id", dec_id)

        if self.action == "SELECT":
            if rea != "ok":
                raise ValueError(f"SelectionAuditRecord with action='SELECT' must have reason='ok', got '{rea}'")
            if self.primary_strategy_id is None:
                raise ValueError("SelectionAuditRecord with action='SELECT' must have primary_strategy_id.")
            strat = _validate_str(self.primary_strategy_id, "primary_strategy_id")
            object.__setattr__(self, "primary_strategy_id", strat)

            if self.primary_setup_id is None:
                raise ValueError("SelectionAuditRecord with action='SELECT' must have primary_setup_id.")
            setup = _validate_str(self.primary_setup_id, "primary_setup_id")
            object.__setattr__(self, "primary_setup_id", setup)

            if self.direction not in {"BUY", "SELL"}:
                raise ValueError(f"SelectionAuditRecord with action='SELECT' must have direction 'BUY' or 'SELL', got '{self.direction}'.")

            if self.total_score is None:
                raise ValueError("SelectionAuditRecord with action='SELECT' must have total_score.")
            tot = _validate_finite_float(self.total_score, "total_score", min_val=0.0, max_val=100.0)
            object.__setattr__(self, "total_score", round(tot, 2))

            if self.cluster_id is None:
                raise ValueError("SelectionAuditRecord with action='SELECT' must have cluster_id.")
            cid = _validate_str(self.cluster_id, "cluster_id")
            object.__setattr__(self, "cluster_id", cid)
        else:  # NO_TRADE
            if rea == "ok":
                raise ValueError("SelectionAuditRecord with action='NO_TRADE' cannot have reason='ok'")
            if self.primary_strategy_id is not None:
                raise ValueError("SelectionAuditRecord with action='NO_TRADE' cannot have primary_strategy_id.")
            if self.primary_setup_id is not None:
                raise ValueError("SelectionAuditRecord with action='NO_TRADE' cannot have primary_setup_id.")
            if self.direction is not None:
                raise ValueError("SelectionAuditRecord with action='NO_TRADE' cannot have direction.")
            if self.total_score is not None:
                raise ValueError("SelectionAuditRecord with action='NO_TRADE' cannot have total_score.")
            if self.cluster_id is not None:
                raise ValueError("SelectionAuditRecord with action='NO_TRADE' cannot have cluster_id.")
            if len(self.supporting_strategy_ids) > 0:
                raise ValueError("SelectionAuditRecord with action='NO_TRADE' cannot have supporting_strategy_ids.")

        if self.score_gap is not None:
            gap = _validate_finite_float(self.score_gap, "score_gap", min_val=0.0)
            object.__setattr__(self, "score_gap", round(gap, 2))

        # Validate selector config fields
        s_ver = _validate_base_token(self.selector_version, "selector_version")
        object.__setattr__(self, "selector_version", s_ver)

        min_s = _validate_finite_float(self.minimum_total_score, "minimum_total_score", min_val=0.0, max_val=100.0)
        object.__setattr__(self, "minimum_total_score", round(min_s, 2))

        min_gap = _validate_finite_float(self.minimum_direction_gap, "minimum_direction_gap", min_val=0.0, max_val=100.0)
        object.__setattr__(self, "minimum_direction_gap", round(min_gap, 2))

        if not isinstance(self.scoring_weights, (tuple, list)) or len(self.scoring_weights) != 4:
            raise StrictModelTypeError("scoring_weights must be a sequence of 4 floats")
        parsed_w = []
        for idx, w in enumerate(self.scoring_weights):
            w_val = _validate_finite_float(w, f"scoring_weights[{idx}]", min_val=0.0, max_val=1.0)
            parsed_w.append(float(w_val))
        if abs(sum(parsed_w) - 1.0) > 1e-9:
            raise ValueError(f"scoring_weights must sum to 1.0 within 1e-9 tolerance, got {sum(parsed_w):.9f}")
        object.__setattr__(self, "scoring_weights", tuple(parsed_w))

        # Validate supporting_strategy_ids
        supp = []
        seen = set()
        for s in self.supporting_strategy_ids:
            s_str = _validate_str(s, "supporting_strategy_id")
            if self.primary_strategy_id is not None and s_str == self.primary_strategy_id:
                raise ValueError(f"supporting_strategy_ids cannot contain primary_strategy_id '{self.primary_strategy_id}'")
            if s_str in seen:
                raise ValueError(f"Duplicate strategy ID in supporting_strategy_ids: '{s_str}'")
            seen.add(s_str)
            supp.append(s_str)
        if tuple(supp) != tuple(sorted(supp)):
            raise ValueError("supporting_strategy_ids must be canonically sorted in lexicographical order")
        object.__setattr__(self, "supporting_strategy_ids", tuple(supp))

        # Validate cluster_scorecards
        from smc.engine.selector import ClusterScorecard, cluster_rank_key
        sc_list = []
        for sc in self.cluster_scorecards:
            if isinstance(sc, dict):
                sc = ClusterScorecard.from_dict(sc)
            elif not isinstance(sc, ClusterScorecard):
                raise StrictModelTypeError(f"cluster_scorecards element must be ClusterScorecard, got {type(sc).__name__}")
            sc_list.append(sc)

        if sc_list != sorted(sc_list, key=cluster_rank_key):
            raise ValueError("cluster_scorecards must be canonically sorted by cluster_rank_key")
        object.__setattr__(self, "cluster_scorecards", tuple(sc_list))

        # Validate direction conflict / score_gap semantics
        if self.action == "SELECT":
            directions = {sc.direction for sc in sc_list}
            if len(directions) >= 2:
                if self.score_gap is None:
                    raise ValueError("score_gap must be provided when opposing directions exist in cluster_scorecards")
            elif len(directions) == 1:
                if self.score_gap is not None:
                    raise ValueError("score_gap must be None when only one direction exists in cluster_scorecards")
        else:  # NO_TRADE
            if rea == "conflicting_direction":
                if self.score_gap is None:
                    raise ValueError("score_gap must be provided when reason is 'conflicting_direction'")
            else:
                if self.score_gap is not None:
                    raise ValueError(f"score_gap must be None when reason is '{rea}'")

        # Validate directional snapshot fields
        buy_cards = [sc for sc in sc_list if sc.direction == "BUY"]
        sell_cards = [sc for sc in sc_list if sc.direction == "SELL"]
        expected_conflict = bool(buy_cards and sell_cards)
        best_buy = min(buy_cards, key=cluster_rank_key) if buy_cards else None
        best_sell = min(sell_cards, key=cluster_rank_key) if sell_cards else None
        expected_buy_score = round(best_buy.total_score, 2) if best_buy else None
        expected_sell_score = round(best_sell.total_score, 2) if best_sell else None

        if not isinstance(self.direction_conflict_present, bool):
            raise StrictModelTypeError(
                f"direction_conflict_present must be bool, got {type(self.direction_conflict_present).__name__}"
            )
        if self.direction_conflict_present != expected_conflict:
            raise ValueError(
                f"direction_conflict_present={self.direction_conflict_present} does not match expected {expected_conflict} "
                f"(buy_cards={len(buy_cards)}, sell_cards={len(sell_cards)})"
            )

        if buy_cards:
            if self.best_buy_score is None:
                raise ValueError("best_buy_score cannot be None when BUY scorecards exist")
            val_buy = _validate_finite_float(self.best_buy_score, "best_buy_score", min_val=0.0, max_val=100.0)
            val_buy_rounded = round(val_buy, 2)
            if abs(val_buy_rounded - expected_buy_score) > 1e-6:
                raise ValueError(
                    f"best_buy_score ({val_buy_rounded}) does not match winner scorecard score ({expected_buy_score})"
                )
            object.__setattr__(self, "best_buy_score", val_buy_rounded)
        else:
            if self.best_buy_score is not None:
                raise ValueError(f"best_buy_score must be None when no BUY scorecards exist, got {self.best_buy_score}")

        if sell_cards:
            if self.best_sell_score is None:
                raise ValueError("best_sell_score cannot be None when SELL scorecards exist")
            val_sell = _validate_finite_float(self.best_sell_score, "best_sell_score", min_val=0.0, max_val=100.0)
            val_sell_rounded = round(val_sell, 2)
            if abs(val_sell_rounded - expected_sell_score) > 1e-6:
                raise ValueError(
                    f"best_sell_score ({val_sell_rounded}) does not match winner scorecard score ({expected_sell_score})"
                )
            object.__setattr__(self, "best_sell_score", val_sell_rounded)
        else:
            if self.best_sell_score is not None:
                raise ValueError(f"best_sell_score must be None when no SELL scorecards exist, got {self.best_sell_score}")

        # Validate gate_reason_counts
        gr_counts = {}
        for k, v in self.gate_reason_counts.items():
            k_str = _validate_str(k, "gate_reason key")
            v_int = _validate_non_negative_int(v, f"gate_reason count for '{k}'")
            gr_counts[k_str] = v_int
        object.__setattr__(self, "gate_reason_counts", MappingProxyType(gr_counts))

        ev_c = _validate_non_negative_int(self.evaluated_count, "evaluated_count")
        el_c = _validate_non_negative_int(self.eligible_count, "eligible_count")
        re_c = _validate_non_negative_int(self.rejected_count, "rejected_count")
        if ev_c != el_c + re_c:
            raise ValueError(f"evaluated_count ({ev_c}) != eligible_count ({el_c}) + rejected_count ({re_c})")
        object.__setattr__(self, "evaluated_count", ev_c)
        object.__setattr__(self, "eligible_count", el_c)
        object.__setattr__(self, "rejected_count", re_c)

        if self.regime is not None:
            reg = _validate_str(self.regime, "regime")
            valid_regimes = {"bullish_trend", "bearish_trend", "ranging", "volatile_reversal", "uncertain"}
            if reg not in valid_regimes:
                raise ValueError(f"Invalid regime '{reg}'. Must be one of {sorted(valid_regimes)}.")
            object.__setattr__(self, "regime", reg)

        object.__setattr__(self, "meta", _freeze(self.meta))
        _validate_audit_internal_consistency(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_version": self.record_version,
            "selector_version": self.selector_version,
            "minimum_total_score": float(self.minimum_total_score),
            "minimum_direction_gap": float(self.minimum_direction_gap),
            "scoring_weights": [float(w) for w in self.scoring_weights],
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_index": int(self.bar_index),
            "timestamp": self.timestamp.isoformat(),
            "decision_id": self.decision_id,
            "action": self.action,
            "reason": self.reason,
            "primary_strategy_id": self.primary_strategy_id,
            "primary_setup_id": self.primary_setup_id,
            "direction": self.direction,
            "total_score": float(self.total_score) if self.total_score is not None else None,
            "score_gap": float(self.score_gap) if self.score_gap is not None else None,
            "direction_conflict_present": bool(self.direction_conflict_present),
            "best_buy_score": float(self.best_buy_score) if self.best_buy_score is not None else None,
            "best_sell_score": float(self.best_sell_score) if self.best_sell_score is not None else None,
            "cluster_id": self.cluster_id,
            "supporting_strategy_ids": list(self.supporting_strategy_ids),
            "cluster_scorecards": [sc.to_dict() for sc in self.cluster_scorecards],
            "gate_reason_counts": dict(self.gate_reason_counts),
            "evaluated_count": int(self.evaluated_count),
            "eligible_count": int(self.eligible_count),
            "rejected_count": int(self.rejected_count),
            "regime": self.regime,
            "meta": _unfreeze(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SelectionAuditRecord:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for SelectionAuditRecord, got {type(data).__name__}")

        REQUIRED_SERIALIZED_FIELDS = frozenset({
            "record_version", "selector_version", "minimum_total_score", "minimum_direction_gap",
            "scoring_weights", "symbol", "timeframe", "bar_index", "timestamp",
            "decision_id", "action", "reason", "primary_strategy_id",
            "primary_setup_id", "direction", "total_score", "score_gap",
            "direction_conflict_present", "best_buy_score", "best_sell_score",
            "cluster_id", "supporting_strategy_ids", "cluster_scorecards",
            "gate_reason_counts", "evaluated_count", "eligible_count",
            "rejected_count", "regime", "meta",
        })

        unknown = set(data.keys()) - REQUIRED_SERIALIZED_FIELDS
        if unknown:
            raise KeyError(f"Unknown fields in SelectionAuditRecord: {sorted(unknown)}")

        missing = [f for f in sorted(REQUIRED_SERIALIZED_FIELDS) if f not in data]
        if missing:
            raise KeyError(f"Missing required field(s) in SelectionAuditRecord: {missing}")

        from smc.engine.selector import ClusterScorecard
        if not isinstance(data["cluster_scorecards"], (list, tuple)):
            raise StrictModelTypeError(f"cluster_scorecards must be a sequence, got {type(data['cluster_scorecards']).__name__}")
        scorecards = tuple(
            ClusterScorecard.from_dict(sc) if isinstance(sc, dict) else sc
            for sc in data["cluster_scorecards"]
        )
        if not isinstance(data["scoring_weights"], (list, tuple)):
            raise StrictModelTypeError(f"scoring_weights must be a sequence, got {type(data['scoring_weights']).__name__}")
        if not isinstance(data["supporting_strategy_ids"], (list, tuple)):
            raise StrictModelTypeError(f"supporting_strategy_ids must be a sequence, got {type(data['supporting_strategy_ids']).__name__}")
        if not isinstance(data["gate_reason_counts"], dict):
            raise StrictModelTypeError(f"gate_reason_counts must be a dict, got {type(data['gate_reason_counts']).__name__}")
        if not isinstance(data["meta"], dict):
            raise StrictModelTypeError(f"meta must be a dict, got {type(data['meta']).__name__}")

        return cls(
            record_version=data["record_version"],
            selector_version=data["selector_version"],
            minimum_total_score=data["minimum_total_score"],
            minimum_direction_gap=data["minimum_direction_gap"],
            scoring_weights=tuple(data["scoring_weights"]),
            symbol=data["symbol"],
            timeframe=data["timeframe"],
            bar_index=data["bar_index"],
            timestamp=data["timestamp"],
            decision_id=data["decision_id"],
            action=data["action"],
            reason=data["reason"],
            primary_strategy_id=data["primary_strategy_id"],
            primary_setup_id=data["primary_setup_id"],
            direction=data["direction"],
            total_score=data["total_score"],
            score_gap=data["score_gap"],
            direction_conflict_present=data["direction_conflict_present"],
            best_buy_score=data["best_buy_score"],
            best_sell_score=data["best_sell_score"],
            cluster_id=data["cluster_id"],
            supporting_strategy_ids=tuple(data["supporting_strategy_ids"]),
            cluster_scorecards=scorecards,
            gate_reason_counts=dict(data["gate_reason_counts"]),
            evaluated_count=data["evaluated_count"],
            eligible_count=data["eligible_count"],
            rejected_count=data["rejected_count"],
            regime=data["regime"],
            meta=data["meta"],
        )


def _validate_audit_internal_consistency(record: SelectionAuditRecord) -> None:
    """
    Validate internal self-consistency invariants of a SelectionAuditRecord standalone:
    - Cluster IDs unique across scorecards.
    - Member setup IDs unique across scorecards (disjoint partition).
    - eligible_count == total unique member setups across scorecards.
    - Scorecard scoring weights match audit scoring weights.
    - Scorecard primary setup score ownership: primary_score >= max(member_scores) - 1e-6.
    - SELECT action invariants:
      * cluster_scorecards non-empty
      * cluster_id appears exactly once in scorecards
      * winning scorecard fields match audit record (setup, strategy, direction, supporting, score)
      * single direction -> rank 1 is winner, score_gap is None
      * conflicting directions -> score_gap matches recomputed gap, gap >= minimum_direction_gap,
        direction matches higher scoring side
      * winning score >= minimum_total_score
      * eligible_count > 0
    - NO_TRADE action invariants:
      * no_eligible_setup -> eligible_count == 0, cluster_scorecards == (), score_gap is None
      * insufficient_score -> cluster_scorecards non-empty, eligible_count > 0,
        max(scores) < minimum_total_score, score_gap is None
      * conflicting_direction -> both BUY and SELL scorecards exist,
        recomputed gap < minimum_direction_gap, score_gap matches recomputed gap
    """
    sc_list = record.cluster_scorecards

    # Action / reason structural prerequisites
    if record.action == "SELECT":
        if len(sc_list) == 0:
            raise ValueError("SelectionAuditRecord with action='SELECT' must have at least one cluster scorecard")
    elif record.action == "NO_TRADE":
        if record.reason == "no_eligible_setup":
            if record.eligible_count != 0:
                raise ValueError(f"eligible_count must be 0 for reason 'no_eligible_setup', got {record.eligible_count}")
            if len(sc_list) != 0:
                raise ValueError(f"cluster_scorecards must be empty for reason 'no_eligible_setup', got {len(sc_list)}")
            if record.score_gap is not None:
                raise ValueError("score_gap must be None for reason 'no_eligible_setup'")
        elif record.reason == "insufficient_score":
            if len(sc_list) == 0:
                raise ValueError("cluster_scorecards cannot be empty for reason 'insufficient_score'")
            if record.eligible_count <= 0:
                raise ValueError("eligible_count must be > 0 for reason 'insufficient_score'")
        elif record.reason == "conflicting_direction":
            if len(sc_list) == 0:
                raise ValueError("cluster_scorecards cannot be empty for reason 'conflicting_direction'")
            if record.eligible_count <= 0:
                raise ValueError("eligible_count must be > 0 for reason 'conflicting_direction'")

    # 1. Cluster IDs unique
    cids = [sc.cluster_id for sc in sc_list]
    if len(cids) != len(set(cids)):
        raise ValueError(f"Duplicate cluster_id found in cluster_scorecards: {cids}")

    # 2. Member setup IDs unique across scorecards and count matches eligible_count
    seen_members = set()
    for sc in sc_list:
        for m in sc.member_setup_ids:
            if m in seen_members:
                raise ValueError(f"Member setup '{m}' appears in multiple cluster scorecards")
            seen_members.add(m)
        if sc.scoring_weights != record.scoring_weights:
            raise ValueError(
                f"Scorecard '{sc.cluster_id}' scoring_weights {sc.scoring_weights} "
                f"does not match audit scoring_weights {record.scoring_weights}"
            )
        # Defense: verify primary score ownership
        max_m = max(sc.member_scores.values()) if sc.member_scores else 0.0
        if sc.member_scores.get(sc.primary_setup_id, 0.0) < max_m - 1e-6:
            raise ValueError(
                f"Scorecard '{sc.cluster_id}' primary setup score {sc.member_scores.get(sc.primary_setup_id)} "
                f"is less than maximum member score {max_m}"
            )

    if record.eligible_count != len(seen_members):
        raise ValueError(
            f"eligible_count ({record.eligible_count}) does not match total unique member "
            f"setup count ({len(seen_members)}) across cluster scorecards"
        )

    # 3. SELECT action
    if record.action == "SELECT":
        matching = [sc for sc in sc_list if sc.cluster_id == record.cluster_id]
        if len(matching) != 1:
            raise ValueError(
                f"Winning cluster_id '{record.cluster_id}' must appear exactly once in cluster_scorecards, "
                f"found {len(matching)}"
            )
        win_sc = matching[0]
        if win_sc.primary_setup_id != record.primary_setup_id:
            raise ValueError(
                f"audit primary_setup_id '{record.primary_setup_id}' does not match "
                f"winning scorecard primary_setup_id '{win_sc.primary_setup_id}'"
            )
        if win_sc.primary_strategy_id != record.primary_strategy_id:
            raise ValueError(
                f"audit primary_strategy_id '{record.primary_strategy_id}' does not match "
                f"winning scorecard primary_strategy_id '{win_sc.primary_strategy_id}'"
            )
        if win_sc.direction != record.direction:
            raise ValueError(
                f"audit direction '{record.direction}' does not match "
                f"winning scorecard direction '{win_sc.direction}'"
            )
        if win_sc.supporting_strategy_ids != record.supporting_strategy_ids:
            raise ValueError(
                f"audit supporting_strategy_ids {record.supporting_strategy_ids} does not match "
                f"winning scorecard supporting_strategy_ids {win_sc.supporting_strategy_ids}"
            )
        if abs(win_sc.total_score - (record.total_score or 0.0)) > 1e-6:
            raise ValueError(
                f"audit total_score {record.total_score} does not match "
                f"winning scorecard total_score {win_sc.total_score}"
            )
        if win_sc.total_score < record.minimum_total_score - 1e-6:
            raise ValueError(
                f"Winning scorecard total_score ({win_sc.total_score}) is less than "
                f"minimum_total_score ({record.minimum_total_score})"
            )
        if record.eligible_count <= 0:
            raise ValueError("eligible_count must be > 0 for SELECT action")

        buy_scs = [sc for sc in sc_list if sc.direction == "BUY"]
        sell_scs = [sc for sc in sc_list if sc.direction == "SELL"]
        if buy_scs and sell_scs:
            best_buy = max(sc.total_score for sc in buy_scs)
            best_sell = max(sc.total_score for sc in sell_scs)
            recomputed_gap = round(abs(best_buy - best_sell), 2)
            if recomputed_gap < record.minimum_direction_gap - 1e-6:
                raise ValueError(
                    f"Cannot SELECT when opposing direction score gap ({recomputed_gap}) "
                    f"is less than minimum_direction_gap ({record.minimum_direction_gap})"
                )
            if record.score_gap is None or abs(record.score_gap - recomputed_gap) > 1e-6:
                raise ValueError(
                    f"audit score_gap {record.score_gap} does not match recomputed gap {recomputed_gap}"
                )
            expected_dir = "BUY" if best_buy > best_sell else "SELL"
            if best_buy != best_sell and win_sc.direction != expected_dir:
                raise ValueError(
                    f"Winning scorecard direction '{win_sc.direction}' does not match "
                    f"higher-scoring direction '{expected_dir}' under conflict"
                )
        else:
            if record.score_gap is not None:
                raise ValueError("score_gap must be None when only one direction exists")
            if win_sc != sc_list[0]:
                raise ValueError("When single direction exists, winning scorecard must be rank 1")

    elif record.action == "NO_TRADE":
        if record.reason == "no_eligible_setup":
            if record.eligible_count != 0:
                raise ValueError(f"eligible_count must be 0 for reason 'no_eligible_setup', got {record.eligible_count}")
            if len(sc_list) != 0:
                raise ValueError(f"cluster_scorecards must be empty for reason 'no_eligible_setup', got {len(sc_list)}")
            if record.score_gap is not None:
                raise ValueError("score_gap must be None for reason 'no_eligible_setup'")
        elif record.reason == "insufficient_score":
            if len(sc_list) == 0:
                raise ValueError("cluster_scorecards cannot be empty for reason 'insufficient_score'")
            if record.eligible_count <= 0:
                raise ValueError("eligible_count must be > 0 for reason 'insufficient_score'")
            best_score = max(sc.total_score for sc in sc_list)
            if best_score >= record.minimum_total_score - 1e-6:
                raise ValueError(
                    f"Best scorecard total_score ({best_score}) is >= minimum_total_score "
                    f"({record.minimum_total_score}), invalid for 'insufficient_score'"
                )
            if record.score_gap is not None:
                raise ValueError("score_gap must be None for reason 'insufficient_score'")
        elif record.reason == "conflicting_direction":
            buy_scs = [sc for sc in sc_list if sc.direction == "BUY"]
            sell_scs = [sc for sc in sc_list if sc.direction == "SELL"]
            if not buy_scs or not sell_scs:
                raise ValueError("Both BUY and SELL scorecards must exist for reason 'conflicting_direction'")
            best_buy = max(sc.total_score for sc in buy_scs)
            best_sell = max(sc.total_score for sc in sell_scs)
            recomputed_gap = round(abs(best_buy - best_sell), 2)
            if recomputed_gap >= record.minimum_direction_gap - 1e-6:
                raise ValueError(
                    f"Score gap ({recomputed_gap}) is >= minimum_direction_gap ({record.minimum_direction_gap}), "
                    f"invalid for 'conflicting_direction'"
                )
            if record.score_gap is None or abs(record.score_gap - recomputed_gap) > 1e-6:
                raise ValueError(
                    f"audit score_gap {record.score_gap} does not match recomputed gap {recomputed_gap}"
                )

    # 4. Directional snapshot validation
    from smc.engine.selector import cluster_rank_key
    buy_cards = [sc for sc in sc_list if sc.direction == "BUY"]
    sell_cards = [sc for sc in sc_list if sc.direction == "SELL"]
    expected_conflict = bool(buy_cards and sell_cards)
    best_buy = min(buy_cards, key=cluster_rank_key) if buy_cards else None
    best_sell = min(sell_cards, key=cluster_rank_key) if sell_cards else None
    expected_buy_score = round(best_buy.total_score, 2) if best_buy else None
    expected_sell_score = round(best_sell.total_score, 2) if best_sell else None

    if not isinstance(record.direction_conflict_present, bool):
        raise StrictModelTypeError(
            f"direction_conflict_present must be bool, got {type(record.direction_conflict_present).__name__}"
        )
    if record.direction_conflict_present != expected_conflict:
        raise ValueError(
            f"direction_conflict_present={record.direction_conflict_present} does not match expected {expected_conflict}"
        )

    if buy_cards:
        if record.best_buy_score is None:
            raise ValueError("best_buy_score cannot be None when BUY scorecards exist")
        val_buy = _validate_finite_float(record.best_buy_score, "best_buy_score", min_val=0.0, max_val=100.0)
        if abs(round(val_buy, 2) - expected_buy_score) > 1e-6:
            raise ValueError(f"best_buy_score ({val_buy}) does not match winner scorecard score ({expected_buy_score})")
    else:
        if record.best_buy_score is not None:
            raise ValueError(f"best_buy_score must be None when no BUY scorecards exist, got {record.best_buy_score}")

    if sell_cards:
        if record.best_sell_score is None:
            raise ValueError("best_sell_score cannot be None when SELL scorecards exist")
        val_sell = _validate_finite_float(record.best_sell_score, "best_sell_score", min_val=0.0, max_val=100.0)
        if abs(round(val_sell, 2) - expected_sell_score) > 1e-6:
            raise ValueError(f"best_sell_score ({val_sell}) does not match winner scorecard score ({expected_sell_score})")
    else:
        if record.best_sell_score is not None:
            raise ValueError(f"best_sell_score must be None when no SELL scorecards exist, got {record.best_sell_score}")


def aggregate_selection_telemetry(
    records: Sequence[SelectionAuditRecord],
) -> dict[str, Any]:
    """
    Pure aggregation function calculating selection funnel metrics across a sequence of audit records.
    Deduplicates records using (symbol, timeframe, decision_id) to prevent double counting.
    Fails closed by raising ValueError if any duplicate key is encountered.
    """
    if not isinstance(records, (list, tuple)):
        raise StrictModelTypeError(f"records must be a sequence, got {type(records).__name__}")

    if len(records) == 0:
        return {
            "total_bars": 0,
            "total_decisions": 0,
            "select_count": 0,
            "no_trade_count": 0,
            "select_rate": 0.0,
            "direction_conflict_bars": 0,
            "conflict_selected_count": 0,
            "conflict_no_trade_count": 0,
            "reasons": {},
            "strategy_selections": {},
            "direction_counts": {"BUY": 0, "SELL": 0},
            "score_stats": {
                "mean_total_score": None,
                "min_total_score": None,
                "max_total_score": None,
                "mean_score_gap": None,
            },
            "gate_reasons_aggregate": {},
            "supporting_strategy_counts": {},
            "symbols": [],
            "timeframes": [],
            "bar_range": None,
            "record_version": "1.0.0",
            "selector_version": "selector-v1",
            "minimum_total_score": 60.0,
            "minimum_direction_gap": 15.0,
            "scoring_weights": [0.25, 0.35, 0.25, 0.15],
        }

    # Verify elements and config consistency
    record_versions = set()
    selector_versions = set()
    min_scores = set()
    min_gaps = set()
    weights_set = set()

    for idx, r in enumerate(records):
        if not isinstance(r, SelectionAuditRecord):
            raise StrictModelTypeError(f"Record at index {idx} is not a SelectionAuditRecord, got {type(r).__name__}")
        _validate_audit_internal_consistency(r)
        record_versions.add(r.record_version)
        selector_versions.add(r.selector_version)
        min_scores.add(r.minimum_total_score)
        min_gaps.add(r.minimum_direction_gap)
        weights_set.add(r.scoring_weights)

    if len(record_versions) > 1:
        raise ValueError(f"Mixed record versions detected in telemetry aggregation: {sorted(record_versions)}")
    if len(selector_versions) > 1:
        raise ValueError(f"Mixed selector versions detected in telemetry aggregation: {sorted(selector_versions)}")
    if len(min_scores) > 1:
        raise ValueError(f"Mixed minimum_total_score detected in telemetry aggregation: {sorted(min_scores)}")
    if len(min_gaps) > 1:
        raise ValueError(f"Mixed minimum_direction_gap detected in telemetry aggregation: {sorted(min_gaps)}")
    if len(weights_set) > 1:
        raise ValueError(f"Mixed scoring_weights detected in telemetry aggregation: {sorted(weights_set)}")

    common_record_ver = next(iter(record_versions))
    common_selector_ver = next(iter(selector_versions))
    common_min_score = next(iter(min_scores))
    common_min_gap = next(iter(min_gaps))
    common_weights = next(iter(weights_set))

    # Deduplicate by (symbol, timeframe, decision_id): fail closed on any duplicate key
    seen_keys: set[tuple[str, str, str]] = set()
    for r in records:
        dedup_key = (r.symbol, r.timeframe, r.decision_id)
        if dedup_key in seen_keys:
            raise ValueError(f"Duplicate telemetry record detected for key {dedup_key}")
        seen_keys.add(dedup_key)

    unique_records = list(records)

    total_bars = len(unique_records)
    total_decisions = total_bars
    select_count = sum(1 for r in unique_records if r.action == "SELECT")
    no_trade_count = sum(1 for r in unique_records if r.action == "NO_TRADE")
    select_rate = round(select_count / total_decisions, 4) if total_decisions > 0 else 0.0

    reasons_counter = Counter(r.reason for r in unique_records)
    reasons = dict(sorted(reasons_counter.items()))

    strategy_selections_counter = Counter(
        r.primary_strategy_id for r in unique_records if r.primary_strategy_id is not None
    )
    strategy_selections = dict(sorted(strategy_selections_counter.items()))

    buy_count = sum(1 for r in unique_records if r.action == "SELECT" and r.direction == "BUY")
    sell_count = sum(1 for r in unique_records if r.action == "SELECT" and r.direction == "SELL")
    direction_counts = {"BUY": buy_count, "SELL": sell_count}

    selected_scores = [
        float(r.total_score) for r in unique_records if r.action == "SELECT" and r.total_score is not None
    ]
    score_gaps = [
        float(r.score_gap) for r in unique_records if r.score_gap is not None
    ]

    score_stats = {
        "mean_total_score": round(float(sum(selected_scores) / len(selected_scores)), 2) if selected_scores else None,
        "min_total_score": round(float(min(selected_scores)), 2) if selected_scores else None,
        "max_total_score": round(float(max(selected_scores)), 2) if selected_scores else None,
        "mean_score_gap": round(float(sum(score_gaps) / len(score_gaps)), 2) if score_gaps else None,
    }

    gate_reasons_counter: Counter[str] = Counter()
    for r in unique_records:
        for k, v in r.gate_reason_counts.items():
            gate_reasons_counter[k] += v
    gate_reasons_aggregate = dict(sorted(gate_reasons_counter.items()))

    supporting_counter: Counter[str] = Counter()
    for r in unique_records:
        for sid in r.supporting_strategy_ids:
            supporting_counter[sid] += 1
    supporting_strategy_counts = dict(sorted(supporting_counter.items()))

    symbols = sorted(set(r.symbol for r in unique_records))
    timeframes = sorted(set(r.timeframe for r in unique_records))
    bar_indices = [r.bar_index for r in unique_records]
    bar_range = [min(bar_indices), max(bar_indices)] if bar_indices else None

    direction_conflict_bars = sum(1 for r in unique_records if r.direction_conflict_present)
    conflict_selected_count = sum(1 for r in unique_records if r.direction_conflict_present and r.action == "SELECT")
    conflict_no_trade_count = sum(1 for r in unique_records if r.direction_conflict_present and r.action == "NO_TRADE")

    return {
        "total_bars": total_bars,
        "total_decisions": total_decisions,
        "select_count": select_count,
        "no_trade_count": no_trade_count,
        "select_rate": select_rate,
        "direction_conflict_bars": direction_conflict_bars,
        "conflict_selected_count": conflict_selected_count,
        "conflict_no_trade_count": conflict_no_trade_count,
        "reasons": reasons,
        "strategy_selections": strategy_selections,
        "direction_counts": direction_counts,
        "score_stats": score_stats,
        "gate_reasons_aggregate": gate_reasons_aggregate,
        "supporting_strategy_counts": supporting_strategy_counts,
        "symbols": symbols,
        "timeframes": timeframes,
        "bar_range": bar_range,
        "record_version": common_record_ver,
        "selector_version": common_selector_ver,
        "minimum_total_score": float(common_min_score),
        "minimum_direction_gap": float(common_min_gap),
        "scoring_weights": list(float(w) for w in common_weights),
    }


__all__ = [
    "SelectionAuditRecord",
    "aggregate_selection_telemetry",
]
