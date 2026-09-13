# T53.8 — Deterministic Selector & Audit Telemetry Implementation Plan

## 1. Trạng thái tài liệu

- Task: `T53.8 - Selector & telemetry`.
- Phụ thuộc: T53.1–T53.7 đã hoàn tất; đặc biệt đầu vào trực tiếp là `ConfluenceBatch` của T53.7.
- Trạng thái hiện tại: **PLAN / Gate A**, chưa triển khai production runtime.
- Quyết định kiến trúc mới: ADR 24 `[PROPOSED]`, chỉ chuyển sang `[ACCEPTED]` sau khi plan được QC và người dùng phê duyệt.
- Task kế tiếp T53.9 vẫn `planned`; fill Open N+1, spread, commission, cash-RR và cooldown không thuộc T53.8.

---

## 2. Bối cảnh và mục tiêu

T53.7 đã tạo pipeline deterministic:

```text
StrategyContext N
  -> StrategyRegistry (S01/S05/S09)
  -> EligibilityGate
  -> EvidenceDeduplicator
  -> DirectionConflictDetector
  -> ConfluenceBatch
```

T53.8 bổ sung tầng cuối trước execution:

```text
ConfluenceBatch + StrategyContext N
  -> deterministic component scoring
  -> primary owner trong từng evidence cluster
  -> best cluster của từng direction
  -> minimum score / direction score-gap policy
  -> SelectionDecision (SELECT hoặc NO_TRADE)
  -> immutable per-bar audit
  -> pure aggregate telemetry
```

Mục tiêu là chọn tối đa một setup tại mỗi closed bar N bằng luật cố định, có thể replay chính xác, không dùng LLM, không học online, không dùng hiệu suất tương lai và không nhìn Open N+1.

---

## 3. Phạm vi

### 3.1. Trong phạm vi

1. Cấu hình selector V1 bất biến và strict.
2. Chấm bốn thành phần `regime`, `setup`, `context`, `execution_proxy` trên thang `[0, 100]`.
3. Tính `total_score` theo trọng số ADR 16.
4. Tạo `StrategyEvaluation` mới cho candidate `ELIGIBLE`; giữ nguyên evaluation `REJECTED` để audit.
5. Chọn primary owner và supporting strategies trong mỗi `EvidenceCluster`.
6. Xếp hạng nhiều cluster cùng hướng bằng tie-break deterministic.
7. Xử lý BUY/SELL conflict bằng score gap 15.0.
8. Áp minimum total score 60.0.
9. Sinh đúng một `SelectionDecision` cho mỗi `ConfluenceBatch`.
10. Sinh per-bar audit record strict, immutable, exact JSON round-trip.
11. Tổng hợp funnel telemetry bằng hàm pure từ danh sách audit records.
12. Batch/incremental/JSON/future-append parity và production integration.

### 3.2. Ngoài phạm vi

- Không sửa `BacktestEngine`.
- Không tạo signal series và không khớp lệnh.
- Không đọc Open/High/Low/Close của N+1.
- Không tính actual entry, executable SL/TP, spread, commission hoặc cash-basis RR tại fill.
- Không kích hoạt cooldown; cooldown chỉ bắt đầu sau vị thế thực sự mở ở T53.9.
- Không fallback sang candidate hạng hai sau khi candidate được chọn bị Fill Gate từ chối ở N+1.
- Không dùng rolling win rate, PnL, drawdown, strategy fitness, contextual bandit hoặc LLM.
- Không tối ưu trọng số bằng dữ liệu lịch sử trong task này.
- Không sửa detector hoặc semantics S01/S05/S09, ngoại trừ metadata-only enrichment đã nêu rõ tại mục 7.5.
- Không triển khai UI/dashboard telemetry.

---

## 4. Nguồn contract hiện có

Implementation phải đọc và tuân thủ:

- `.agent/DECISIONS.md`: ADR 15, 16, 17, 19, 20, 21, 22, 23 và ADR 24 sau khi được duyệt.
- `T53_WAVE1_SEMANTICS.md`: scoring weights, score gap, ownership, precision và reason codes.
- `T53_7_REGIME_GATE_DEDUP_CONFLICT_IMPLEMENTATION_PLAN.md`: contract `ConfluenceBatch`, cluster identity và conflict.
- `smc/engine/models.py`: `CandidateSetup`, `MarketRegime`, `StrategyEvaluation`, `SelectionDecision`, `make_decision_id`.
- `smc/engine/confluence.py`: `EvidenceCluster`, `DirectionConflict`, `ConfluenceBatch`.
- `smc/engine/eligibility.py`: regime matrix và hard-gate output.

Không được đoán field hoặc metadata. Trước khi code, đọc lại phiên bản mới nhất của các file trên.

---

## 5. Các quyết định cần khóa tại Gate A

### 5.1. Selector chỉ dùng dữ liệu as-of N

API selector nhận cả `ConfluenceBatch` và `StrategyContext` của cùng bar N. Context chỉ dùng để chấm bias/session/ATR tại signal bar và phải thỏa:

```text
batch.bar_index == context.bar_index
batch.timestamp == context.bar_close_time
batch.regime.bar_index == context.bar_index
batch.regime.timestamp == context.bar_close_time
```

Candidate, evidence, context hoặc regime có timestamp/bar index sau N là integrity error. Selector không truy cập DataFrame đầy đủ và không nhận future bars.

### 5.2. Execution score tại T53.8 là planned proxy

`S_exec` của T53.8 dùng `CandidateSetup.planned_rr`, tức structural pre-fill RR đã biết tại N. Tên telemetry bắt buộc ghi `execution_score_basis="planned_rr_pre_fill"`.

T53.8 không gọi đây là cash-RR. T53.9 vẫn bắt buộc tính lại cash-basis RR tại Open N+1 sau spread/commission và có quyền hủy lệnh bằng `insufficient_rr_at_fill` hoặc `geometry_violation_at_fill`.

### 5.3. Cluster không cộng phiếu hoặc cộng điểm giữa strategies

Score của cluster bằng score của member xếp hạng cao nhất. Không lấy tổng, trung bình hoặc cộng bonus theo số strategy. Điều này bảo vệ nguyên tắc “một opportunity = một risk unit” và ngăn S01+S09 nhân đôi sức nặng chỉ vì cùng phát hiện một setup.

Member cao nhất là `primary_owner`; các strategy khác trong cluster chỉ là supporting attribution.

### 5.4. Thứ tự minimum-score và direction conflict

1. Chấm tất cả cluster có member eligible.
2. Tìm best BUY và best SELL, chưa lọc minimum score.
3. Nếu hai hướng cùng tồn tại:
   - `gap = round(abs(best_buy_score - best_sell_score), 2)`.
   - `gap < 15.0` -> `NO_TRADE / conflicting_direction`, kể cả một phía có score dưới 60.
   - `gap >= 15.0` -> hướng điểm cao thắng; exact 15.0 được chọn.
4. Nếu chỉ có một hướng, best cluster của hướng đó là winner tạm thời.
5. Winner chỉ được `SELECT` khi `total_score >= 60.0`; exact 60.0 pass.
6. Winner dưới 60.0 -> `NO_TRADE / insufficient_score`.

Không được lọc bỏ phía dưới 60 trước khi đo conflict, vì như vậy có thể che mất trạng thái thị trường đối hướng sát điểm.

### 5.5. Bốn outcome reason của selector

Decision-level reason tách khỏi 18 gate/fill reason codes:

- `ok`: đã chọn setup.
- `no_eligible_setup`: batch không có eligible cluster.
- `insufficient_score`: có eligible cluster nhưng winner dưới 60.
- `conflicting_direction`: có BUY và SELL, score gap dưới 15.

Gate rejection reasons vẫn nằm trong `StrategyEvaluation.rejection_reasons`; selector không ghi đè hoặc biến chúng thành một reason chung.

---

## 6. File dự kiến

### Tạo mới

- `smc/engine/selector.py`: config, scorer, cluster scorecard và selector.
- `smc/engine/telemetry.py`: per-bar audit model và pure aggregator.
- `tests/test_smc_engine_selector.py`.
- `tests/test_smc_engine_telemetry.py`.
- `tests/test_smc_engine_t53_8_integration.py`.

### Có thể sửa

- `smc/engine/models.py`: siết invariant `StrategyEvaluation` và `SelectionDecision` phục vụ output thật.
- `smc/engine/__init__.py`: public exports.
- `smc/engine/strategies/s01_ict_2022.py`: chỉ bổ sung `sweep_type` vào immutable evidence details.
- `smc/engine/strategies/s09_ict_silver_bullet.py`: chỉ bổ sung `sweep_type` vào immutable evidence details và bỏ/chuẩn hóa placeholder `quality_scores={"base_score": 100.0}` nếu nó mâu thuẫn scorer.
- Test S01/S09 tương ứng nếu full-payload fixture thay đổi.
- `.agent/DECISIONS.md`, `.agent/TASKS.md`, `.agent/CHANGELOG.md`, `walkthrough.md`.

Không sửa `engine/backtest_engine.py`, `engine/strategies.py`, API/UI hoặc T53.9 adapter trong task này.

---

## 7. Component scoring V1

### 7.1. Config

```python
@dataclass(frozen=True)
class SelectorConfig:
    regime_weight: float = 0.25
    setup_weight: float = 0.35
    context_weight: float = 0.25
    execution_weight: float = 0.15
    minimum_total_score: float = 60.0
    minimum_direction_gap: float = 15.0
    version: str = "selector-v1"
```

Validation:

- Reject bool-as-number, NaN, Inf, số âm và score ngoài `[0,100]`.
- Mỗi weight trong `[0,1]`, tổng sau validation phải bằng 1.0 trong tolerance cố định `1e-12`; không tự normalize.
- Version là non-empty stable token/string.
- Exact JSON round-trip; unknown field reject.
- Constants/maps scoring bọc read-only, không bị caller mutate.

### 7.2. Regime score

- Dùng giá trị matrix từ T53.7.
- Với evaluation `ELIGIBLE`, scorer recompute bằng `get_regime_matrix_score(strategy_id, direction, batch.regime.regime)` và yêu cầu bằng `evaluation.regime_score` đã ghi bởi gate.
- Sai khác là integrity/programming error, không âm thầm chọn một giá trị.
- Không thay đổi regime hoặc dùng score performance lịch sử.

### 7.3. Setup score

#### S05

Đọc duy nhất `order_block` evidence canonical:

| `details.quality` | Base |
|---|---:|
| `premium_candidate` | 90.0 |
| `strong` | 75.0 |
| `base` | 60.0 |

Thiếu OB evidence đã được Gate reject. Nếu evaluation khai báo `ELIGIBLE` nhưng quality thiếu/không hợp lệ, raise integrity error; không fallback lạc quan.

#### S01/S09

1. Nếu `structure_event.details.displacement is True` -> base 85.0.
2. Nếu không có displacement và `context.atr14 > 0`, đồng thời FVG gap `(top-bottom) >= context.atr14` -> base 80.0.
3. Còn lại -> base 65.0.

Quyết định V1 dùng ATR14 tại signal bar N vì đó là ATR duy nhất nằm trong contract `StrategyContext`; không truy hồi ATR từ full DataFrame và không giả mạo ATR tại FVG formation bar. Audit phải ghi `fvg_atr_basis="signal_bar_atr14"`.

#### Bonuses

Mỗi bonus tối đa một lần trên một candidate:

- `+10`: có liquidity sweep evidence với `details.sweep_type == "clean"`.
- `+10`: sweep/pool evidence có `pool_kind` hoặc `kind` là `equal_highs`/`equal_lows`.
- `+10`: candidate có cả `fair_value_gap` và `order_block` evidence, thể hiện direct zone confluence.

Không cộng bonus theo số evidence trùng; dùng evidence kind/ID unique. Clamp `[0,100]`, round 2 decimals.

### 7.4. Context score

Bias relation:

- Candidate BUY + bullish bias, hoặc SELL + bearish bias: 60.
- Neutral: 30; chỉ có thể còn eligible với S01/S09.
- Opposed hoặc missing: integrity error nếu evaluation đang `ELIGIBLE`, vì T53.7 phải hard reject.

Session:

- S09 có canonical `window_name/window_key` đã qua Gate: 40.
- S01/S05: `context.session_decision.in_session is True`: 40.
- S01/S05 không có active session: 10.

`S_context = clamp(bias + session, 0, 100)`. Không tự suy session bằng giờ hệ thống và không dùng wall clock.

### 7.5. Metadata-only enrichment

S01/S09 hiện có `LiquiditySweepSnapshot.sweep_type` nhưng evidence payload chưa ghi field này. Trước scoring production, bổ sung:

```python
details={
    ...,
    "sweep_type": sweep.sweep_type,
}
```

Đây chỉ là audit/scoring metadata as-of N, không đổi trigger, state machine, candidate identity, entry/SL/TP hoặc cluster ID. Full-payload tests của hai strategy phải được cập nhật và future-append parity phải giữ nguyên.

Không dùng placeholder `CandidateSetup.quality_scores` làm source of truth. Scorer luôn recompute từ candidate evidence/context. Nếu giữ field này vì backward compatibility, nó chỉ là producer metadata và không được phép override công thức V1.

### 7.6. Planned execution score

Với `RR = candidate.planned_rr`:

```text
RR < 1.5       -> 0.0 (trạng thái này không được ELIGIBLE; nếu gặp thì integrity error)
1.5 <= RR < 2  -> 30 + ((RR - 1.5) / 0.5) * 30
2 <= RR < 3    -> 60 + ((RR - 2.0) / 1.0) * 40
RR >= 3        -> 100
```

Kiểm tra exact boundaries 1.50, 2.00, 3.00; clamp và round 2 decimals. Không dùng giá Open N+1, spread, commission hoặc lot size.

### 7.7. Total score

```text
total = round(
    0.25 * regime_score
  + 0.35 * setup_score
  + 0.25 * context_score
  + 0.15 * exec_score,
  2,
)
```

Tính total từ chính bốn component đã clamp/round và được lưu trong output. Không cộng score giữa member, không dùng evidence count như vote.

`StrategyEvaluation.details` sau scoring phải giữ thông tin Gate cũ và bổ sung namespace `scoring` gồm version, raw feature flags, base/bonus breakdown, bias/session relation, RR basis và weights. Không ghi đè audit của Gate.

Evaluation `REJECTED` được giữ nguyên payload; scorer không cho điểm để “cứu” hard failure.

---

## 8. Cluster ownership và stable ranking

### 8.1. `ClusterScorecard`

Tạo frozen strict model tối thiểu:

```python
@dataclass(frozen=True)
class ClusterScorecard:
    cluster_id: str
    direction: Literal["BUY", "SELL"]
    primary_setup_id: str
    primary_strategy_id: str
    supporting_strategy_ids: tuple[str, ...]
    member_setup_ids: tuple[str, ...]
    total_score: float
    regime_score: float
    setup_score: float
    context_score: float
    exec_score: float
    planned_rr: float
```

Rank key là chi tiết tính toán nội bộ, không serialize như dữ liệu nguồn. Model phải kiểm tra total từ bốn component/weights, kiểm tra IDs unique/canonical và được tạo qua factory từ scored cluster; không tin payload tampered. Deep immutable, unknown fields reject, exact JSON round-trip.

### 8.2. Member rank

Sort ascending theo key sau, trong đó score/RR cao hơn được đổi dấu:

```text
(
  -total_score,
  -setup_score,
  -context_score,
  -exec_score,
  -regime_score,
  -planned_rr,
  strategy_id,
  setup_id,
)
```

Member đầu là primary. `supporting_strategy_ids` là unique/sorted strategy IDs của các member còn lại, loại primary ID. Hai member cùng strategy không tạo supporting ID giả.

### 8.3. Cluster/direction rank

Nhiều cluster cùng direction xếp theo primary member rank, sau đó `cluster_id` làm khóa cuối. Không phụ thuộc input order, dict/set order, object identity hoặc Python `hash()`.

`cluster.total_score` chính là primary member total score. Evidence overlap chỉ được phản ánh trong component rules đã khóa, không tự cộng số strategy.

---

## 9. Selector API và decision policy

### 9.1. API đề xuất

```python
class DeterministicStrategySelector:
    def __init__(self, config: SelectorConfig | None = None): ...

    def select(
        self,
        batch: ConfluenceBatch,
        context: StrategyContext,
    ) -> SelectorOutput: ...

def select_strategy(
    batch: ConfluenceBatch,
    context: StrategyContext,
    config: SelectorConfig | None = None,
) -> SelectorOutput: ...
```

Selector V1 stateless/pure. Cùng input/config phải cho exact output giống nhau. Không cần cache, reset hoặc lock state; thread safety đến từ immutability.

### 9.2. `SELECT`

- `selected_setup`: candidate của primary member trong winning cluster.
- `primary_strategy_id`: primary member strategy ID.
- `supporting_strategy_ids`: attribution unique/sorted của cluster thắng.
- `evaluations`: toàn bộ batch evaluations, trong đó eligible members được thay bằng scored evaluations; rejected evaluations giữ nguyên.
- `regime`: batch regime.
- `reason="ok"`.
- `score_gap`: chỉ non-null khi cả hai direction tồn tại; nếu chỉ một direction, dùng `None`.
- `execution_payload={}` trong T53.8; T53.9 mới tạo signal/SL/TP payload.
- `meta` tối thiểu chứa `selector_version`, `symbol`, `timeframe`, `selected_cluster_id`, `cluster_total_score` và `execution_score_basis="planned_rr_pre_fill"` để T53.9 có audit source rõ ràng.
- `decision_id = make_decision_id(N, "SELECT", primary_strategy_id)`.

### 9.3. `NO_TRADE`

- `selected_setup=None`.
- `primary_strategy_id=None`.
- `supporting_strategy_ids=()`.
- `execution_payload={}`.
- `decision_id = make_decision_id(N, "NO_TRADE", None)`.
- `reason` theo mục 5.5.
- `score_gap` chỉ có giá trị với `conflicting_direction`; trường hợp khác là `None`.
- Vẫn lưu toàn bộ scored/rejected evaluations để audit.
- `meta` vẫn chứa selector version, symbol/timeframe, thresholds và execution-score basis; không chứa active signal.

### 9.4. Siết `SelectionDecision`

Model hiện có phải được kiểm tra và bổ sung regression cho các invariant:

- `decision_id` đúng canonical ID suy từ bar/action/primary.
- Timestamp/regime bar/time đồng nhất.
- Evaluations unique theo setup ID và canonical sort.
- Với SELECT, selected setup phải xuất hiện đúng một lần trong evaluations với status ELIGIBLE và payload giống hệt.
- Primary ID phải trùng strategy của selected setup.
- Supporting IDs unique/sorted, không chứa primary, và đúng tập strategy còn lại của winning cluster được ghi trong audit/meta.
- Với NO_TRADE, supporting IDs bắt buộc rỗng.
- Reason/action/score-gap cross-field đúng mục 9.2–9.3.
- Unknown fields trong `from_dict()` bị reject.
- Tampered score, selected setup, primary/supporting IDs hoặc execution payload bị fail closed.

Không phá JSON payload hợp lệ của T53.1; nếu cần migration, ghi version rõ và thêm backward-compatibility tests.

---

## 10. Telemetry contract

### 10.1. Nguyên tắc

- Telemetry là audit deterministic, không ảnh hưởng selection.
- Không dùng wall-clock, random UUID hoặc process-local hash.
- Per-bar record được tạo atomically cùng decision.
- Aggregator là pure function; không giữ unbounded state trong selector.
- Không ghi PnL/win rate vì chưa có execution outcome.

### 10.2. `SelectionAuditRecord`

Frozen strict model tối thiểu:

```python
@dataclass(frozen=True)
class SelectionAuditRecord:
    decision_id: str
    bar_index: int
    timestamp: pd.Timestamp
    symbol: str
    timeframe: str
    regime: str
    evaluation_count: int
    eligible_count: int
    rejected_count: int
    cluster_scorecards: tuple[ClusterScorecard, ...]
    direction_conflict_present: bool
    best_buy_score: float | None
    best_sell_score: float | None
    score_gap: float | None
    minimum_total_score: float
    minimum_direction_gap: float
    scoring_weights: tuple[float, float, float, float]
    action: Literal["SELECT", "NO_TRADE"]
    reason: str
    selected_cluster_id: str | None
    selected_setup_id: str | None
    primary_strategy_id: str | None
    supporting_strategy_ids: tuple[str, ...]
    gate_reason_counts: Mapping[str, int]
    selector_version: str
```

Mọi count/ID/action phải cross-validate với decision, evaluations và scorecards. `to_dict()/from_dict()` exact; mappings serialized theo key canonical.

### 10.3. `SelectorOutput`

```python
@dataclass(frozen=True)
class SelectorOutput:
    decision: SelectionDecision
    audit: SelectionAuditRecord
```

Constructor kiểm tra `decision_id`, bar, timestamp, action, reason và selected IDs giữa hai object khớp hoàn toàn.

### 10.4. Aggregate telemetry

```python
def aggregate_selection_telemetry(
    records: Sequence[SelectionAuditRecord],
) -> Mapping[str, Any]: ...
```

Output JSON-safe/canonical tối thiểu:

- `bars_total`, `select_count`, `no_trade_count`.
- `evaluation_count`, `eligible_count`, `rejected_count`, `cluster_count`.
- `decision_reason_counts`.
- `gate_rejection_reason_counts`.
- `selected_strategy_counts`, `selected_direction_counts`, `selected_regime_counts`.
- `direction_conflict_bars`, `conflict_selected_count`, `conflict_no_trade_count`.
- `selector_version` và config thresholds.

Empty input trả counters bằng 0 và mappings rỗng. Mixed selector versions/configs phải raise, không gộp mù. Một aggregate có thể chứa nhiều symbol/timeframe; khóa chống double-count là `(symbol, timeframe, decision_id)`, không chỉ `decision_id`, vì grammar ID hiện tại không chứa market identity.

---

## 11. Error, atomicity và determinism policy

- Schema/type/config/timestamp mismatch: raise `StrictModelTypeError`, `StrategyValidationError` hoặc `StrategyStateError` theo convention hiện có.
- Market condition không đạt minimum score/conflict gap: trả `NO_TRADE`, không raise.
- Eligible evaluation thiếu dữ liệu bắt buộc để chấm score: integrity error, không tự dùng score 0 hoặc default lạc quan.
- Không mutate `batch`, `context`, candidate, evidence hoặc evaluation đầu vào.
- Nếu scoring/audit creation lỗi, không trả partial `SelectionDecision`.
- Mọi collection output canonical tuple/read-only mapping.
- Input permutation phải cho full serialized payload giống hệt.
- Append future bars không được thay đổi decision/audit prefix.
- Không dùng current date/time của máy trong decision hoặc telemetry.

---

## 12. Test matrix tối thiểu

### A. Config và models — 20 tests

1. SelectorConfig defaults exact.
2. Config exact JSON round-trip.
3. Reject bool-as-number.
4. Reject NaN/Inf.
5. Reject negative/out-of-range thresholds.
6. Reject weights không tổng bằng 1.
7. Unknown config field reject.
8. ClusterScorecard deep immutability.
9. Audit deep immutability.
10. SelectorOutput cross-field validation.
11. Unknown model fields reject.
12. NumPy scalar normalization đúng convention.
13. SelectionDecision canonical ID mismatch reject.
14. SELECT thiếu selected evaluation reject.
15. SELECT selected evaluation REJECTED reject.
16. Duplicate evaluation setup ID reject.
17. NO_TRADE có supporting ID reject.
18. Reason/action/score-gap mismatch reject.
19. Exact JSON round-trip toàn output graph.
20. Tampered nested payload fail closed.

### B. Component scoring — 30 tests

21–23. Regime score S01/S05/S09 exact từ matrix.
24. Gate/scorer regime-score mismatch raises.
25–27. S05 base/strong/premium = 60/75/90.
28. S05 missing/invalid quality integrity error.
29. S01 displacement base 85.
30. S09 displacement base 85.
31. Non-displacement FVG >= signal ATR base 80.
32. Non-displacement FVG < ATR base 65.
33. ATR zero guard base 65.
34. Clean sweep bonus once.
35. Wick-only không bonus.
36. Equal-highs bonus once.
37. Equal-lows bonus once.
38. Swing pool không bonus.
39. Direct FVG+OB bonus once.
40. Duplicate evidence không double-count/được model chặn.
41. Setup score clamp 100.
42. BUY aligned bias 60.
43. SELL aligned bias 60.
44. Neutral S01/S09 bias 30.
45. Opposed/missing bias trên ELIGIBLE raises.
46. Active session 40.
47. Outside session 10.
48. S09 canonical window 40.
49. Rejected evaluation không được score cứu.
50. Scoring details ghi đủ source/basis/version.

### C. Planned execution score và total — 15 tests

51. RR 1.49 integrity failure cho eligible.
52. RR 1.50 -> exec 30.
53. RR 1.75 -> exec 45.
54. RR 2.00 -> exec 60.
55. RR 2.50 -> exec 80.
56. RR 3.00 -> exec 100.
57. RR >3 clamp 100.
58. Long/Short symmetry.
59. Total formula exact fixture.
60. Total exact 60 passes.
61. Total 59.99 fails.
62. Score round 2 decimals.
63. Total tính từ stored component values.
64. Không dùng Open N+1.
65. Không dùng spread/commission/cash-RR trong T53.8.

### D. Ownership và same-direction ranking — 20 tests

66. Một member -> primary chính nó.
67. S01/S09 cùng cluster -> score cao hơn làm primary.
68. Supporting IDs unique/sorted.
69. Cùng strategy nhiều member không tạo self-support.
70. Strategy count không cộng cluster score.
71. Cluster score bằng primary score.
72. Total tie -> setup score tie-break.
73. Setup tie -> context score tie-break.
74. Context tie -> exec score tie-break.
75. Exec tie -> regime score tie-break.
76. Component tie -> planned RR tie-break.
77. Full numeric tie -> strategy ID tie-break.
78. Strategy tie -> setup ID tie-break.
79. Cluster tie -> cluster ID cuối.
80. BUY multiple clusters chọn đúng best.
81. SELL mirror.
82. Input member permutation invariant.
83. Input cluster permutation invariant.
84. No hash/object identity dependency.
85. Selected candidate payload giữ nguyên.

### E. Decision policy — 20 tests

86. Zero evaluations -> NO_TRADE/no_eligible_setup.
87. Chỉ rejected -> NO_TRADE/no_eligible_setup.
88. Eligible cluster 59.99 -> insufficient_score.
89. Eligible cluster 60 -> SELECT.
90. One direction selects best only.
91. BUY/SELL gap 14.99 -> conflict NO_TRADE.
92. Gap exact 15 -> winner SELECT.
93. Gap >15 -> winner SELECT.
94. BUY winner symmetry.
95. SELL winner symmetry.
96. Một phía 62, phía kia 59, gap 3 -> conflict NO_TRADE.
97. Một phía 80, phía kia 50, gap 30 -> chọn phía 80.
98. Hai phía dưới 60 nhưng gap nhỏ -> conflicting_direction.
99. Hai phía dưới 60 nhưng gap lớn -> insufficient_score của winner.
100. Exact equal score -> conflict NO_TRADE gap 0.
101. SELECT decision ID canonical.
102. NO_TRADE decision ID canonical.
103. Execution payload luôn rỗng.
104. Tối đa một selected setup mỗi bar.
105. DirectionConflict object và scorecard IDs cross-check.

### F. Telemetry — 20 tests

106. SELECT audit exact.
107. No-eligible audit exact.
108. Insufficient-score audit exact.
109. Conflict audit exact.
110. Gate reason histogram canonical.
111. Multiple rejection reasons counted per evaluation.
112. Cluster/member counts exact.
113. Conflict resolved count.
114. Conflict no-trade count.
115. Selected strategy/direction/regime counts.
116. Empty aggregate.
117. Duplicate decision ID reject.
118. Mixed version reject.
119. Mixed thresholds/config reject.
120. Aggregate input permutation invariant.
121. Audit JSON round-trip.
122. Aggregate JSON dumps succeeds with `allow_nan=False`.
123. Telemetry không tác động decision.
124. No PnL/win-rate field trước execution.
125. Bounded per-bar payload theo số candidate/cluster của bar.

### G. Production integration và QC — tối thiểu 15 tests

126. Production Registry -> Gate -> Confluence -> Selector cho S01.
127. Production pipeline cho S05.
128. Production pipeline cho S09.
129. Production S01/S09 same cluster chỉ tạo một SELECT.
130. S05 cluster riêng cạnh S01/S09 xếp hạng đúng.
131. BUY/SELL production-like conflict dùng gap policy.
132. Batch/incremental decision full-payload parity.
133. JSON replay SelectorOutput exact parity.
134. Append future bars không đổi decision/audit prefix.
135. Input permutation full-payload invariant.
136. Metadata enrichment S01 không đổi trigger/geometry/ID.
137. Metadata enrichment S09 không đổi trigger/geometry/ID.
138. Full T53.7 regression pass.
139. Full strategy/context/registry regression pass.
140. Full repository suite, compileall và diff-check pass.

Test count có thể tăng khi phát hiện boundary mới; không được giảm coverage bằng mock score bỏ qua production scorer.

---

## 13. Benchmark policy

- Selector/scorer phải tuyến tính theo số evaluation/cluster của chính bar, không theo tổng chiều dài stream.
- Benchmark 10.000 synthetic closed bars sau warm-up, có bars rỗng, một hướng, multi-cluster và conflict.
- Báo median, P99 và total wall-clock; không biến timing nhiễu thành correctness gate quá chặt.
- Bắt buộc: state không tăng theo số bar, output deterministic và không có quadratic scan theo history.
- Không giảm workload/candidate count để làm đẹp số.
- ContextBuilder performance debt theo ADR 19 không thuộc T53.8.

---

## 14. Trình tự triển khai

### T53.8.0 — Gate A: khóa plan và ADR 24

- QC tài liệu này.
- Chốt sáu điểm: planned RR basis, signal-bar ATR basis, cluster score không aggregate, conflict-before-minimum-score, tie-break và telemetry schema.
- Chuyển ADR 24 từ PROPOSED sang ACCEPTED sau phê duyệt.
- Chưa code trước Gate A.

### T53.8.1 — Strict config/models

- Implement `SelectorConfig`, `ClusterScorecard`, `SelectionAuditRecord`, `SelectorOutput`.
- Siết `SelectionDecision` invariants và serialization.
- Hoàn tất nhóm test A trước khi đi tiếp.

### T53.8.2 — Component scorer

- Metadata-only enrichment S01/S09.
- Implement bốn component score và total formula.
- Hoàn tất nhóm B–C và strategy full-payload regressions.

### T53.8.3 — Ownership và selector

- Primary/supporting attribution.
- Same-direction rank, BUY/SELL gap, minimum score và NO_TRADE outcomes.
- Hoàn tất nhóm D–E.

### T53.8.4 — Telemetry

- Per-bar audit record.
- Pure aggregate funnel telemetry.
- Hoàn tất nhóm F.

### T53.8.5 — Integration, docs và QC handoff

- Production Registry -> Gate -> Confluence -> Selector tests.
- Batch/incremental/JSON/future-append/permutation parity.
- Full regression, compileall, diff-check và benchmark report.
- Cập nhật `.agent` và `walkthrough.md` bằng kết quả thực chạy.
- Chuyển T53.8 sang `review`, không tự chuyển `done`; T53.9 vẫn `planned` cho tới independent QC.

Không chuyển subtask sau sang doing trước khi subtask hiện tại pass targeted tests.

---

## 15. Lệnh xác minh bắt buộc

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_smc_engine_selector -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_engine_telemetry -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_engine_t53_8_integration -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_engine_regime tests.test_smc_engine_eligibility tests.test_smc_engine_confluence tests.test_smc_engine_t53_7_integration -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_strategy_s01 tests.test_smc_strategy_s05 tests.test_smc_strategy_s09 tests.test_smc_engine_registry tests.test_smc_engine_context_qc -v
.\.venv\Scripts\python.exe -m unittest discover tests -v
.\.venv\Scripts\python.exe -m compileall -q server.py engine smc tests
git diff --check
```

Nếu không sửa frontend thì không cần Node suite. Nếu diff ngoài phạm vi xuất hiện, phải phân loại là thay đổi có sẵn của người dùng và không được sửa/xóa.

---

## 16. Acceptance criteria cuối

- [ ] SelectorConfig strict, immutable, exact JSON round-trip và weights tổng đúng 1.
- [ ] Bốn component scores đúng formula/boundaries và round 2 decimals.
- [ ] Regime score được đối chiếu với T53.7 matrix, không tin payload sai.
- [ ] Setup bonuses tính tối đa một lần/evidence và không double-count.
- [ ] Planned execution score không đọc N+1/spread/commission/cash-RR.
- [ ] Rejected evaluations được giữ nguyên và không được score cứu.
- [ ] Cluster score bằng primary member, không cộng vote theo số strategy.
- [ ] Primary/supporting attribution deterministic và không self-support.
- [ ] Same-direction tie-break ổn định đến setup/cluster ID.
- [ ] Conflict gap `<15` NO_TRADE; exact `15` chọn bên thắng.
- [ ] Conflict được xử lý trước minimum score theo ADR 24.
- [ ] Minimum total `<60` NO_TRADE; exact `60` SELECT.
- [ ] Mỗi bar tối đa một selected setup.
- [ ] SelectionDecision cross-field strict và canonical decision ID.
- [ ] Execution payload rỗng trong T53.8; fill/cooldown vẫn thuộc T53.9.
- [ ] Per-bar audit và aggregate telemetry immutable, deterministic, exact JSON-safe.
- [ ] Duplicate decision không bị double-count; mixed config/version fail closed.
- [ ] Production S01/S05/S09 đi xuyên toàn pipeline đến selector.
- [ ] Batch/incremental/JSON/future-append/permutation full-payload parity pass.
- [ ] Existing T53.7/strategies/context/registry regressions pass.
- [ ] Full suite, compileall và git diff-check pass.
- [ ] Independent QC không còn P0/P1 trước khi đánh dấu done.
- [ ] T53.9 không bị triển khai sớm.

---

## 17. Rủi ro và biện pháp

| Rủi ro | Biện pháp |
|---|---|
| Score dùng dữ liệu N+1 | API chỉ nhận batch/context N; execution payload rỗng; future-append probes |
| Planned RR bị gọi nhầm cash-RR | Ghi basis rõ trong audit; T53.9 bắt buộc revalidate |
| S01+S09 bị cộng như hai phiếu | Cluster score bằng primary member, không aggregate |
| Metadata thiếu làm bonus ngẫu nhiên | Metadata enrichment rõ; thiếu required scoring feature fail/conservative theo contract |
| Tie phụ thuộc input order | Rank key đầy đủ với lexical IDs cuối |
| Lọc minimum score che conflict | Conflict-before-minimum-score được khóa và test boundaries |
| Telemetry làm thay đổi quyết định | Audit tạo sau scoring từ cùng immutable facts; parity test decision có/không telemetry |
| Model hiện tại nhận payload mâu thuẫn | Siết SelectionDecision và nested audit validation |
| Tối ưu theo lịch sử quá sớm | Không dùng PnL/win-rate/rolling fitness trong Wave 1 |

---

## 18. Gate phê duyệt

ADR 24 đề xuất chốt:

1. `S_exec` ở T53.8 dùng planned structural RR tại N; cash-RR vẫn thuộc Fill Gate T53.9.
2. FVG size fallback dùng ATR14 tại signal bar N và ghi rõ basis; không retrospective lookup.
3. Cluster score bằng primary member score, tuyệt đối không cộng số strategy/evidence như vote.
4. Direction conflict được xét trước minimum score; gap 15.0 inclusive.
5. Tie-break dùng component scores -> RR -> strategy/setup/cluster IDs, hoàn toàn deterministic.
6. Telemetry gồm immutable per-bar audit + pure aggregate, không dùng performance outcome để chọn lệnh.

Chỉ bắt đầu T53.8.1 sau khi Gate A được QC và người dùng phê duyệt.
