# KẾ HOẠCH TRIỂN KHAI MULTI-STRATEGY SMC CONFLUENCE & SELECTION ENGINE

> Phiên bản: 1.0 — 2026-09-09  
> Phạm vi: Giai đoạn T53, sau khi Swing, Structure, OB, FVG, Liquidity và Context đã hoàn tất.  
> Nguồn nghiệp vụ: `SMC_STRATEGY_CATALOG_V1.md`.  
> Trạng thái: Plan chờ thảo luận và khóa semantics trước khi implementation.

---

## 1. Mục tiêu

Xây một engine dùng chung có thể:

1. Chạy nhiều strategy template SMC trên cùng snapshot dữ liệu.
2. Loại các strategy không đủ điều kiện tại market context hiện tại.
3. Gom các setup trùng evidence để không mở nhiều lệnh cho cùng một cơ hội.
4. Chấm điểm và chọn setup tốt nhất, hoặc trả `NO_TRADE`.
5. Giữ toàn bộ quyết định deterministic, audit được và không lookahead.
6. Cho phép bổ sung LLM supervisor về sau mà không giao quyền trực tiếp cho execution.

Đích đầu tiên không phải chứng minh lợi nhuận. Đích đầu tiên là chứng minh correctness, determinism, parity và khả năng so sánh các strategy công bằng.

---

## 2. Phạm vi giai đoạn này

### Làm trong T53

- Domain models cho strategy/confluence/selection.
- Contract snapshot bất biến tại mỗi closed bar.
- Strategy template interface và registry.
- Ba strategy Wave 1:
  - S01 ICT 2022 Reversal.
  - S05 BOS → Order Block Retest.
  - S09 ICT Silver Bullet.
- Rule-based Market Regime V1.
- Eligibility gate.
- Evidence deduplication và conflict resolver.
- Deterministic selector.
- Batch/incremental parity.
- Adapter chạy multi-strategy trong backtest hiện tại.
- Telemetry, reason codes và audit output.
- Test, benchmark và QC.

### Chưa làm trong T53 Wave 1

- LLM tự chọn hoặc tự đặt lệnh.
- Contextual bandit, reinforcement learning hoặc online learning.
- Tối ưu weights tự động.
- Breaker Block, IFVG, Mitigation Block, SMT divergence.
- Multi-symbol execution.
- Tích hợp broker/live execution.
- Thay đổi toàn diện BacktestEngine cũ.
- Triển khai cả 10 strategy trong một lượt.

---

## 3. Nguyên tắc kiến trúc

```text
OHLCV / Replay closed bar
          ↓
Shared SMC Detectors
          ↓
Immutable StrategyContext
          ↓
Strategy Template Registry
  ├─ S01 ICT 2022 Reversal
  ├─ S05 BOS/OB Continuation
  └─ S09 Silver Bullet
          ↓
CandidateSetup[]
          ↓
Eligibility Gate
          ↓
Evidence Deduplicator / Conflict Resolver
          ↓
Deterministic Strategy Selector
          ↓
SelectionDecision | NO_TRADE
          ↓
Risk/Execution Adapter
          ↓
BacktestEngine (fill từ bar N+1)
```

Ranh giới bắt buộc:

- Detector không biết strategy nào sẽ được chọn.
- Strategy template không tự đặt lệnh.
- Selector không sửa detector state hoặc candidate input.
- Risk gate có quyền reject quyết định của selector.
- Execution không diễn giải text/reasoning; chỉ nhận model đã validate.
- Đường code SMC cũ được giữ làm baseline cho tới khi adapter mới đạt parity theo contract đã chốt.

---

## 4. Domain models dự kiến

### 4.1. StrategyContext

Snapshot read-only chứa mọi dữ liệu đã biết tại một closed bar:

```python
@dataclass(frozen=True)
class StrategyContext:
    bar_index: int
    timestamp: pd.Timestamp
    candle: Mapping[str, Any]
    htf_bias: BiasState
    session: SessionDecision
    structure_events: tuple[StructureEvent, ...]
    order_blocks: tuple[OrderBlock, ...]
    fvgs: tuple[FairValueGap, ...]
    liquidity_pools: tuple[LiquidityPool, ...]
    liquidity_sweeps: tuple[LiquiditySweep, ...]
```

Contract:

- Không chứa object chưa `confirmed_at <= bar_index/as_of`.
- Không chứa final batch state thay cho state tại bar hiện tại.
- Không mutate source object hoặc mapping.

### 4.2. StrategyProfile

```python
@dataclass(frozen=True)
class StrategyProfile:
    strategy_id: str
    family: Literal["reversal", "continuation", "time_based"]
    allowed_regimes: frozenset[str]
    required_evidence: frozenset[str]
    optional_evidence: frozenset[str]
    forbidden_conditions: frozenset[str]
    minimum_rr: float
    cooldown_bars: int
```

### 4.3. CandidateSetup

```python
@dataclass(frozen=True)
class CandidateSetup:
    strategy_id: str
    setup_id: str
    direction: Literal["long", "short"]
    signal_bar: int
    available_at: int
    entry_zone_low: float
    entry_zone_high: float
    stop_anchor: float
    target_candidates: tuple[float, ...]
    expires_at: int
    evidence: tuple[EvidenceRef, ...]
    reason_codes: tuple[str, ...]
```

### 4.4. MarketRegime

Regime V1 có thể gồm:

```text
bullish_trend
bearish_trend
range
reversal_forming
session_expansion
high_volatility
low_volatility
uncertain
```

Model phải có confidence, source features, `as_of` và reason codes. Regime phải được tính từ dữ liệu đến hiện tại, không được gán nhãn quá khứ bằng dữ liệu tương lai.

### 4.5. StrategyEvaluation

Ghi lại:

- Eligible/rejected.
- Lý do loại.
- Regime fit.
- Setup quality.
- Execution quality.
- Final score.
- Missing requirements.
- Duplicate/cluster identity.

### 4.6. SelectionDecision

```python
@dataclass(frozen=True)
class SelectionDecision:
    bar_index: int
    selected_setup: CandidateSetup | None
    evaluations: tuple[StrategyEvaluation, ...]
    regime: MarketRegime
    decision: Literal["selected", "no_trade"]
    reason_codes: tuple[str, ...]
```

---

## 5. Evidence identity và event ordering

Mỗi evidence phải có identity ổn định, không phụ thuộc object memory address:

```text
swing:<mode>:<kind>:<index>
structure:<mode>:<direction>:<index>
ob:<source_event_index>:<ob_index>
fvg:<mode>:<direction>:<index>:<confirmed_at>
pool:<mode>:<kind>:<created_at>:<source_indices_hash>
sweep:<pool_id>:<swept_at>:<direction>
session:<name>:<trading_date>
```

Event ordering validator cần kiểm tra từng template, ví dụ S01:

```text
sweep.swept_at
  <= structure.index
  <= fvg.confirmed_at
  <= setup.available_at
  <= setup.signal_bar
```

Equality chỉ được chấp nhận khi semantics của hai event cho phép cùng closed bar. Mọi boundary phải có test riêng.

---

## 6. Strategy Template interface

```python
class StrategyTemplate(Protocol):
    strategy_id: str
    profile: StrategyProfile

    def evaluate(self, context: StrategyContext) -> tuple[CandidateSetup, ...]: ...
```

Yêu cầu:

- Pure/deterministic với cùng context và state snapshot.
- Không gọi ngược data loader hoặc execution.
- Không mutate input.
- Không tự cộng rolling performance.
- Không tự giải quyết conflict với strategy khác.
- Trả candidate cùng reason/source metadata đầy đủ.

Registry phải reject duplicate `strategy_id` và hỗ trợ bật/tắt strategy bằng config.

---

## 7. Wave 1 — Ba strategy đại diện

### 7.1. S01 ICT 2022 Reversal

State machine:

```text
IDLE
→ SWEEP_SEEN
→ MSS_CONFIRMED
→ FVG_READY
→ ENTRY_PENDING
→ FILLED | EXPIRED | INVALIDATED
```

Điểm phải chốt trước code:

- Liquidity source nào hợp lệ.
- Sweep → MSS max bars.
- MSS → FVG max bars.
- FVG entry level.
- SL dùng sweep extreme hay zone.
- Target liquidity và minimum RR.

### 7.2. S05 BOS → OB Retest

State machine:

```text
IDLE
→ BIAS_ALIGNED
→ BOS_CONFIRMED
→ OB_READY
→ RETEST_PENDING
→ FILLED | EXPIRED | INVALIDATED
```

Điểm phải chốt:

- BOS cần displacement bắt buộc hay optional.
- OB quality tối thiểu.
- Retest/mitigation/retest-count limits.
- Opposite CHoCH hủy setup như thế nào.
- Target external liquidity hay RR cố định.

### 7.3. S09 ICT Silver Bullet

State machine:

```text
OUTSIDE_WINDOW
→ WINDOW_OPEN
→ SWEEP_SEEN
→ MSS_CONFIRMED
→ FVG_READY
→ ENTRY_PENDING
→ FILLED | WINDOW_EXPIRED | INVALIDATED
```

Điểm phải chốt:

- Danh sách time windows và timezone canonical.
- Event nào bắt buộc xảy ra trong window.
- Có grace period cho entry hay không.
- Số setup tối đa mỗi window.
- Daily/HTF bias là bắt buộc hay filter.

---

## 8. Market Regime V1

Regime V1 dùng rule-based classifier để làm baseline audit được.

Feature đầu vào dự kiến:

- HTF bias.
- Số BOS cùng hướng trong rolling window.
- Số CHoCH/opposite break.
- Swing expansion/contraction.
- ATR hiện tại so với rolling percentile chỉ dùng quá khứ.
- Efficiency ratio hoặc directional body/range ratio.
- Liquidity sweep gần nhất.
- Session state.

Nguyên tắc:

- Regime không bắt buộc phải chọn một nhãn chắc chắn; có `uncertain`.
- Không dùng centered rolling window.
- Không dùng full-sample percentile/scaler.
- Không dùng smoothed state được tính bằng dữ liệu tương lai.
- Có hysteresis/minimum confirmation để tránh flip mỗi bar.
- Wave 1 dùng regime làm eligibility nhẹ hoặc điểm số; chưa tự động tối ưu rule.

---

## 9. Eligibility Gate

Gate chạy trước scoring và trả reason code cho từng rejection.

Reason codes tối thiểu:

```text
wrong_regime
uncertain_regime
htf_bias_mismatch
missing_required_evidence
invalid_event_order
outside_session
expired_setup
invalid_order_block
invalid_fvg
stale_liquidity_sweep
opposite_structure_shift
insufficient_rr
spread_too_high
cooldown_active
duplicate_setup
conflicting_direction
```

Gate không được “cứu” setup bằng optional score nếu thiếu điều kiện bắt buộc.

---

## 10. Deduplication và conflict resolution

Setup cluster key sơ bộ:

```text
(direction, liquidity_sweep_id, structure_event_id, entry_zone_id, signal_window)
```

Quy tắc:

- Nhiều template dùng cùng sweep/MSS/FVG là một cơ hội, không phải nhiều phiếu độc lập.
- Evidence chỉ được tính một lần theo evidence ID.
- Hai candidate cùng cluster được merge metadata và supporting strategy IDs.
- Bullish và bearish candidate cùng bar không tự triệt tiêu im lặng; phải trả conflict reason.
- Wave 1 mặc định reject khi hai direction có score gần nhau dưới `minimum_score_gap`.
- Không mở nhiều lệnh nếu BacktestEngine chỉ hỗ trợ một position.

---

## 11. Deterministic Selector V1

Selector chỉ xét candidate đã qua gate.

Score V1 nên tách thành thành phần, chưa khóa weight cuối cùng:

```text
regime_fit
setup_quality
context_quality
execution_quality
stability_placeholder
```

Quy tắc chọn:

1. Không candidate eligible → `NO_TRADE`.
2. Một candidate → chọn nếu đạt minimum score/RR.
3. Nhiều candidate cùng cluster → merge rồi chọn một.
4. Nhiều cluster cùng direction → chọn score cao nhất theo tie-break deterministic.
5. Conflict hai direction → yêu cầu score gap; nếu không đủ → `NO_TRADE`.
6. Tie-break không được phụ thuộc iteration/hash order.

Rolling performance và strategy fitness chưa được dùng để chọn live trong Wave 1. Chúng chỉ được ghi telemetry cho giai đoạn sau.

---

## 12. Backtest adapter

BacktestEngine hiện nhận một `strategy_id` và signal series. Adapter mới phải giữ tương thích:

```text
MultiStrategyEngine
→ SelectionDecision tại bar N
→ signal/candidate adapter
→ BacktestEngine chỉ fill từ bar N+1
```

Giai đoạn đầu:

- Không thay logic fill/SL/TP cũ nếu chưa cần.
- Lưu `strategy_id`, `setup_id`, regime, evidence và reason codes vào trade metadata.
- Cho phép chạy:
  - Từng strategy riêng.
  - Cả ba strategy với selector.
  - Baseline SMC cũ để đối chiếu.
- Không so sánh chiến lược nếu transaction-cost assumptions khác nhau.

---

## 13. Telemetry và audit

Mỗi bar cần thống kê:

- Candidate tạo bởi từng strategy.
- Candidate bị loại và reason.
- Cluster/duplicate count.
- Direction conflicts.
- Setup selected.
- No-trade reasons.
- Order pending/filled/expired/cancelled.
- Performance theo strategy/regime/session sau khi trade đóng.

Audit record phải đủ để replay lại quyết định mà không gọi LLM.

---

## 14. Task breakdown

### T53.0 — Khóa semantics Wave 1

- Thảo luận và ghi ADR cho các câu hỏi mở của S01, S05, S09.
- Chốt entry, SL, target, expiry, cooldown và same-bar ordering.
- Chốt cách dùng regime: gate hay score.
- Không code engine trước khi ADR hoàn tất.

### T53.1 — Domain models và serialization

- Tạo models trong phạm vi `smc/engine`.
- Validation, immutability, stable IDs, `to_dict()`.
- Unit tests đầy đủ cho model và invalid input.

### T53.2 — StrategyContext builder

- Gom output các detector theo as-of bar.
- Lọc future/unconfirmed/invalid state đúng thời điểm.
- Batch/incremental snapshot parity.
- Performance hiện tại 11–12s/10.000 bars được chấp nhận cho phạm vi closed-bar real-time với ít mã theo ADR 19. Benchmark `< 7.0s` được chuyển thành test opt-in/technical debt; mục tiêu dài hạn `< 1.5s` vẫn được lưu và không chặn T53.3.

### T53.3 — Template protocol và registry

- Protocol/base class.
- Registry deterministic.
- Config bật/tắt strategy.
- Reject duplicate IDs và invalid config.

### T53.4 — Implement S01 ICT 2022 Reversal

- State machine, event ordering, expiry và candidate output.
- Long/short symmetry, replay cutoff, duplicate protection.

### T53.5 — Implement S05 BOS → OB Retest

- Bias/BOS/OB relation, lifecycle validation và continuation invalidation.
- Long/short symmetry và same-leg constraints.

### T53.6 — Implement S09 Silver Bullet

- Time-window state machine, timezone/DST, sweep/MSS/FVG ordering.
- Window/grace expiry và one/multiple setup policy theo ADR.

### T53.7 — Regime, eligibility, dedup và conflict

- Rule-based regime V1.
- Eligibility reason codes.
- Evidence cluster và direction conflict policy.

### T53.8 — Selector và telemetry

- Deterministic component scoring.
- Stable tie-break, score gap và no-trade policy.
- Audit records/funnel stats.

### T53.9 — Backtest integration và QC

- Multi-strategy adapter.
- Single-strategy vs selector runs.
- Full tests, benchmarks, replay/no-lookahead probes.
- Independent QC trước khi Wave 2.

---

## 15. Test plan

### Contract tests

- Model validation và serialization.
- Stable ID determinism.
- Deep input immutability.
- Context không chứa future event.

### Strategy tests

- Happy path Long/Short.
- Thiếu từng required event.
- Sai event order.
- Boundary của mỗi timeout/expiry.
- Zone invalid/fill/mitigation trước setup.
- Duplicate delivery.
- Batch/incremental/replay parity toàn bộ field.

### Selector tests

- Zero eligible → no trade.
- Một eligible → select.
- Nhiều candidate cùng cluster → một decision.
- Cùng direction khác cluster → stable ranking.
- Opposite direction conflict → score-gap hoặc no trade.
- Tie-break deterministic với input order khác nhau.
- Không cộng trùng evidence.

### Integration tests

- Sweep → MSS → FVG → S01 → order N+1.
- Bias → BOS → OB → S05 → order N+1.
- Window → sweep → MSS → FVG → S09 → order N+1.
- Ba strategy cùng chạy nhưng chỉ một position setup được execution.
- Full replay cutoff tại mọi transition state.

### Performance tests

- Benchmark context building, ba templates, gate, dedup và selector riêng.
- Benchmark end-to-end 10.000 bars.
- Worst case: nhiều active events/candidates và duplicate clusters.
- Không tuyên bố O(1) tuyệt đối; ghi complexity theo active-state/config bounds.

---

## 16. Cổng nghiệm thu

### Gate A — Spec ready

- ADR semantics S01/S05/S09 đã chốt.
- Model/API và same-bar ordering rõ ràng.
- Không còn câu hỏi có thể làm thay đổi kiến trúc Wave 1.

### Gate B — Framework ready

- T53.1–T53.3 pass unit tests.
- Context snapshot chứng minh zero-lookahead.
- Existing full suite không regression.

### Gate C — Wave 1 strategies ready

- S01, S05, S09 pass toàn bộ strategy tests.
- Long/short symmetry.
- Batch/incremental/replay parity.
- Không duplicate setup.

### Gate D — Selector ready

- Gate/dedup/conflict/selector deterministic.
- No-trade policy hoạt động.
- Không double-count evidence.

### Gate E — Integration ready

- Signal bar N chỉ fill từ N+1.
- Full Python/Node tests pass.
- Benchmark normal/worst-case được ghi lại.
- `compileall` và `git diff --check` pass.
- Independent QC không còn P0/P1.

Chỉ sau Gate E mới mở Wave 2.

---

## 17. Wave 2 và Wave 3

### Wave 2

- S03 Sweep → CHoCH → FVG.
- S02 Sweep → CHoCH → OB.
- S06 BOS → FVG Retest.
- S10 Asian Range Sweep.

### Wave 3

- S04 OB + FVG Overlap.
- S07 FVG Continuation.
- S08 OB Continuation.

Mỗi strategy mới phải dùng framework đã có, không copy detector và không tạo đường execution riêng.

---

## 18. Giai đoạn sau T53

Sau khi cả 10 strategy chạy ổn định:

1. Thu thập performance theo strategy/regime bằng walk-forward.
2. Thêm rolling strategy fitness với minimum sample và shrinkage.
3. So sánh static selector với regime selector.
4. Chỉ sau đó nghiên cứu contextual bandit.
5. LLM supervisor chạy shadow mode, chỉ đề xuất shortlist và giải thích.
6. Risk gate và execution luôn deterministic.

---

## 19. Rủi ro chính

| Rủi ro | Biện pháp |
|---|---|
| Lookahead qua regime/percentile | Expanding/rolling past-only, replay probes |
| Strategy overlap tạo nhiều lệnh | Stable evidence IDs và SetupCluster |
| Over-filtering không có lệnh | Telemetry từng gate, bắt đầu ba required conditions |
| Score che mất required sequence | Gate required events trước scoring |
| Strategy switching quá nhanh | Hysteresis, cooldown, minimum score gap |
| Selector overfit | Baseline cố định, walk-forward, OOS và cost stress |
| Code lặp giữa strategies | Shared context/helpers, template protocol |
| Làm hỏng BacktestEngine | Adapter tương thích, không rewrite sớm |
| Tài liệu và code lệch nhau | ADR + acceptance tests + changelog |

---

## 20. Quyết định khuyến nghị để bắt đầu

1. Chấp nhận kiến trúc deterministic multi-strategy trước LLM.
2. Giữ `run_smc_strategy()` cũ làm baseline trong Wave 1.
3. Tạo package domain mới `smc/engine/`; top-level `engine/` tiếp tục là platform/backtest layer.
4. Thực hiện T53.0 trước: thảo luận và khóa semantics cho S01, S05, S09.
5. Không bắt đầu code cả ba strategy trước khi models/context contract pass QC.
