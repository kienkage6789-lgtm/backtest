"""
smc.engine.confluence
=====================
Evidence deduplication, candidate clustering, direction conflict detection,
and confluence batch assembly for the SMC Strategy Framework (T53.7.3).

Responsibilities:
1. EvidenceCluster: Frozen, immutable representation of a cluster of eligible candidate setups
   sharing the same opportunity (direction + cluster_id or non-null structure leg + zone).
2. DirectionConflict: Formal representation of conflicting BUY and SELL eligible clusters
   occurring at the same bar index.
3. ConfluenceBatch: Deeply immutable, JSON-round-trip container holding market regime,
   all strategy evaluations, eligible clusters, and any direction conflict.
4. EvidenceDeduplicator: Pure function/engine for collapsing duplicate setups, checking cluster
   direction consistency, and merging shared-opportunity candidates.
5. DirectionConflictDetector: Pure detector for identifying simultaneous BUY and SELL clusters.
6. build_confluence_batch: Helper assembling evaluations, clusters, and conflict into a ConfluenceBatch.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

import numpy as np
import pandas as pd

from smc.engine.context import StrategyContext
from smc.engine.errors import StrategyStateError, StrictModelTypeError
from smc.engine.models import (
    CandidateSetup,
    MarketRegime,
    StrategyEvaluation,
    _freeze,
    _parse_timestamp,
    _unfreeze,
    _validate_non_negative_int,
    _validate_str,
)


@dataclass(frozen=True)
class EvidenceCluster:
    """
    Immutable representation of an evidence-backed cluster of candidate setups
    sharing the same trading opportunity.
    """
    cluster_id: str
    direction: Literal["BUY", "SELL"]
    members: tuple[StrategyEvaluation, ...]
    strategy_ids: tuple[str, ...] = field(default_factory=tuple)
    evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    overlap_evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        cid = _validate_str(self.cluster_id, "cluster_id")
        object.__setattr__(self, "cluster_id", cid)

        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"Invalid direction '{self.direction}'. Must be 'BUY' or 'SELL'.")

        if not isinstance(self.members, (tuple, list)):
            raise StrictModelTypeError(
                f"members must be a sequence of StrategyEvaluation, got {type(self.members).__name__}"
            )
        if len(self.members) == 0:
            raise ValueError("EvidenceCluster must have at least one member.")

        converted_members = []
        seen_setup_ids: set[str] = set()
        for idx, m in enumerate(self.members):
            if isinstance(m, dict):
                m = StrategyEvaluation.from_dict(m)
            elif not isinstance(m, StrategyEvaluation):
                raise StrictModelTypeError(
                    f"Member at index {idx} must be StrategyEvaluation, got {type(m).__name__}"
                )

            cand = getattr(m, "candidate", getattr(m, "setup", None))
            sid = cand.setup_id if cand is not None else None

            if m.status != "ELIGIBLE":
                raise ValueError(
                    f"Member setup '{sid}' is REJECTED; only ELIGIBLE evaluations can be in EvidenceCluster."
                )

            if m.candidate.direction != self.direction:
                raise ValueError(
                    f"Member setup '{sid}' direction '{m.candidate.direction}' "
                    f"does not match cluster direction '{self.direction}'."
                )

            if sid in seen_setup_ids:
                raise ValueError(f"duplicate setup_id in EvidenceCluster: '{sid}'")
            seen_setup_ids.add(sid)

            converted_members.append(m)

        def _validate_derived_field(name: str, given: Any, expected: tuple[str, ...]) -> tuple[str, ...]:
            if not isinstance(given, (tuple, list)):
                raise StrictModelTypeError(f"'{name}' must be a sequence of strings, got {type(given).__name__}")
            seen = set()
            for x in given:
                if not isinstance(x, str):
                    raise StrictModelTypeError(f"Items in '{name}' must be strings, got {type(x).__name__}")
                if x in seen:
                    raise ValueError(f"Duplicate ID '{x}' in '{name}'.")
                seen.add(x)
            sorted_given = tuple(sorted(seen))
            if sorted_given != expected:
                raise ValueError(
                    f"Tampered or inconsistent '{name}': payload has {list(given)} but derived from members is {list(expected)}"
                )
            return expected

        # Canonical sort: (strategy_id, setup_id)
        sorted_members = tuple(sorted(converted_members, key=lambda m: (m.candidate.strategy_id, m.candidate.setup_id)))
        object.__setattr__(self, "members", sorted_members)

        # Canonical unique sorted strategy_ids
        strat_ids = tuple(sorted(set(m.candidate.strategy_id for m in sorted_members)))
        if self.strategy_ids is not None and len(self.strategy_ids) > 0:
            object.__setattr__(self, "strategy_ids", _validate_derived_field("strategy_ids", self.strategy_ids, strat_ids))
        else:
            object.__setattr__(self, "strategy_ids", strat_ids)

        # Canonical unique sorted union of all evidence_ids
        all_ev_ids = set()
        ev_counter: Counter[str] = Counter()
        for m in sorted_members:
            member_eids = set()
            for ev in m.candidate.evidences:
                all_ev_ids.add(ev.evidence_id)
                member_eids.add(ev.evidence_id)
            for eid in member_eids:
                ev_counter[eid] += 1

        calc_ev_ids = tuple(sorted(all_ev_ids))
        if self.evidence_ids is not None and len(self.evidence_ids) > 0:
            object.__setattr__(self, "evidence_ids", _validate_derived_field("evidence_ids", self.evidence_ids, calc_ev_ids))
        else:
            object.__setattr__(self, "evidence_ids", calc_ev_ids)

        calc_overlap_ids = tuple(sorted(eid for eid, count in ev_counter.items() if count >= 2))
        if self.overlap_evidence_ids is not None and len(self.overlap_evidence_ids) > 0:
            object.__setattr__(self, "overlap_evidence_ids", _validate_derived_field("overlap_evidence_ids", self.overlap_evidence_ids, calc_overlap_ids))
        else:
            object.__setattr__(self, "overlap_evidence_ids", calc_overlap_ids)

        # Deep immutable meta
        object.__setattr__(self, "meta", _freeze(self.meta))

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "direction": self.direction,
            "members": [m.to_dict() for m in self.members],
            "strategy_ids": list(self.strategy_ids),
            "evidence_ids": list(self.evidence_ids),
            "overlap_evidence_ids": list(self.overlap_evidence_ids),
            "meta": _unfreeze(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceCluster:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for EvidenceCluster, got {type(data).__name__}")
        allowed = {"cluster_id", "direction", "members", "strategy_ids", "evidence_ids", "overlap_evidence_ids", "meta"}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise StrictModelTypeError(f"Unknown fields in EvidenceCluster: {sorted(unknown)}")
        for req in ("cluster_id", "direction", "members"):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in EvidenceCluster.")
        members = tuple(
            StrategyEvaluation.from_dict(m) if isinstance(m, dict) else m
            for m in data["members"]
        )
        seen_setup_ids_dict = set()
        for m in members:
            cand = getattr(m, "candidate", getattr(m, "setup", None))
            sid = cand.setup_id if cand is not None else None
            if sid in seen_setup_ids_dict:
                raise ValueError(f"duplicate setup_id in EvidenceCluster: '{sid}'")
            seen_setup_ids_dict.add(sid)

        def _check_derived(name: str, given: Any, expected: set[str]) -> tuple[str, ...]:
            if not isinstance(given, (tuple, list)):
                raise StrictModelTypeError(f"'{name}' must be a sequence of strings, got {type(given).__name__}")
            seen = set()
            for x in given:
                if not isinstance(x, str):
                    raise StrictModelTypeError(f"Items in '{name}' must be strings, got {type(x).__name__}")
                if x in seen:
                    raise ValueError(f"Duplicate ID '{x}' in '{name}'.")
                seen.add(x)
            if seen != expected:
                raise ValueError(
                    f"Tampered or inconsistent '{name}': payload has {list(given)} but derived from members is {sorted(expected)}"
                )
            return tuple(sorted(seen))

        # Pre-validate derived fields if present in data
        kwargs: dict[str, Any] = {
            "cluster_id": data["cluster_id"],
            "direction": data["direction"],
            "members": members,
            "meta": data.get("meta", {}),
        }
        if "strategy_ids" in data:
            strat_expected = set(m.candidate.strategy_id for m in members)
            kwargs["strategy_ids"] = _check_derived("strategy_ids", data["strategy_ids"], strat_expected)
        if "evidence_ids" in data:
            all_ev_ids = set()
            for m in members:
                for ev in m.candidate.evidences:
                    all_ev_ids.add(ev.evidence_id)
            kwargs["evidence_ids"] = _check_derived("evidence_ids", data["evidence_ids"], all_ev_ids)
        if "overlap_evidence_ids" in data:
            ev_counter: Counter[str] = Counter()
            for m in members:
                member_eids = set(ev.evidence_id for ev in m.candidate.evidences)
                for eid in member_eids:
                    ev_counter[eid] += 1
            overlap_expected = set(eid for eid, count in ev_counter.items() if count >= 2)
            kwargs["overlap_evidence_ids"] = _check_derived("overlap_evidence_ids", data["overlap_evidence_ids"], overlap_expected)

        return cls(**kwargs)


@dataclass(frozen=True)
class DirectionConflict:
    """
    Immutable representation of simultaneous BUY and SELL eligible clusters occurring at bar N.
    """
    bar_index: int
    buy_cluster_ids: tuple[str, ...]
    sell_cluster_ids: tuple[str, ...]
    reason: str = "conflicting_direction"

    def __post_init__(self) -> None:
        b_idx = _validate_non_negative_int(self.bar_index, "bar_index")
        object.__setattr__(self, "bar_index", b_idx)

        if not isinstance(self.buy_cluster_ids, (tuple, list)):
            raise StrictModelTypeError(f"buy_cluster_ids must be a sequence, got {type(self.buy_cluster_ids).__name__}")
        if not isinstance(self.sell_cluster_ids, (tuple, list)):
            raise StrictModelTypeError(f"sell_cluster_ids must be a sequence, got {type(self.sell_cluster_ids).__name__}")

        if len(self.buy_cluster_ids) == 0:
            raise ValueError("DirectionConflict must have at least one buy_cluster_id.")
        if len(self.sell_cluster_ids) == 0:
            raise ValueError("DirectionConflict must have at least one sell_cluster_id.")

        seen_buy = set()
        for cid in self.buy_cluster_ids:
            cid_str = _validate_str(cid, "buy_cluster_id")
            if cid_str in seen_buy:
                raise ValueError(f"Duplicate cluster_id '{cid_str}' in buy_cluster_ids.")
            seen_buy.add(cid_str)

        seen_sell = set()
        for cid in self.sell_cluster_ids:
            cid_str = _validate_str(cid, "sell_cluster_id")
            if cid_str in seen_sell:
                raise ValueError(f"Duplicate cluster_id '{cid_str}' in sell_cluster_ids.")
            seen_sell.add(cid_str)

        buy_cids = tuple(sorted(seen_buy))
        sell_cids = tuple(sorted(seen_sell))

        overlap = set(buy_cids).intersection(set(sell_cids))
        if overlap:
            raise ValueError(f"buy_cluster_ids and sell_cluster_ids cannot overlap: {sorted(overlap)}")

        object.__setattr__(self, "buy_cluster_ids", buy_cids)
        object.__setattr__(self, "sell_cluster_ids", sell_cids)

        r_str = _validate_str(self.reason, "reason")
        if r_str != "conflicting_direction":
            raise ValueError(f"Invalid reason '{r_str}'. Must be 'conflicting_direction'.")
        object.__setattr__(self, "reason", r_str)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar_index": self.bar_index,
            "buy_cluster_ids": list(self.buy_cluster_ids),
            "sell_cluster_ids": list(self.sell_cluster_ids),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DirectionConflict:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for DirectionConflict, got {type(data).__name__}")
        allowed = {"bar_index", "buy_cluster_ids", "sell_cluster_ids", "reason"}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise StrictModelTypeError(f"Unknown fields in DirectionConflict: {sorted(unknown)}")
        for req in ("bar_index", "buy_cluster_ids", "sell_cluster_ids"):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in DirectionConflict.")
        return cls(
            bar_index=data["bar_index"],
            buy_cluster_ids=tuple(data["buy_cluster_ids"]),
            sell_cluster_ids=tuple(data["sell_cluster_ids"]),
            reason=data.get("reason", "conflicting_direction"),
        )


@dataclass(frozen=True)
class ConfluenceBatch:
    """
    Immutable container of market regime, all candidate evaluations, eligible clusters,
    and any direction conflict for bar N.
    """
    bar_index: int
    timestamp: pd.Timestamp
    regime: MarketRegime
    evaluations: tuple[StrategyEvaluation, ...]
    eligible_clusters: tuple[EvidenceCluster, ...]
    direction_conflict: DirectionConflict | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        b_idx = _validate_non_negative_int(self.bar_index, "bar_index")
        object.__setattr__(self, "bar_index", b_idx)

        ts = _parse_timestamp(self.timestamp, "timestamp")
        object.__setattr__(self, "timestamp", ts)

        reg = self.regime
        if isinstance(reg, dict):
            reg = MarketRegime.from_dict(reg)
        elif not isinstance(reg, MarketRegime):
            raise StrictModelTypeError(f"regime must be MarketRegime, got {type(reg).__name__}")

        if reg.bar_index != b_idx:
            raise ValueError(
                f"regime.bar_index ({reg.bar_index}) does not match ConfluenceBatch.bar_index ({b_idx})"
            )
        if reg.timestamp != ts:
            raise ValueError(
                f"regime.timestamp ({reg.timestamp}) does not match ConfluenceBatch.timestamp ({ts})"
            )
        object.__setattr__(self, "regime", reg)

        if not isinstance(self.evaluations, (tuple, list)):
            raise StrictModelTypeError(
                f"evaluations must be a sequence, got {type(self.evaluations).__name__}"
            )
        conv_evals = []
        for idx, ev in enumerate(self.evaluations):
            if isinstance(ev, dict):
                ev = StrategyEvaluation.from_dict(ev)
            elif not isinstance(ev, StrategyEvaluation):
                raise StrictModelTypeError(
                    f"Evaluation at index {idx} must be StrategyEvaluation, got {type(ev).__name__}"
                )
            conv_evals.append(ev)

        # Enforce unique setup_ids in evaluations with payload integrity
        seen_setup_ids: dict[str, dict[str, Any]] = {}
        for ev in conv_evals:
            sid = ev.candidate.setup_id
            payload = ev.to_dict()
            if sid in seen_setup_ids:
                if seen_setup_ids[sid] != payload:
                    raise StrategyStateError(
                        f"Integrity error: Duplicate setup_id '{sid}' with conflicting payload in evaluations."
                    )
                raise ValueError(f"Duplicate setup_id '{sid}' in evaluations.")
            seen_setup_ids[sid] = payload

        # Canonical sort for evaluations: (strategy_id, setup_id)
        sorted_evals = tuple(sorted(conv_evals, key=lambda e: (e.candidate.strategy_id, e.candidate.setup_id)))
        object.__setattr__(self, "evaluations", sorted_evals)

        if not isinstance(self.eligible_clusters, (tuple, list)):
            raise StrictModelTypeError(
                f"eligible_clusters must be a sequence, got {type(self.eligible_clusters).__name__}"
            )
        conv_clusters = []
        for idx, cl in enumerate(self.eligible_clusters):
            if isinstance(cl, dict):
                cl = EvidenceCluster.from_dict(cl)
            elif not isinstance(cl, EvidenceCluster):
                raise StrictModelTypeError(
                    f"Cluster at index {idx} must be EvidenceCluster, got {type(cl).__name__}"
                )
            conv_clusters.append(cl)

        # Canonical sort for clusters: (direction, cluster_id)
        sorted_clusters = tuple(sorted(conv_clusters, key=lambda c: (c.direction, c.cluster_id)))
        object.__setattr__(self, "eligible_clusters", sorted_clusters)

        # Cross-validate eligible_clusters against evaluations (P1.6)
        eval_by_setup_id: dict[str, StrategyEvaluation] = {e.candidate.setup_id: e for e in conv_evals}
        eligible_eval_ids: set[str] = {e.candidate.setup_id for e in conv_evals if e.status == "ELIGIBLE"}
        rejected_eval_ids: set[str] = {e.candidate.setup_id for e in conv_evals if e.status != "ELIGIBLE"}

        seen_cluster_ids = set()
        clustered_setup_ids = set()
        for cl in sorted_clusters:
            if cl.cluster_id in seen_cluster_ids:
                raise ValueError(f"Duplicate cluster_id '{cl.cluster_id}' in eligible_clusters.")
            seen_cluster_ids.add(cl.cluster_id)

            for member in cl.members:
                msid = member.candidate.setup_id
                if member.status != "ELIGIBLE" or msid in rejected_eval_ids:
                    raise ValueError(f"Member '{msid}' in cluster '{cl.cluster_id}' is not ELIGIBLE.")

                if msid not in eval_by_setup_id:
                    raise ValueError(f"Member '{msid}' in cluster '{cl.cluster_id}' not found in evaluations.")

                if member.to_dict() != eval_by_setup_id[msid].to_dict():
                    raise StrategyStateError(
                        f"Member '{msid}' payload does not match evaluation in ConfluenceBatch."
                    )

                if msid in clustered_setup_ids:
                    raise ValueError(f"Candidate '{msid}' appears in multiple clusters.")
                clustered_setup_ids.add(msid)

        unclustered = eligible_eval_ids - clustered_setup_ids
        if unclustered:
            raise ValueError(f"Eligible evaluations not assigned to any cluster: {sorted(unclustered)}.")

        # Validate direction_conflict vs eligible_clusters
        buy_cids = tuple(sorted(c.cluster_id for c in sorted_clusters if c.direction == "BUY"))
        sell_cids = tuple(sorted(c.cluster_id for c in sorted_clusters if c.direction == "SELL"))
        has_conflict = len(buy_cids) > 0 and len(sell_cids) > 0

        conflict = self.direction_conflict
        if isinstance(conflict, dict):
            conflict = DirectionConflict.from_dict(conflict)

        if has_conflict:
            if conflict is None:
                raise ValueError(
                    "ConfluenceBatch has both BUY and SELL eligible clusters, so direction_conflict cannot be None."
                )
            if not isinstance(conflict, DirectionConflict):
                raise StrictModelTypeError(
                    f"direction_conflict must be DirectionConflict, got {type(conflict).__name__}"
                )
            if conflict.bar_index != b_idx:
                raise ValueError(
                    f"direction_conflict.bar_index ({conflict.bar_index}) does not match ConfluenceBatch.bar_index ({b_idx})"
                )
            if conflict.buy_cluster_ids != buy_cids:
                raise ValueError(
                    f"direction_conflict.buy_cluster_ids {conflict.buy_cluster_ids} does not match eligible BUY clusters {buy_cids}"
                )
            if conflict.sell_cluster_ids != sell_cids:
                raise ValueError(
                    f"direction_conflict.sell_cluster_ids {conflict.sell_cluster_ids} does not match eligible SELL clusters {sell_cids}"
                )
            object.__setattr__(self, "direction_conflict", conflict)
        else:
            if conflict is not None:
                raise ValueError(
                    "direction_conflict must be None when there are not both BUY and SELL eligible clusters."
                )
            object.__setattr__(self, "direction_conflict", None)

        object.__setattr__(self, "meta", _freeze(self.meta))

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar_index": self.bar_index,
            "timestamp": self.timestamp.isoformat(),
            "regime": self.regime.to_dict(),
            "evaluations": [e.to_dict() for e in self.evaluations],
            "eligible_clusters": [c.to_dict() for c in self.eligible_clusters],
            "direction_conflict": self.direction_conflict.to_dict() if self.direction_conflict else None,
            "meta": _unfreeze(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConfluenceBatch:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for ConfluenceBatch, got {type(data).__name__}")
        allowed = {"bar_index", "timestamp", "regime", "evaluations", "eligible_clusters", "direction_conflict", "meta"}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise StrictModelTypeError(f"Unknown fields in ConfluenceBatch: {sorted(unknown)}")
        for req in ("bar_index", "timestamp", "regime", "evaluations", "eligible_clusters"):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in ConfluenceBatch.")
        regime = MarketRegime.from_dict(data["regime"]) if isinstance(data["regime"], dict) else data["regime"]
        evals = tuple(
            StrategyEvaluation.from_dict(e) if isinstance(e, dict) else e
            for e in data["evaluations"]
        )
        clusters = tuple(
            EvidenceCluster.from_dict(c) if isinstance(c, dict) else c
            for c in data["eligible_clusters"]
        )
        conflict = (
            DirectionConflict.from_dict(data["direction_conflict"])
            if data.get("direction_conflict") is not None
            else None
        )
        return cls(
            bar_index=data["bar_index"],
            timestamp=data["timestamp"],
            regime=regime,
            evaluations=evals,
            eligible_clusters=clusters,
            direction_conflict=conflict,
            meta=data.get("meta", {}),
        )


def _extract_candidate_leg_and_zone(candidate: CandidateSetup) -> tuple[str | None, str | None]:
    """
    Extract canonical non-null structure leg ID and zone evidence ID.
    Returns (structure_leg_id, zone_evidence_id).
    """
    leg_id = candidate.meta.get("structure_leg_id")
    zone_id: str | None = None

    for ev in candidate.evidences:
        if leg_id is None and ev.kind in ("structure_event", "fair_value_gap", "order_block"):
            cand_leg = ev.details.get("structure_leg_id")
            if cand_leg is not None:
                leg_id = cand_leg
        if ev.kind in ("fair_value_gap", "order_block"):
            if zone_id is None:
                zone_id = ev.evidence_id

    return (str(leg_id) if leg_id is not None else None, str(zone_id) if zone_id is not None else None)


class EvidenceDeduplicator:
    """
    Stateless deduplicator that groups ELIGIBLE strategy evaluations into EvidenceClusters.
    """

    def deduplicate(
        self,
        evaluations: Sequence[StrategyEvaluation],
    ) -> tuple[EvidenceCluster, ...]:
        # 1. Deduplicate evaluations by setup_id with integrity check
        seen_evals_by_setup_id: dict[str, StrategyEvaluation] = {}
        for ev in evaluations:
            sid = ev.candidate.setup_id
            if sid in seen_evals_by_setup_id:
                existing = seen_evals_by_setup_id[sid]
                # Check payload equality
                if existing.to_dict() != ev.to_dict():
                    raise StrategyStateError(
                        f"Integrity error: Duplicate setup_id '{sid}' with conflicting payload."
                    )
                # Idempotent collapse: skip duplicate
                continue
            seen_evals_by_setup_id[sid] = ev

        unique_evals = list(seen_evals_by_setup_id.values())

        # 2. Filter for ELIGIBLE evaluations only
        eligible_evals = [e for e in unique_evals if e.status == "ELIGIBLE"]
        if not eligible_evals:
            return ()

        # 3. Check for same cluster_id with conflicting/opposite direction
        seen_cluster_directions: dict[str, str] = {}
        for e in eligible_evals:
            cid = e.candidate.evidence_cluster_id
            if cid:
                if cid in seen_cluster_directions:
                    existing_dir = seen_cluster_directions[cid]
                    if existing_dir != e.candidate.direction:
                        raise StrategyStateError(
                            f"Integrity error: Same cluster ID '{cid}' with opposite directions: "
                            f"'{existing_dir}' vs '{e.candidate.direction}'."
                        )
                else:
                    seen_cluster_directions[cid] = e.candidate.direction

        # 4. Connected components clustering
        n = len(eligible_evals)
        parent = list(range(n))

        def find(i: int) -> int:
            path = []
            while parent[i] != i:
                path.append(i)
                i = parent[i]
            for p in path:
                parent[p] = i
            return i

        def union(i: int, j: int) -> None:
            root_i = find(i)
            root_j = find(j)
            if root_i != root_j:
                parent[root_j] = root_i

        # Precompute tokens for each candidate
        cand_tokens = [
            (e.candidate.direction, e.candidate.evidence_cluster_id, _extract_candidate_leg_and_zone(e.candidate))
            for e in eligible_evals
        ]

        for i in range(n):
            dir_i, cid_i, (leg_i, zone_i) = cand_tokens[i]
            for j in range(i + 1, n):
                dir_j, cid_j, (leg_j, zone_j) = cand_tokens[j]
                if dir_i != dir_j:
                    continue

                # Match condition 1: same non-empty evidence_cluster_id
                if cid_i and cid_j and cid_i == cid_j:
                    union(i, j)
                    continue

                # Match condition 2: same non-null structure leg AND same zone evidence ID
                if leg_i is not None and zone_i is not None and leg_i == leg_j and zone_i == zone_j:
                    union(i, j)
                    continue

        # Group by component root
        groups: dict[int, list[StrategyEvaluation]] = {}
        for idx in range(n):
            root = find(idx)
            groups.setdefault(root, []).append(eligible_evals[idx])

        # 5. Build EvidenceCluster for each group
        clusters: list[EvidenceCluster] = []
        for root, group_members in groups.items():
            # Canonical cluster_id: min of non-empty evidence_cluster_ids, or fallback
            cids = [m.candidate.evidence_cluster_id for m in group_members if m.candidate.evidence_cluster_id]
            direction = group_members[0].candidate.direction
            if cids:
                canonical_cid = min(cids)
            else:
                canonical_cid = f"cluster_{direction}_{group_members[0].candidate.setup_id}"

            cluster = EvidenceCluster(
                cluster_id=canonical_cid,
                direction=direction,
                members=tuple(group_members),
            )
            clusters.append(cluster)

        # Sort clusters by (direction, cluster_id)
        return tuple(sorted(clusters, key=lambda c: (c.direction, c.cluster_id)))


class DirectionConflictDetector:
    """
    Stateless detector that checks if eligible clusters contain both BUY and SELL directions.
    """

    def detect(
        self,
        clusters: Sequence[EvidenceCluster],
        bar_index: int,
    ) -> DirectionConflict | None:
        _validate_non_negative_int(bar_index, "bar_index")

        buy_cids = tuple(sorted(c.cluster_id for c in clusters if c.direction == "BUY"))
        sell_cids = tuple(sorted(c.cluster_id for c in clusters if c.direction == "SELL"))

        if buy_cids and sell_cids:
            return DirectionConflict(
                bar_index=bar_index,
                buy_cluster_ids=buy_cids,
                sell_cluster_ids=sell_cids,
                reason="conflicting_direction",
            )
        return None


def build_confluence_batch(
    evaluations: Sequence[StrategyEvaluation],
    regime: MarketRegime,
    context: StrategyContext,
    meta: Mapping[str, Any] | None = None,
) -> ConfluenceBatch:
    """
    Helper function to assemble StrategyEvaluations, deduce clusters, detect direction conflicts,
    and package them into a canonical ConfluenceBatch for the current bar.
    """
    if regime.bar_index != context.bar_index:
        raise StrategyStateError(
            f"Preflight error: regime.bar_index ({regime.bar_index}) != context.bar_index ({context.bar_index})"
        )
    if regime.timestamp != context.bar_close_time:
        raise StrategyStateError(
            f"Preflight error: regime.timestamp ({regime.timestamp}) != context.bar_close_time ({context.bar_close_time})"
        )

    # Validate temporal horizon for all evaluations
    N = context.bar_index
    close_time = context.bar_close_time
    for ev in evaluations:
        cand = ev.candidate
        if cand.bar_index > N:
            raise StrategyStateError(
                f"Candidate setup '{cand.setup_id}' bar_index {cand.bar_index} is in the future relative to context bar {N}"
            )
        if cand.timestamp > close_time:
            raise StrategyStateError(
                f"Candidate setup '{cand.setup_id}' timestamp {cand.timestamp} is in the future relative to bar close {close_time}"
            )
        for e_ref in cand.evidences:
            if e_ref.bar_index > cand.bar_index:
                raise StrategyStateError(
                    f"Evidence '{e_ref.evidence_id}' bar_index {e_ref.bar_index} is after candidate bar {cand.bar_index}"
                )
            if e_ref.bar_index > N:
                raise StrategyStateError(
                    f"Evidence '{e_ref.evidence_id}' bar_index {e_ref.bar_index} is in the future relative to context bar {N}"
                )
            if e_ref.time is not None:
                if e_ref.time > cand.timestamp:
                    raise StrategyStateError(
                        f"Evidence '{e_ref.evidence_id}' time {e_ref.time} is after candidate timestamp {cand.timestamp}"
                    )
                if e_ref.time > close_time:
                    raise StrategyStateError(
                        f"Evidence '{e_ref.evidence_id}' time {e_ref.time} is in the future relative to bar close {close_time}"
                    )

    deduplicator = EvidenceDeduplicator()
    eligible_clusters = deduplicator.deduplicate(evaluations)

    conflict_detector = DirectionConflictDetector()
    conflict = conflict_detector.detect(eligible_clusters, context.bar_index)

    # Collapse duplicate evaluations canonically
    seen_evals_by_setup_id: dict[str, StrategyEvaluation] = {}
    for ev in evaluations:
        sid = ev.candidate.setup_id
        if sid in seen_evals_by_setup_id:
            existing = seen_evals_by_setup_id[sid]
            if existing.to_dict() != ev.to_dict():
                raise StrategyStateError(
                    f"Integrity error: Duplicate setup_id '{sid}' with conflicting payload."
                )
            continue
        seen_evals_by_setup_id[sid] = ev

    sorted_evals = tuple(
        sorted(seen_evals_by_setup_id.values(), key=lambda e: (e.candidate.strategy_id, e.candidate.setup_id))
    )

    return ConfluenceBatch(
        bar_index=context.bar_index,
        timestamp=context.bar_close_time,
        regime=regime,
        evaluations=sorted_evals,
        eligible_clusters=eligible_clusters,
        direction_conflict=conflict,
        meta=meta or {},
    )


__all__ = [
    "EvidenceCluster",
    "DirectionConflict",
    "ConfluenceBatch",
    "EvidenceDeduplicator",
    "DirectionConflictDetector",
    "build_confluence_batch",
]
