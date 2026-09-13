"""
smc.engine.selector
===================
Deterministic Strategy Selector, Ownership Resolution & Component Scoring Engine
for the SMC Strategy Framework (T53.8).

Responsibilities:
1. SelectorConfig: Frozen configuration for component weights and decision thresholds.
2. ClusterScorecard: Frozen, deeply immutable representation of scored clusters.
3. Component Scorers: Pure functions computing regime, setup, context, execution, and total scores.
4. DeterministicStrategySelector & select_strategy:
   - Evaluates eligible clusters and their member candidates.
   - Enforces cluster ownership (highest ranked member defines cluster score and primary strategy).
   - Evaluates direction conflict (BUY vs SELL gap < 15.0 -> NO_TRADE; >= 15.0 -> winner).
   - Enforces minimum total score threshold (>= 60.0).
   - Generates deterministic SelectionDecision and immutable SelectionAuditRecord.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, Optional, Sequence

import pandas as pd

from smc.engine.confluence import ConfluenceBatch, EvidenceCluster
from smc.engine.context import StrategyContext
from smc.engine.eligibility import get_regime_matrix_score
from smc.engine.errors import StrategyStateError, StrictModelTypeError
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
    make_decision_id,
)
from smc.engine.telemetry import SelectionAuditRecord


@dataclass(frozen=True)
class SelectorConfig:
    """
    Immutable configuration for the deterministic selector.

    Attributes:
        weight_regime: Weight for market regime component (default 0.25).
        weight_setup: Weight for setup quality and structural features (default 0.35).
        weight_context: Weight for HTF bias and session alignment (default 0.25).
        weight_exec: Weight for planned execution proxy (default 0.15).
        minimum_total_score: Minimum total score required to SELECT a setup (default 60.0).
        direction_conflict_gap: Minimum score advantage required between opposing directions (default 15.0).
        version: Canonical selector version token (default "selector-v1").
    """
    weight_regime: float = 0.25
    weight_setup: float = 0.35
    weight_context: float = 0.25
    weight_exec: float = 0.15
    minimum_total_score: float = 60.0
    direction_conflict_gap: float = 15.0
    version: str = "selector-v1"

    def __post_init__(self) -> None:
        wr = _validate_finite_float(self.weight_regime, "weight_regime", min_val=0.0, max_val=1.0)
        ws = _validate_finite_float(self.weight_setup, "weight_setup", min_val=0.0, max_val=1.0)
        wc = _validate_finite_float(self.weight_context, "weight_context", min_val=0.0, max_val=1.0)
        we = _validate_finite_float(self.weight_exec, "weight_exec", min_val=0.0, max_val=1.0)
        total_weight = wr + ws + wc + we
        if abs(total_weight - 1.0) > 1e-9:
            raise ValueError(f"Weights must sum to 1.0, got {total_weight:.6f}")

        min_score = _validate_finite_float(self.minimum_total_score, "minimum_total_score", min_val=0.0, max_val=100.0)
        gap = _validate_finite_float(self.direction_conflict_gap, "direction_conflict_gap", min_val=0.0, max_val=100.0)
        ver = _validate_base_token(self.version, "version")

        object.__setattr__(self, "weight_regime", wr)
        object.__setattr__(self, "weight_setup", ws)
        object.__setattr__(self, "weight_context", wc)
        object.__setattr__(self, "weight_exec", we)
        object.__setattr__(self, "minimum_total_score", round(min_score, 2))
        object.__setattr__(self, "direction_conflict_gap", round(gap, 2))
        object.__setattr__(self, "version", ver)

    @property
    def scoring_weights(self) -> tuple[float, float, float, float]:
        return (self.weight_regime, self.weight_setup, self.weight_context, self.weight_exec)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "weight_regime": float(self.weight_regime),
            "weight_setup": float(self.weight_setup),
            "weight_context": float(self.weight_context),
            "weight_exec": float(self.weight_exec),
            "minimum_total_score": float(self.minimum_total_score),
            "direction_conflict_gap": float(self.direction_conflict_gap),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SelectorConfig:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for SelectorConfig, got {type(data).__name__}")
        allowed = {
            "version", "weight_regime", "weight_setup", "weight_context", "weight_exec",
            "minimum_total_score", "direction_conflict_gap",
        }
        unknown = set(data.keys()) - allowed
        if unknown:
            raise KeyError(f"Unknown fields in SelectorConfig: {sorted(unknown)}")
        return cls(
            version=data.get("version", "selector-v1"),
            weight_regime=data.get("weight_regime", 0.25),
            weight_setup=data.get("weight_setup", 0.35),
            weight_context=data.get("weight_context", 0.25),
            weight_exec=data.get("weight_exec", 0.15),
            minimum_total_score=data.get("minimum_total_score", 60.0),
            direction_conflict_gap=data.get("direction_conflict_gap", 15.0),
        )


@dataclass(frozen=True)
class ClusterScorecard:
    """
    Immutable representation of an evidence cluster's scoring results and ownership.
    Enforces strict component score recomputation, membership invariants, and canonical sorting.
    """
    cluster_id: str
    direction: Literal["BUY", "SELL"]
    primary_strategy_id: str
    primary_setup_id: str
    supporting_strategy_ids: tuple[str, ...]
    total_score: float
    regime_score: float
    setup_score: float
    context_score: float
    exec_score: float
    planned_rr: float
    member_count: int
    scoring_weights: tuple[float, float, float, float] = (0.25, 0.35, 0.25, 0.15)
    member_setup_ids: tuple[str, ...] = field(default_factory=tuple)
    member_scores: Mapping[str, float] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        cid = _validate_str(self.cluster_id, "cluster_id")
        object.__setattr__(self, "cluster_id", cid)

        if self.direction not in {"BUY", "SELL"}:
            raise ValueError(f"Invalid direction '{self.direction}'. Must be 'BUY' or 'SELL'.")

        strat = _validate_str(self.primary_strategy_id, "primary_strategy_id")
        object.__setattr__(self, "primary_strategy_id", strat)

        setup = _validate_str(self.primary_setup_id, "primary_setup_id")
        object.__setattr__(self, "primary_setup_id", setup)

        # Validate scoring_weights strictly
        if not isinstance(self.scoring_weights, (tuple, list)) or len(self.scoring_weights) != 4:
            raise StrictModelTypeError("scoring_weights must be a sequence of 4 floats")
        parsed_w = []
        for idx, w in enumerate(self.scoring_weights):
            w_val = _validate_finite_float(w, f"scoring_weights[{idx}]", min_val=0.0, max_val=1.0)
            parsed_w.append(w_val)
        if abs(sum(parsed_w) - 1.0) > 1e-9:
            raise ValueError(f"scoring_weights must sum to 1.0, got {sum(parsed_w):.6f}")
        w_tuple = tuple(parsed_w)
        object.__setattr__(self, "scoring_weights", w_tuple)

        # Validate supporting_strategy_ids: unique, canonical sorted, does not contain primary
        if not isinstance(self.supporting_strategy_ids, (tuple, list)):
            raise StrictModelTypeError(f"supporting_strategy_ids must be a sequence, got {type(self.supporting_strategy_ids).__name__}")
        supp_raw = list(self.supporting_strategy_ids)
        supp = []
        seen_supp = set()
        for s in supp_raw:
            s_str = _validate_str(s, "supporting_strategy_id")
            if s_str == strat:
                raise ValueError(f"supporting_strategy_ids cannot contain primary_strategy_id '{strat}'")
            if s_str in seen_supp:
                raise ValueError(f"Duplicate strategy ID in supporting_strategy_ids: '{s_str}'")
            seen_supp.add(s_str)
            supp.append(s_str)
        if tuple(supp_raw) != tuple(sorted(supp)):
            raise ValueError(f"supporting_strategy_ids must be canonically sorted, got {supp_raw}")
        object.__setattr__(self, "supporting_strategy_ids", tuple(supp))

        # Member setup IDs: unique, canonical sorted, contains primary_setup_id
        if not isinstance(self.member_setup_ids, (tuple, list)):
            raise StrictModelTypeError(f"member_setup_ids must be a sequence, got {type(self.member_setup_ids).__name__}")
        m_ids_raw = list(self.member_setup_ids)
        m_ids = []
        seen_m = set()
        for m in m_ids_raw:
            m_str = _validate_str(m, "member_setup_id")
            if m_str in seen_m:
                raise ValueError(f"Duplicate setup ID in member_setup_ids: '{m_str}'")
            seen_m.add(m_str)
            m_ids.append(m_str)
        if tuple(m_ids_raw) != tuple(sorted(m_ids)):
            raise ValueError(f"member_setup_ids must be canonically sorted, got {m_ids_raw}")
        if setup not in seen_m:
            raise ValueError(f"primary_setup_id '{setup}' must be in member_setup_ids")
        object.__setattr__(self, "member_setup_ids", tuple(m_ids))

        mc = _validate_non_negative_int(self.member_count, "member_count")
        if mc != len(m_ids):
            raise ValueError(f"member_count ({mc}) != len(member_setup_ids) ({len(m_ids)})")
        object.__setattr__(self, "member_count", mc)

        # Validate member_scores: mapping, keys exactly match member_setup_ids
        if not isinstance(self.member_scores, (dict, Mapping)):
            raise StrictModelTypeError(f"member_scores must be a mapping, got {type(self.member_scores).__name__}")
        parsed_m_scores = {}
        for k, v in self.member_scores.items():
            k_str = _validate_str(k, "member_scores key")
            v_val = _validate_finite_float(v, f"member_scores['{k}']", min_val=0.0, max_val=100.0)
            parsed_m_scores[k_str] = round(v_val, 2)
        if set(parsed_m_scores.keys()) != set(m_ids):
            raise ValueError(
                f"member_scores keys {sorted(parsed_m_scores.keys())} do not match "
                f"member_setup_ids {sorted(m_ids)}"
            )

        t = _validate_finite_float(self.total_score, "total_score", min_val=0.0, max_val=100.0)
        r = _validate_finite_float(self.regime_score, "regime_score", min_val=0.0, max_val=100.0)
        s = _validate_finite_float(self.setup_score, "setup_score", min_val=0.0, max_val=100.0)
        c = _validate_finite_float(self.context_score, "context_score", min_val=0.0, max_val=100.0)
        e = _validate_finite_float(self.exec_score, "exec_score", min_val=0.0, max_val=100.0)
        rr = _validate_finite_float(self.planned_rr, "planned_rr", min_val=0.0)

        # Recompute total_score from weights and component scores
        expected_total = round(
            w_tuple[0] * r
            + w_tuple[1] * s
            + w_tuple[2] * c
            + w_tuple[3] * e,
            2,
        )
        if abs(t - expected_total) > 1e-6:
            raise ValueError(f"total_score {t} does not match expected recomputed score {expected_total}")

        if abs(parsed_m_scores[setup] - t) > 1e-6:
            raise ValueError(
                f"member_scores[primary_setup_id] ({parsed_m_scores[setup]}) != total_score ({t})"
            )

        # Option B: Total-score primary ownership invariant
        # Standalone scorecard validates that primary setup has the maximal score among members.
        # Full multi-tier tie-breaking (planned_rr, strategy_id, setup_id) is enforced by SelectorOutput.
        max_m_score = max(parsed_m_scores.values())
        if parsed_m_scores[setup] < max_m_score - 1e-6:
            raise ValueError(
                f"primary_setup_id '{setup}' score ({parsed_m_scores[setup]}) is less than "
                f"maximum member score ({max_m_score}) in cluster '{cid}'"
            )

        object.__setattr__(self, "total_score", round(t, 2))
        object.__setattr__(self, "regime_score", round(r, 2))
        object.__setattr__(self, "setup_score", round(s, 2))
        object.__setattr__(self, "context_score", round(c, 2))
        object.__setattr__(self, "exec_score", round(e, 2))
        object.__setattr__(self, "planned_rr", round(rr, 4))
        object.__setattr__(self, "member_scores", _freeze(parsed_m_scores))
        object.__setattr__(self, "details", _freeze(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "direction": self.direction,
            "primary_strategy_id": self.primary_strategy_id,
            "primary_setup_id": self.primary_setup_id,
            "supporting_strategy_ids": list(self.supporting_strategy_ids),
            "total_score": float(self.total_score),
            "regime_score": float(self.regime_score),
            "setup_score": float(self.setup_score),
            "context_score": float(self.context_score),
            "exec_score": float(self.exec_score),
            "planned_rr": float(self.planned_rr),
            "member_count": int(self.member_count),
            "scoring_weights": [float(w) for w in self.scoring_weights],
            "member_setup_ids": list(self.member_setup_ids),
            "member_scores": _unfreeze(self.member_scores),
            "details": _unfreeze(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterScorecard:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for ClusterScorecard, got {type(data).__name__}")
        allowed = {
            "cluster_id", "direction", "primary_strategy_id", "primary_setup_id",
            "supporting_strategy_ids", "total_score", "regime_score",
            "setup_score", "context_score", "exec_score", "planned_rr",
            "member_count", "scoring_weights", "member_setup_ids", "member_scores", "details",
        }
        unknown = set(data.keys()) - allowed
        if unknown:
            raise KeyError(f"Unknown fields in ClusterScorecard: {sorted(unknown)}")
        for req in (
            "cluster_id", "direction", "primary_strategy_id", "primary_setup_id",
            "total_score", "regime_score", "setup_score", "context_score",
            "exec_score", "planned_rr", "member_count", "scoring_weights",
            "member_setup_ids", "member_scores",
        ):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in ClusterScorecard.")
        return cls(
            cluster_id=data["cluster_id"],
            direction=data["direction"],
            primary_strategy_id=data["primary_strategy_id"],
            primary_setup_id=data["primary_setup_id"],
            supporting_strategy_ids=tuple(data.get("supporting_strategy_ids", ())),
            total_score=data["total_score"],
            regime_score=data["regime_score"],
            setup_score=data["setup_score"],
            context_score=data["context_score"],
            exec_score=data["exec_score"],
            planned_rr=data["planned_rr"],
            member_count=data["member_count"],
            scoring_weights=tuple(data["scoring_weights"]),
            member_setup_ids=tuple(data["member_setup_ids"]),
            member_scores=data["member_scores"],
            details=data.get("details", {}),
        )


@dataclass(frozen=True)
class SelectorOutput:
    """
    Immutable container returning the complete selector decision, audit record, and scorecards.
    Enforces comprehensive cross-field validation between decision, audit record, evaluations, and scorecards.
    """
    decision: SelectionDecision
    audit_record: SelectionAuditRecord
    scorecards: tuple[ClusterScorecard, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.decision, dict):
            object.__setattr__(self, "decision", SelectionDecision.from_dict(self.decision))
        elif not isinstance(self.decision, SelectionDecision):
            raise StrictModelTypeError(f"decision must be SelectionDecision, got {type(self.decision).__name__}")

        if isinstance(self.audit_record, dict):
            object.__setattr__(self, "audit_record", SelectionAuditRecord.from_dict(self.audit_record))
        elif not isinstance(self.audit_record, SelectionAuditRecord):
            raise StrictModelTypeError(f"audit_record must be SelectionAuditRecord, got {type(self.audit_record).__name__}")

        if not isinstance(self.scorecards, (list, tuple)):
            raise StrictModelTypeError(f"scorecards must be a sequence, got {type(self.scorecards).__name__}")

        sc_list = []
        for sc in self.scorecards:
            if isinstance(sc, dict):
                sc = ClusterScorecard.from_dict(sc)
            elif not isinstance(sc, ClusterScorecard):
                raise StrictModelTypeError(f"scorecards element must be ClusterScorecard, got {type(sc).__name__}")
            sc_list.append(sc)
        sc_tuple = tuple(sc_list)
        object.__setattr__(self, "scorecards", sc_tuple)

        dec = self.decision
        aud = self.audit_record

        # 1. Base cross-field validations between decision and audit
        if dec.decision_id != aud.decision_id:
            raise ValueError(f"decision_id mismatch: decision '{dec.decision_id}' vs audit '{aud.decision_id}'")
        if dec.bar_index != aud.bar_index:
            raise ValueError(f"bar_index mismatch: decision {dec.bar_index} vs audit {aud.bar_index}")
        if dec.timestamp != aud.timestamp:
            raise ValueError(f"timestamp mismatch: decision {dec.timestamp} vs audit {aud.timestamp}")
        if dec.action != aud.action:
            raise ValueError(f"action mismatch: decision '{dec.action}' vs audit '{aud.action}'")
        if dec.reason != aud.reason:
            raise ValueError(f"reason mismatch: decision '{dec.reason}' vs audit '{aud.reason}'")
        if dec.primary_strategy_id != aud.primary_strategy_id:
            raise ValueError(f"primary_strategy_id mismatch: decision '{dec.primary_strategy_id}' vs audit '{aud.primary_strategy_id}'")
        if dec.supporting_strategy_ids != aud.supporting_strategy_ids:
            raise ValueError(f"supporting_strategy_ids mismatch: decision {dec.supporting_strategy_ids} vs audit {aud.supporting_strategy_ids}")
        if dec.score_gap != aud.score_gap:
            raise ValueError(f"score_gap mismatch: decision {dec.score_gap} vs audit {aud.score_gap}")

        dec_regime_str = dec.regime.regime if dec.regime is not None else None
        if dec_regime_str != aud.regime:
            raise ValueError(f"regime mismatch: decision '{dec_regime_str}' vs audit '{aud.regime}'")

        if dec.action == "SELECT":
            if dec.selected_setup is None:
                raise ValueError("decision with action='SELECT' must have selected_setup")
            if aud.primary_setup_id != dec.selected_setup.setup_id:
                raise ValueError(f"primary_setup_id mismatch: audit '{aud.primary_setup_id}' vs decision setup '{dec.selected_setup.setup_id}'")
            if aud.direction != dec.selected_setup.direction:
                raise ValueError(f"direction mismatch: audit '{aud.direction}' vs decision setup '{dec.selected_setup.direction}'")
            if aud.cluster_id is None:
                raise ValueError("action='SELECT' requires audit_record.cluster_id")
        else:  # NO_TRADE
            if aud.primary_setup_id is not None:
                raise ValueError("audit with action='NO_TRADE' cannot have primary_setup_id")
            if aud.direction is not None:
                raise ValueError("audit with action='NO_TRADE' cannot have direction")
            if aud.cluster_id is not None:
                raise ValueError("audit with action='NO_TRADE' cannot have cluster_id")

        # 2. Config consistency across decision.meta, audit_record, and scorecards
        req_meta_fields = {
            "selector_version", "minimum_total_score", "minimum_direction_gap",
            "scoring_weights", "execution_score_basis", "fvg_atr_basis",
            "symbol", "timeframe",
        }
        for field_name in req_meta_fields:
            if field_name not in dec.meta:
                raise KeyError(f"decision.meta missing required field '{field_name}'")

        if dec.meta["selector_version"] != aud.selector_version:
            raise ValueError(f"selector_version mismatch: decision.meta '{dec.meta['selector_version']}' vs audit '{aud.selector_version}'")
        if abs(float(dec.meta["minimum_total_score"]) - float(aud.minimum_total_score)) > 1e-6:
            raise ValueError(f"minimum_total_score mismatch: decision.meta {dec.meta['minimum_total_score']} vs audit {aud.minimum_total_score}")
        if abs(float(dec.meta["minimum_direction_gap"]) - float(aud.minimum_direction_gap)) > 1e-6:
            raise ValueError(f"minimum_direction_gap mismatch: decision.meta {dec.meta['minimum_direction_gap']} vs audit {aud.minimum_direction_gap}")
        if tuple(float(w) for w in dec.meta["scoring_weights"]) != tuple(float(w) for w in aud.scoring_weights):
            raise ValueError(f"scoring_weights mismatch: decision.meta {dec.meta['scoring_weights']} vs audit {aud.scoring_weights}")
        if dec.meta["symbol"] != aud.symbol:
            raise ValueError(f"symbol mismatch: decision.meta '{dec.meta['symbol']}' vs audit '{aud.symbol}'")
        if dec.meta["timeframe"] != aud.timeframe:
            raise ValueError(f"timeframe mismatch: decision.meta '{dec.meta['timeframe']}' vs audit '{aud.timeframe}'")
        if dec.meta["execution_score_basis"] != "planned_rr_pre_fill":
            raise ValueError(f"execution_score_basis must be 'planned_rr_pre_fill', got '{dec.meta['execution_score_basis']}'")
        if dec.meta["fvg_atr_basis"] != "signal_bar_atr14":
            raise ValueError(f"fvg_atr_basis must be 'signal_bar_atr14', got '{dec.meta['fvg_atr_basis']}'")

        for sc in sc_tuple:
            if sc.scoring_weights != aud.scoring_weights:
                raise ValueError(f"Scorecard '{sc.cluster_id}' scoring_weights {sc.scoring_weights} != audit {aud.scoring_weights}")
            if sc.details.get("execution_score_basis") != "planned_rr_pre_fill":
                raise ValueError(f"Scorecard '{sc.cluster_id}' execution_score_basis must be 'planned_rr_pre_fill'")
            if sc.details.get("fvg_atr_basis") != "signal_bar_atr14":
                raise ValueError(f"Scorecard '{sc.cluster_id}' fvg_atr_basis must be 'signal_bar_atr14'")

        # 3. Evaluation counts and gate reason histogram recomputed from decision.evaluations
        real_evaluated_count = len(dec.evaluations)
        real_eligible_count = sum(1 for ev in dec.evaluations if ev.status == "ELIGIBLE")
        real_rejected_count = sum(1 for ev in dec.evaluations if ev.status == "REJECTED")

        if aud.evaluated_count != real_evaluated_count:
            raise ValueError(f"evaluated_count mismatch: audit {aud.evaluated_count} != evaluations count {real_evaluated_count}")
        if aud.eligible_count != real_eligible_count:
            raise ValueError(f"eligible_count mismatch: audit {aud.eligible_count} != eligible evaluations count {real_eligible_count}")
        if aud.rejected_count != real_rejected_count:
            raise ValueError(f"rejected_count mismatch: audit {aud.rejected_count} != rejected evaluations count {real_rejected_count}")

        recomputed_gate_counts = dict(Counter(r for ev in dec.evaluations for r in ev.rejection_reasons))
        if dict(aud.gate_reason_counts) != recomputed_gate_counts:
            raise ValueError(f"gate_reason_counts mismatch: audit {dict(aud.gate_reason_counts)} != recomputed {recomputed_gate_counts}")

        # Verify cluster_scorecards match exactly
        if aud.cluster_scorecards != sc_tuple:
            raise ValueError("audit_record.cluster_scorecards must equal SelectorOutput.scorecards")

        # Cluster IDs unique
        seen_cluster_ids = set()
        for sc in sc_tuple:
            if sc.cluster_id in seen_cluster_ids:
                raise ValueError(f"Duplicate cluster_id '{sc.cluster_id}' in scorecards")
            seen_cluster_ids.add(sc.cluster_id)

        # 4. Scorecard consistency with evaluations mapping
        eval_by_setup = {ev.candidate.setup_id: ev for ev in dec.evaluations}
        eligible_setups = {ev.candidate.setup_id for ev in dec.evaluations if ev.status == "ELIGIBLE"}
        all_scorecard_member_setups: set[str] = set()

        for sc in sc_tuple:
            sc_member_evals = []
            for m_id in sc.member_setup_ids:
                if m_id not in eval_by_setup:
                    raise ValueError(f"Scorecard member '{m_id}' does not exist in decision evaluations")
                m_ev = eval_by_setup[m_id]
                if m_ev.status != "ELIGIBLE":
                    raise ValueError(f"Scorecard member '{m_id}' is not ELIGIBLE in evaluations (status='{m_ev.status}')")
                if m_id in all_scorecard_member_setups:
                    raise ValueError(f"Setup '{m_id}' appears in multiple scorecards")
                all_scorecard_member_setups.add(m_id)
                sc_member_evals.append(m_ev)

                # Member score must match evaluation total score
                if abs(sc.member_scores[m_id] - m_ev.total_score) > 1e-6:
                    raise ValueError(f"member_score[{m_id}] ({sc.member_scores[m_id]}) != evaluation total_score ({m_ev.total_score})")

            # Primary member must exist in scorecard
            if sc.primary_setup_id not in sc.member_setup_ids:
                raise ValueError(f"primary_setup_id '{sc.primary_setup_id}' not in member_setup_ids of scorecard '{sc.cluster_id}'")

            p_ev = eval_by_setup[sc.primary_setup_id]
            if sc.primary_strategy_id != p_ev.candidate.strategy_id:
                raise ValueError(f"Scorecard primary_strategy_id '{sc.primary_strategy_id}' != evaluation '{p_ev.candidate.strategy_id}'")
            if sc.direction != p_ev.candidate.direction:
                raise ValueError(f"Scorecard direction '{sc.direction}' != evaluation '{p_ev.candidate.direction}'")
            if abs(sc.planned_rr - p_ev.candidate.planned_rr) > 1e-4:
                raise ValueError(f"Scorecard planned_rr {sc.planned_rr} != evaluation planned_rr {p_ev.candidate.planned_rr}")
            if abs(sc.regime_score - p_ev.regime_score) > 1e-6:
                raise ValueError(f"Scorecard regime_score {sc.regime_score} != evaluation regime_score {p_ev.regime_score}")
            if abs(sc.setup_score - p_ev.setup_score) > 1e-6:
                raise ValueError(f"Scorecard setup_score {sc.setup_score} != evaluation setup_score {p_ev.setup_score}")
            if abs(sc.context_score - p_ev.context_score) > 1e-6:
                raise ValueError(f"Scorecard context_score {sc.context_score} != evaluation context_score {p_ev.context_score}")
            if abs(sc.exec_score - p_ev.exec_score) > 1e-6:
                raise ValueError(f"Scorecard exec_score {sc.exec_score} != evaluation exec_score {p_ev.exec_score}")
            if abs(sc.total_score - p_ev.total_score) > 1e-6:
                raise ValueError(f"Scorecard total_score {sc.total_score} != evaluation total_score {p_ev.total_score}")

            # Primary member must actually win member_rank_key
            best_member_ev = min(sc_member_evals, key=member_rank_key)
            if sc.primary_setup_id != best_member_ev.candidate.setup_id:
                raise ValueError(
                    f"Scorecard '{sc.cluster_id}' primary '{sc.primary_setup_id}' does not win "
                    f"canonical member rank key over '{best_member_ev.candidate.setup_id}'"
                )

            # Derived supporting strategies and count
            expected_supporting = tuple(sorted(set(
                m.candidate.strategy_id for m in sc_member_evals
                if m.candidate.strategy_id != sc.primary_strategy_id
            )))
            if sc.supporting_strategy_ids != expected_supporting:
                raise ValueError(
                    f"Scorecard '{sc.cluster_id}' supporting strategies {sc.supporting_strategy_ids} "
                    f"!= derived {expected_supporting}"
                )
            if sc.member_count != len(sc_member_evals):
                raise ValueError(f"Scorecard '{sc.cluster_id}' member_count {sc.member_count} != derived {len(sc_member_evals)}")

        # All eligible evaluations must belong to scorecards
        if all_scorecard_member_setups != eligible_setups:
            missing = eligible_setups - all_scorecard_member_setups
            raise ValueError(f"Scorecard members do not cover all eligible evaluations: missing {missing}")

        # 5. Cluster ranking, Direction Conflict & Decision policy verification
        if not sc_tuple:
            if dec.action != "NO_TRADE":
                raise ValueError(f"Zero scorecards requires action='NO_TRADE', got '{dec.action}'")
            if dec.reason != "no_eligible_setup":
                raise ValueError(f"Zero scorecards requires reason='no_eligible_setup', got '{dec.reason}'")
            if dec.selected_setup is not None:
                raise ValueError("Zero scorecards cannot have selected_setup")
            if dec.score_gap is not None:
                raise ValueError("Zero scorecards must have score_gap=None")
        else:
            buy_scs = [sc for sc in sc_tuple if sc.direction == "BUY"]
            sell_scs = [sc for sc in sc_tuple if sc.direction == "SELL"]

            if buy_scs and sell_scs:
                best_buy = min(buy_scs, key=cluster_rank_key)
                best_sell = min(sell_scs, key=cluster_rank_key)
                expected_gap = round(abs(best_buy.total_score - best_sell.total_score), 2)
                if expected_gap < aud.minimum_direction_gap:
                    if dec.action != "NO_TRADE" or dec.reason != "conflicting_direction":
                        raise ValueError(
                            f"Score gap {expected_gap} < threshold {aud.minimum_direction_gap} "
                            f"must result in NO_TRADE/conflicting_direction, got {dec.action}/{dec.reason}"
                        )
                    if dec.score_gap is None or abs(dec.score_gap - expected_gap) > 1e-6:
                        raise ValueError(f"score_gap {dec.score_gap} does not match recomputed gap {expected_gap}")
                    if aud.score_gap is None or abs(aud.score_gap - expected_gap) > 1e-6:
                        raise ValueError(f"audit.score_gap {aud.score_gap} does not match recomputed gap {expected_gap}")
                else:
                    # Winner determination with gap >= threshold
                    if best_buy.total_score > best_sell.total_score:
                        expected_winner = best_buy
                    elif best_sell.total_score > best_buy.total_score:
                        expected_winner = best_sell
                    else:
                        expected_winner = min([best_buy, best_sell], key=cluster_rank_key)

                    if expected_winner.total_score < aud.minimum_total_score:
                        if dec.action != "NO_TRADE" or dec.reason != "insufficient_score":
                            raise ValueError(
                                f"Winning cluster score {expected_winner.total_score} < minimum {aud.minimum_total_score} "
                                f"must result in NO_TRADE/insufficient_score, got {dec.action}/{dec.reason}"
                            )
                        if dec.score_gap is not None:
                            raise ValueError("NO_TRADE due to insufficient_score must have decision score_gap=None")
                        if aud.score_gap is not None:
                            raise ValueError("NO_TRADE due to insufficient_score must have audit score_gap=None")
                    else:
                        if dec.action != "SELECT" or dec.reason != "ok":
                            raise ValueError(f"Decisive winner meeting threshold must SELECT/ok, got {dec.action}/{dec.reason}")
                        if dec.score_gap is None or abs(dec.score_gap - expected_gap) > 1e-6:
                            raise ValueError(f"score_gap {dec.score_gap} does not match recomputed gap {expected_gap}")
                        if aud.score_gap is None or abs(aud.score_gap - expected_gap) > 1e-6:
                            raise ValueError(f"audit.score_gap {aud.score_gap} does not match recomputed gap {expected_gap}")
                        self._validate_winning_cluster(expected_winner, dec, aud, eval_by_setup)
            else:
                # Single direction: score_gap must be None
                if dec.score_gap is not None:
                    raise ValueError(f"Single direction must have score_gap=None, got {dec.score_gap}")
                if aud.score_gap is not None:
                    raise ValueError(f"Single direction audit must have score_gap=None, got {aud.score_gap}")

                expected_winner = min(sc_tuple, key=cluster_rank_key)
                if expected_winner.total_score < aud.minimum_total_score:
                    if dec.action != "NO_TRADE" or dec.reason != "insufficient_score":
                        raise ValueError(
                            f"Single direction score {expected_winner.total_score} < minimum {aud.minimum_total_score} "
                            f"must result in NO_TRADE/insufficient_score, got {dec.action}/{dec.reason}"
                        )
                else:
                    if dec.action != "SELECT" or dec.reason != "ok":
                        raise ValueError(f"Single direction winner meeting threshold must SELECT/ok, got {dec.action}/{dec.reason}")
                    self._validate_winning_cluster(expected_winner, dec, aud, eval_by_setup)

    def _validate_winning_cluster(
        self,
        win_sc: ClusterScorecard,
        dec: SelectionDecision,
        aud: SelectionAuditRecord,
        eval_by_setup: Mapping[str, StrategyEvaluation],
    ) -> None:
        if aud.cluster_id != win_sc.cluster_id:
            raise ValueError(f"Winning cluster '{win_sc.cluster_id}' != audit cluster_id '{aud.cluster_id}'")
        if win_sc.primary_strategy_id != dec.primary_strategy_id:
            raise ValueError(f"Winning scorecard primary_strategy_id '{win_sc.primary_strategy_id}' != decision '{dec.primary_strategy_id}'")
        if win_sc.primary_setup_id != dec.selected_setup.setup_id:
            raise ValueError(f"Winning scorecard primary_setup_id '{win_sc.primary_setup_id}' != decision setup '{dec.selected_setup.setup_id}'")
        if win_sc.direction != dec.selected_setup.direction:
            raise ValueError(f"Winning scorecard direction '{win_sc.direction}' != decision setup '{dec.selected_setup.direction}'")
        if win_sc.supporting_strategy_ids != dec.supporting_strategy_ids:
            raise ValueError(f"Winning scorecard supporting_strategy_ids {win_sc.supporting_strategy_ids} != decision {dec.supporting_strategy_ids}")
        if abs(win_sc.total_score - aud.total_score) > 1e-6:
            raise ValueError(f"Winning scorecard total_score {win_sc.total_score} != audit total_score {aud.total_score}")
        ev_total = eval_by_setup[win_sc.primary_setup_id].total_score
        if abs(win_sc.total_score - ev_total) > 1e-6:
            raise ValueError(f"Winning scorecard total_score {win_sc.total_score} != evaluation total_score {ev_total}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "audit_record": self.audit_record.to_dict(),
            "scorecards": [sc.to_dict() for sc in self.scorecards],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SelectorOutput:
        if not isinstance(data, dict):
            raise StrictModelTypeError(f"Expected dict for SelectorOutput, got {type(data).__name__}")
        allowed = {"decision", "audit_record", "scorecards"}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise KeyError(f"Unknown fields in SelectorOutput: {sorted(unknown)}")
        for req in ("decision", "audit_record"):
            if req not in data:
                raise KeyError(f"Missing required field '{req}' in SelectorOutput.")
        return cls(
            decision=SelectionDecision.from_dict(data["decision"]),
            audit_record=SelectionAuditRecord.from_dict(data["audit_record"]),
            scorecards=tuple(
                ClusterScorecard.from_dict(sc) if isinstance(sc, dict) else sc
                for sc in data.get("scorecards", ())
            ),
        )


# =============================================================================
# Component Scorers
# =============================================================================

def compute_regime_score(
    candidate: CandidateSetup,
    regime: MarketRegime,
    eval_item: Optional[StrategyEvaluation] = None,
) -> float:
    """
    Compute regime score from the 30-cell Strategy x Direction x Regime matrix.
    If eval_item is provided and was marked ELIGIBLE at Gate, verifies strict agreement.
    """
    score = get_regime_matrix_score(candidate.strategy_id, candidate.direction, regime.regime)
    if eval_item is not None and eval_item.status == "ELIGIBLE":
        if abs(eval_item.regime_score - score) > 1e-6:
            raise StrategyStateError(
                f"Regime score mismatch for candidate {candidate.setup_id}: "
                f"Gate evaluated {eval_item.regime_score} vs Selector computed {score}"
            )
    return round(score, 2)


def compute_setup_score(
    candidate: CandidateSetup,
    context: StrategyContext,
) -> tuple[float, dict[str, Any]]:
    """
    Compute setup score (0.0 to 100.0) based on structural quality and bonus confluence.

    Returns:
        tuple[setup_score, breakdown_details]
    """
    sid = candidate.strategy_id
    base_score: float
    ob_quality: Optional[str] = None
    displacement: Optional[bool] = None
    fvg_gap: Optional[float] = None
    atr14_val: Optional[float] = context.atr14

    if sid == "S05":
        if candidate.meta.get("flow_type") == "ltf_confirmation":
            conf_ev = next((ev for ev in candidate.evidences if ev.kind == "structure_event"), None)
            if conf_ev is not None and conf_ev.details.get("displacement") is True:
                base_score = 85.0
                displacement = True
            else:
                displacement = False
                base_score = 70.0
            ob_ev = next((ev for ev in candidate.evidences if ev.kind == "order_block"), None)
            if ob_ev is not None:
                ob_quality = ob_ev.details.get("quality")
        else:
            ob_ev = next((ev for ev in candidate.evidences if ev.kind == "order_block"), None)
            if ob_ev is None:
                raise StrategyStateError(f"Candidate {candidate.setup_id} (S05) missing order_block evidence.")
            ob_quality = ob_ev.details.get("quality")
            if ob_quality == "premium_candidate":
                base_score = 90.0
            elif ob_quality == "strong":
                base_score = 75.0
            elif ob_quality == "base":
                base_score = 60.0
            else:
                raise StrategyStateError(
                    f"Candidate {candidate.setup_id} (S05) missing valid OB quality: '{ob_quality}'"
                )
    elif sid in {"S01", "S09"}:
        mss_ev = next((ev for ev in candidate.evidences if ev.kind == "structure_event"), None)
        if mss_ev is not None and mss_ev.details.get("displacement") is True:
            base_score = 85.0
            displacement = True
        else:
            displacement = False
            fvg_ev = next((ev for ev in candidate.evidences if ev.kind == "fair_value_gap"), None)
            if fvg_ev is not None and "top" in fvg_ev.details and "bottom" in fvg_ev.details:
                fvg_gap = round(abs(float(fvg_ev.details["top"]) - float(fvg_ev.details["bottom"])), 5)
            else:
                fvg_gap = 0.0

            if atr14_val is not None and atr14_val > 0 and fvg_gap >= atr14_val:
                base_score = 80.0
            else:
                base_score = 65.0
    else:
        base_score = 50.0

    # Bonuses (+10 each, applied once per candidate, capped at 100.0)
    bonuses: list[str] = []

    # Bonus 1: Clean sweep
    has_clean_sweep = any(
        ev.kind == "liquidity_sweep" and ev.details.get("sweep_type") == "clean"
        for ev in candidate.evidences
    )
    if has_clean_sweep:
        bonuses.append("clean_sweep")

    # Bonus 2: Equal highs / equal lows
    has_eq_pool = any(
        ev.kind in {"liquidity_sweep", "liquidity_pool"}
        and (
            ev.details.get("pool_kind") in {"equal_highs", "equal_lows"}
            or ev.details.get("kind") in {"equal_highs", "equal_lows"}
        )
        for ev in candidate.evidences
    )
    if has_eq_pool:
        bonuses.append("equal_highs_lows")

    # Bonus 3: Direct zone confluence (fair_value_gap + order_block)
    has_fvg = any(ev.kind == "fair_value_gap" for ev in candidate.evidences)
    has_ob = any(ev.kind == "order_block" for ev in candidate.evidences)
    if has_fvg and has_ob:
        bonuses.append("fvg_ob_confluence")

    bonus_points = len(bonuses) * 10.0
    setup_score = min(100.0, base_score + bonus_points)
    setup_score = round(setup_score, 2)

    details: dict[str, Any] = {
        "base_score": base_score,
        "bonuses": list(bonuses),
        "bonus_points": bonus_points,
        "fvg_atr_basis": "signal_bar_atr14",
    }
    if ob_quality is not None:
        details["ob_quality"] = ob_quality
    if displacement is not None:
        details["displacement"] = displacement
    if fvg_gap is not None:
        details["fvg_gap"] = fvg_gap
    if atr14_val is not None:
        details["atr14"] = atr14_val

    return setup_score, details


def compute_context_score(
    candidate: CandidateSetup,
    context: StrategyContext,
) -> tuple[float, dict[str, Any]]:
    """
    Compute context score (0.0 to 100.0) from HTF bias and session alignment.

    Returns:
        tuple[context_score, breakdown_details]
    """
    if context.htf_bias is None or context.htf_bias.bias is None:
        raise StrategyStateError(f"Candidate {candidate.setup_id} context missing HTF bias.")

    bias_val = context.htf_bias.bias
    direction = candidate.direction

    is_aligned = (direction == "BUY" and bias_val == "bullish") or (direction == "SELL" and bias_val == "bearish")
    is_neutral = (bias_val == "neutral")
    is_opposed = (direction == "BUY" and bias_val == "bearish") or (direction == "SELL" and bias_val == "bullish")

    if is_aligned:
        bias_score = 60.0
    elif is_neutral:
        if candidate.strategy_id == "S05":
            raise StrategyStateError(f"S05 candidate {candidate.setup_id} cannot have neutral HTF bias.")
        bias_score = 30.0
    elif is_opposed:
        raise StrategyStateError(f"Candidate {candidate.setup_id} direction {direction} opposes HTF bias {bias_val}.")
    else:
        raise StrategyStateError(f"Unrecognized HTF bias value: '{bias_val}'")

    sid = candidate.strategy_id
    if sid == "S09":
        session_score = 40.0
    elif sid in {"S01", "S05"}:
        if context.session_decision is not None and context.session_decision.in_session is True:
            session_score = 40.0
        else:
            session_score = 10.0
    else:
        session_score = 25.0

    context_score = min(100.0, bias_score + session_score)
    context_score = round(context_score, 2)

    details = {
        "bias_score": bias_score,
        "session_score": session_score,
        "htf_bias": bias_val,
        "session_active": (
            context.session_decision.in_session
            if context.session_decision is not None
            else None
        ),
    }
    return context_score, details


def compute_execution_score(
    candidate: CandidateSetup,
) -> tuple[float, dict[str, Any]]:
    """
    Compute execution score proxy (0.0 to 100.0) from CandidateSetup.planned_rr at closed bar N.
    Uses piecewise linear interpolation:
      - planned_rr < 1.5: Error (rejected at gate)
      - 1.5 <= planned_rr < 2.0: 30.0 + ((RR - 1.5) / 0.5) * 30.0
      - 2.0 <= planned_rr < 3.0: 60.0 + ((RR - 2.0) / 1.0) * 40.0
      - planned_rr >= 3.0: 100.0

    Returns:
        tuple[exec_score, breakdown_details]
    """
    rr = candidate.planned_rr
    if rr < 1.5:
        raise StrategyStateError(f"Candidate {candidate.setup_id} planned_rr {rr} < 1.5.")

    if rr < 2.0:
        exec_score = 30.0 + ((rr - 1.5) / 0.5) * 30.0
    elif rr < 3.0:
        exec_score = 60.0 + ((rr - 2.0) / 1.0) * 40.0
    else:
        exec_score = 100.0

    exec_score = max(0.0, min(100.0, exec_score))
    exec_score = round(exec_score, 2)

    details = {
        "planned_rr": rr,
        "execution_score_basis": "planned_rr_pre_fill",
    }
    return exec_score, details


def compute_total_score(
    regime_score: float,
    setup_score: float,
    context_score: float,
    exec_score: float,
    config: SelectorConfig,
) -> float:
    """
    Compute final composite score from the 4 components using configured weights.
    Total = round(wr * regime + ws * setup + wc * context + we * exec, 2).
    """
    raw = (
        config.weight_regime * regime_score
        + config.weight_setup * setup_score
        + config.weight_context * context_score
        + config.weight_exec * exec_score
    )
    score = round(raw, 2)
    return max(0.0, min(100.0, score))


# =============================================================================
# Deterministic Tie-Breaking Keys
# =============================================================================

def member_rank_key(eval_item: StrategyEvaluation) -> tuple:
    """
    Deterministic canonical ranking key for candidate evaluations within a cluster.
    Lower tuple value indicates higher rank (sorted ascending).
    """
    c = eval_item.candidate
    return (
        -round(float(eval_item.total_score), 2),
        -round(float(eval_item.setup_score), 2),
        -round(float(eval_item.context_score), 2),
        -round(float(eval_item.exec_score), 2),
        -round(float(eval_item.regime_score), 2),
        -round(float(c.planned_rr), 4),
        str(c.strategy_id),
        str(c.setup_id),
    )


def cluster_rank_key(scorecard: ClusterScorecard) -> tuple:
    """
    Deterministic canonical ranking key for evidence clusters.
    Lower tuple value indicates higher rank (sorted ascending).
    """
    return (
        -round(float(scorecard.total_score), 2),
        -round(float(scorecard.setup_score), 2),
        -round(float(scorecard.context_score), 2),
        -round(float(scorecard.exec_score), 2),
        -round(float(scorecard.regime_score), 2),
        -round(float(scorecard.planned_rr), 4),
        str(scorecard.primary_strategy_id),
        str(scorecard.primary_setup_id),
        str(scorecard.cluster_id),
    )


# =============================================================================
# Deterministic Strategy Selector
# =============================================================================

class DeterministicStrategySelector:
    """
    Deterministic selector evaluating eligible clusters and candidates, resolving direction
    conflicts, assigning cluster ownership, and producing decisions and audit telemetry.
    """

    def __init__(self, config: Optional[SelectorConfig] = None):
        self._config = config or SelectorConfig()

    @property
    def config(self) -> SelectorConfig:
        return self._config

    def select(self, batch: ConfluenceBatch, context: StrategyContext) -> SelectorOutput:
        return select_strategy(batch, context, self._config)


def select_strategy(
    batch: ConfluenceBatch,
    context: StrategyContext,
    config: Optional[SelectorConfig] = None,
) -> SelectorOutput:
    """
    Select the single winning candidate setup or decide NO_TRADE for bar N.

    Args:
        batch: ConfluenceBatch holding market regime, evaluations, and eligible clusters.
        context: StrategyContext at closed bar N.
        config: SelectorConfig with weights and thresholds (default SelectorConfig()).

    Returns:
        SelectorOutput containing SelectionDecision, SelectionAuditRecord, and tuple of ClusterScorecards.
    """
    if not isinstance(batch, ConfluenceBatch):
        raise StrictModelTypeError(f"batch must be ConfluenceBatch, got {type(batch).__name__}")
    if not isinstance(context, StrategyContext):
        raise StrictModelTypeError(f"context must be StrategyContext, got {type(context).__name__}")

    if batch.bar_index != context.bar_index:
        raise ValueError(
            f"batch.bar_index ({batch.bar_index}) does not match context.bar_index ({context.bar_index})"
        )
    if batch.timestamp != context.bar_close_time:
        raise ValueError(
            f"batch.timestamp ({batch.timestamp}) does not match context.bar_close_time ({context.bar_close_time})"
        )
    if batch.regime is not None:
        if batch.regime.bar_index != context.bar_index:
            raise ValueError(
                f"batch.regime.bar_index ({batch.regime.bar_index}) does not match context.bar_index ({context.bar_index})"
            )
        if batch.regime.timestamp != context.bar_close_time:
            raise ValueError(
                f"batch.regime.timestamp ({batch.regime.timestamp}) does not match context.bar_close_time ({context.bar_close_time})"
            )

    cfg = config or SelectorConfig()
    N = batch.bar_index

    # Handle case with no eligible clusters
    if not batch.eligible_clusters:
        decision_id = make_decision_id(N, "NO_TRADE", None)
        meta = {
            "selector_version": cfg.version,
            "minimum_total_score": cfg.minimum_total_score,
            "minimum_direction_gap": cfg.direction_conflict_gap,
            "scoring_weights": list(cfg.scoring_weights),
            "execution_score_basis": "planned_rr_pre_fill",
            "fvg_atr_basis": "signal_bar_atr14",
            "symbol": context.symbol,
            "timeframe": context.timeframe,
            "winner_cluster_id": None,
        }
        decision = SelectionDecision(
            decision_id=decision_id,
            bar_index=N,
            timestamp=batch.timestamp,
            action="NO_TRADE",
            selected_setup=None,
            primary_strategy_id=None,
            supporting_strategy_ids=(),
            regime=batch.regime,
            evaluations=batch.evaluations,
            reason="no_eligible_setup",
            score_gap=None,
            execution_payload={},
            meta=meta,
        )
        gate_counts = Counter(
            r for ev in batch.evaluations for r in ev.rejection_reasons
        )
        audit_record = SelectionAuditRecord(
            record_version="1.0.0",
            selector_version=cfg.version,
            minimum_total_score=cfg.minimum_total_score,
            minimum_direction_gap=cfg.direction_conflict_gap,
            scoring_weights=cfg.scoring_weights,
            symbol=context.symbol,
            timeframe=context.timeframe,
            bar_index=N,
            timestamp=batch.timestamp,
            decision_id=decision_id,
            action="NO_TRADE",
            reason="no_eligible_setup",
            primary_strategy_id=None,
            primary_setup_id=None,
            direction=None,
            total_score=None,
            score_gap=None,
            direction_conflict_present=False,
            best_buy_score=None,
            best_sell_score=None,
            cluster_id=None,
            supporting_strategy_ids=(),
            cluster_scorecards=(),
            gate_reason_counts=dict(gate_counts),
            evaluated_count=len(batch.evaluations),
            eligible_count=0,
            rejected_count=len(batch.evaluations),
            regime=batch.regime.regime if batch.regime else None,
            meta=meta,
        )
        return SelectorOutput(
            decision=decision,
            audit_record=audit_record,
            scorecards=(),
        )

    # Score each cluster and its member candidates
    cluster_scorecards: list[ClusterScorecard] = []
    primary_eval_by_cluster: dict[str, StrategyEvaluation] = {}
    scored_evals_by_setup_id: dict[str, StrategyEvaluation] = {}

    for cluster in batch.eligible_clusters:
        scored_members: list[StrategyEvaluation] = []
        for eval_item in cluster.members:
            cand = eval_item.candidate
            r_score = compute_regime_score(cand, batch.regime, eval_item)
            s_score, s_details = compute_setup_score(cand, context)
            c_score, c_details = compute_context_score(cand, context)
            e_score, e_details = compute_execution_score(cand)
            t_score = compute_total_score(r_score, s_score, c_score, e_score, cfg)

            merged_details = dict(eval_item.details)
            merged_details.update({
                "fvg_atr_basis": s_details.get("fvg_atr_basis", "signal_bar_atr14"),
                "execution_score_basis": e_details.get("execution_score_basis", "planned_rr_pre_fill"),
                "setup_score_breakdown": s_details,
                "context_score_breakdown": c_details,
                "exec_score_breakdown": e_details,
            })

            scored_eval = StrategyEvaluation(
                candidate=cand,
                status=eval_item.status,
                rejection_reasons=eval_item.rejection_reasons,
                regime_score=r_score,
                setup_score=s_score,
                context_score=c_score,
                exec_score=e_score,
                total_score=t_score,
                details=merged_details,
            )
            scored_members.append(scored_eval)
            scored_evals_by_setup_id[cand.setup_id] = scored_eval

        # Primary member has minimum member_rank_key
        primary_member = min(scored_members, key=member_rank_key)
        primary_eval_by_cluster[cluster.cluster_id] = primary_member

        supporting_strats = tuple(sorted(set(
            m.candidate.strategy_id
            for m in scored_members
            if m.candidate.strategy_id != primary_member.candidate.strategy_id
        )))

        m_setup_ids = tuple(sorted(set(m.candidate.setup_id for m in scored_members)))
        m_scores = {m.candidate.setup_id: m.total_score for m in scored_members}

        scorecard = ClusterScorecard(
            cluster_id=cluster.cluster_id,
            direction=cluster.direction,
            primary_strategy_id=primary_member.candidate.strategy_id,
            primary_setup_id=primary_member.candidate.setup_id,
            supporting_strategy_ids=supporting_strats,
            total_score=primary_member.total_score,
            regime_score=primary_member.regime_score,
            setup_score=primary_member.setup_score,
            context_score=primary_member.context_score,
            exec_score=primary_member.exec_score,
            planned_rr=primary_member.candidate.planned_rr,
            member_count=len(scored_members),
            scoring_weights=cfg.scoring_weights,
            member_setup_ids=m_setup_ids,
            member_scores=m_scores,
            details={
                "member_setup_ids": list(m_setup_ids),
                "member_scores": dict(m_scores),
                "primary_details": dict(primary_member.details),
                "fvg_atr_basis": "signal_bar_atr14",
                "execution_score_basis": "planned_rr_pre_fill",
            },
        )
        cluster_scorecards.append(scorecard)

    # Sort scorecards canonically by cluster_rank_key
    cluster_scorecards.sort(key=cluster_rank_key)

    # Direction Evaluation & Conflict Handling
    buy_scorecards = [sc for sc in cluster_scorecards if sc.direction == "BUY"]
    sell_scorecards = [sc for sc in cluster_scorecards if sc.direction == "SELL"]

    action: DecisionAction
    reason: str
    selected_setup: Optional[CandidateSetup] = None
    primary_strategy_id: Optional[str] = None
    supporting_strategy_ids: tuple[str, ...] = ()
    score_gap: Optional[float] = None
    winner_scorecard: Optional[ClusterScorecard] = None

    if buy_scorecards and sell_scorecards:
        # Both BUY and SELL eligible clusters exist: compare best BUY vs best SELL
        best_buy = min(buy_scorecards, key=cluster_rank_key)
        best_sell = min(sell_scorecards, key=cluster_rank_key)
        gap = round(abs(best_buy.total_score - best_sell.total_score), 2)
        score_gap = gap

        if gap < cfg.direction_conflict_gap:
            # Score gap is strictly less than 15.0 -> NO_TRADE due to conflicting direction
            action = "NO_TRADE"
            reason = "conflicting_direction"
            selected_setup = None
            primary_strategy_id = None
            supporting_strategy_ids = ()
            winner_scorecard = None
        else:
            # Score gap >= 15.0: higher scoring direction wins (exact 15.0 is SELECT)
            if best_buy.total_score > best_sell.total_score:
                winner_scorecard = best_buy
            elif best_sell.total_score > best_buy.total_score:
                winner_scorecard = best_sell
            else:
                # Deterministic tie-break fallback
                winner_scorecard = min([best_buy, best_sell], key=cluster_rank_key)

            # Check winner against minimum_total_score
            if winner_scorecard.total_score < cfg.minimum_total_score:
                action = "NO_TRADE"
                reason = "insufficient_score"
                selected_setup = None
                primary_strategy_id = None
                supporting_strategy_ids = ()
                score_gap = None
                winner_scorecard = None
            else:
                action = "SELECT"
                reason = "ok"
                primary_eval = primary_eval_by_cluster[winner_scorecard.cluster_id]
                selected_setup = primary_eval.candidate
                primary_strategy_id = winner_scorecard.primary_strategy_id
                supporting_strategy_ids = winner_scorecard.supporting_strategy_ids
    else:
        # Only one direction exists
        score_gap = None
        winner_scorecard = min(cluster_scorecards, key=cluster_rank_key)

        if winner_scorecard.total_score < cfg.minimum_total_score:
            action = "NO_TRADE"
            reason = "insufficient_score"
            selected_setup = None
            primary_strategy_id = None
            supporting_strategy_ids = ()
            score_gap = None
            winner_scorecard = None
        else:
            action = "SELECT"
            reason = "ok"
            primary_eval = primary_eval_by_cluster[winner_scorecard.cluster_id]
            selected_setup = primary_eval.candidate
            primary_strategy_id = winner_scorecard.primary_strategy_id
            supporting_strategy_ids = winner_scorecard.supporting_strategy_ids

    # Update all evaluations from batch with scored versions
    final_evaluations = tuple(
        scored_evals_by_setup_id.get(ev.candidate.setup_id, ev)
        for ev in batch.evaluations
    )

    common_meta = {
        "selector_version": cfg.version,
        "minimum_total_score": cfg.minimum_total_score,
        "minimum_direction_gap": cfg.direction_conflict_gap,
        "scoring_weights": list(cfg.scoring_weights),
        "execution_score_basis": "planned_rr_pre_fill",
        "fvg_atr_basis": "signal_bar_atr14",
        "symbol": context.symbol,
        "timeframe": context.timeframe,
        "winner_cluster_id": winner_scorecard.cluster_id if (action == "SELECT" and winner_scorecard) else None,
    }

    decision_id = make_decision_id(N, action, primary_strategy_id)
    decision = SelectionDecision(
        decision_id=decision_id,
        bar_index=N,
        timestamp=batch.timestamp,
        action=action,
        selected_setup=selected_setup,
        primary_strategy_id=primary_strategy_id,
        supporting_strategy_ids=supporting_strategy_ids,
        regime=batch.regime,
        evaluations=final_evaluations,
        reason=reason,
        score_gap=score_gap,
        execution_payload={},  # Strictly empty dict in T53.8; cash-RR, fill, cooldown in T53.9
        meta=common_meta,
    )

    gate_counts = Counter(
        r for ev in final_evaluations for r in ev.rejection_reasons
    )
    buy_cards = [sc for sc in cluster_scorecards if sc.direction == "BUY"]
    sell_cards = [sc for sc in cluster_scorecards if sc.direction == "SELL"]
    direction_conflict_present = bool(buy_cards and sell_cards)
    best_buy = min(buy_cards, key=cluster_rank_key) if buy_cards else None
    best_sell = min(sell_cards, key=cluster_rank_key) if sell_cards else None
    best_buy_score = round(best_buy.total_score, 2) if best_buy else None
    best_sell_score = round(best_sell.total_score, 2) if best_sell else None

    audit_record = SelectionAuditRecord(
        record_version="1.0.0",
        selector_version=cfg.version,
        minimum_total_score=cfg.minimum_total_score,
        minimum_direction_gap=cfg.direction_conflict_gap,
        scoring_weights=cfg.scoring_weights,
        symbol=context.symbol,
        timeframe=context.timeframe,
        bar_index=N,
        timestamp=batch.timestamp,
        decision_id=decision_id,
        action=action,
        reason=reason,
        primary_strategy_id=primary_strategy_id,
        primary_setup_id=selected_setup.setup_id if selected_setup else None,
        direction=selected_setup.direction if selected_setup else None,
        total_score=winner_scorecard.total_score if (action == "SELECT" and winner_scorecard) else None,
        score_gap=score_gap,
        direction_conflict_present=direction_conflict_present,
        best_buy_score=best_buy_score,
        best_sell_score=best_sell_score,
        cluster_id=winner_scorecard.cluster_id if (action == "SELECT" and winner_scorecard) else None,
        supporting_strategy_ids=supporting_strategy_ids,
        cluster_scorecards=tuple(cluster_scorecards),
        gate_reason_counts=dict(gate_counts),
        evaluated_count=len(final_evaluations),
        eligible_count=sum(1 for ev in final_evaluations if ev.status == "ELIGIBLE"),
        rejected_count=sum(1 for ev in final_evaluations if ev.status == "REJECTED"),
        regime=batch.regime.regime if batch.regime else None,
        meta=common_meta,
    )

    return SelectorOutput(
        decision=decision,
        audit_record=audit_record,
        scorecards=tuple(cluster_scorecards),
    )


__all__ = [
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
    "member_rank_key",
    "cluster_rank_key",
]
