# KẾ HOẠCH TRIỂN KHAI T53.7 — REGIME, ELIGIBILITY GATE, DEDUP & CONFLICT

> Ngày lập: 2026-09-10  
> Trạng thái: PLAN — chờ QC/phê duyệt trước khi implementation  
> Phụ thuộc đã hoàn tất: T53.1, T53.2, T53.3, T53.4, T53.5, T53.6  
> Phạm vi kế tiếp: T53.8 Selector & Telemetry; T53.9 Backtest Adapter

---

## 1. Mục tiêu

T53.7 xây lớp deterministic nằm sau `StrategyRegistry` và trước selector:

```text
StrategyContext N
      +
Registry output {S01, S05, S09 -> CandidateSetup[]}
      ↓
MarketRegimeClassifier V1
      ↓
EligibilityGate
      ↓
EvidenceDeduplicator
      ↓
DirectionConflictDetector
      ↓
ConfluenceBatch cho T53.8
```

Kết quả phải trả lời được bốn câu hỏi mà không dùng LLM và không đọc tương lai:

1. Thị trường tại closed bar N thuộc regime nào?
2. Candidate nào đủ điều kiện cứng và candidate nào bị loại vì lý do gì?
3. Candidate nào từ nhiều strategy thực chất là cùng một cơ hội/rủi ro?
4. Có đồng thời cơ hội BUY và SELL hay không?

T53.7 không chọn lệnh cuối, không tính tổng điểm, không áp dụng khoảng cách điểm 15 và không tạo signal cho BacktestEngine. Các việc đó thuộc T53.8–T53.9.

---

## 2. Phạm vi

### 2.1. Làm trong T53.7

- Rule-based `MarketRegimeClassifier` với đúng 5 trạng thái đã khóa.
- Stateful trailing buffers cho close, ATR14 và recent market events.
- Warm-up, Kaufman ER, ATR percentile và cây phân loại deterministic.
- Bảng `Strategy × Direction × Regime` đủ 30 ô.
- Eligibility Gate trả `StrategyEvaluation` và reason codes chuẩn hóa.
- Dedup candidate theo canonical opportunity identity.
- Phát hiện conflict BUY/SELL, giữ đủ dữ liệu cho selector xử lý.
- Frozen/JSON-safe confluence models cần thiết giữa T53.7 và T53.8.
- Atomicity, idempotency, reset, bounded state, permutation invariance.
- Batch/incremental/JSON replay/future-append parity.
- Package exports, tests và tài liệu `.agent`.

### 2.2. Không làm trong T53.7

- Không tính `setup_score`, `context_score`, `exec_score` hoặc `total_score` cuối.
- Không chọn `primary_strategy_id` dựa trên score.
- Không áp dụng `minimum_score_gap=15` để chọn BUY hoặc SELL.
- Không tạo `SelectionDecision` cuối cùng.
- Không tạo `execution_payload`, signal series hoặc fill N+1.
- Không áp dụng cooldown sau fill; chưa có fill trong tầng này.
- Không tính cash-based RR có spread/commission tại Open N+1.
- Không sửa semantics của S01/S05/S09 hoặc detector.
- Không tối ưu weights, ML, bandit hay LLM selector.
- Không triển khai T53.8/T53.9 sớm.

---

## 3. Những contract hiện có phải tái sử dụng

- `StrategyContext` là snapshot immutable as-of closed bar N.
- `StrategyRegistry.evaluate_enabled(context)` là nguồn candidate duy nhất.
- `CandidateSetup.evidence_cluster_id` là identity opportunity do strategy tạo.
- S01 và S09 đã dùng cùng cluster convention khi cùng MSS/FVG.
- `MarketRegime`, `StrategyEvaluation` đã có trong `smc/engine/models.py`.
- `StrategyEvaluation` là immutable; T53.7 tạo bản gate-stage với:
  - `regime_score` đã xác định;
  - `setup_score=context_score=exec_score=total_score=0.0`;
  - `details["evaluation_stage"] = "eligibility_gate"`.
- T53.8 sẽ tạo evaluation enriched mới thay vì mutate object T53.7.
- Dùng `validate_as_of_evidence()` và các guards hiện có; không viết đường snapshot thứ hai.

---

## 4. Cấu trúc file dự kiến

| File | Vai trò |
|---|---|
| `smc/engine/regime.py` | Config, stateful classifier, batch helper |
| `smc/engine/eligibility.py` | 30-cell matrix, reason catalog, Eligibility Gate |
| `smc/engine/confluence.py` | Frozen cluster/result models, dedup và conflict detector |
| `smc/engine/errors.py` | Thêm lỗi integrity chuyên biệt nếu thực sự cần |
| `smc/engine/__init__.py` | Public exports đã chốt |
| `tests/test_smc_engine_regime.py` | Regime correctness, parity, atomicity, state bounds |
| `tests/test_smc_engine_eligibility.py` | 30-cell matrix và hard-gate tests |
| `tests/test_smc_engine_confluence.py` | Dedup, collision, conflict, permutation tests |
| `tests/test_smc_engine_t53_7_integration.py` | Registry → regime → gate → cluster/conflict |

Không tạo orchestrator/selector hoàn chỉnh trong T53.7.

---

## 5. Market Regime Classifier V1

### 5.1. Config

```python
@dataclass(frozen=True)
class RegimeClassifierConfig:
    close_lookback: int = 20
    atr_lookback: int = 100
    er_threshold: float = 0.30
    volatile_atr_percentile: float = 60.0
    recent_sweep_bars: int = 20
    structure_mode: str = "swing"
```

Validation:

- Không chấp nhận bool ở trường int/float.
- Lookback là int dương; `close_lookback >= 2`.
- Threshold finite; `0 <= er_threshold <= 1`.
- Percentile finite; `0 <= value <= 100`.
- `structure_mode in {"swing", "internal"}`; Wave 1 mặc định `swing` để tránh đếm trùng/noise giữa hai hierarchy.
- Exact `to_dict()/from_dict()`, reject unknown fields và JSON round-trip.

### 5.2. Input và lifecycle

API:

```python
class MarketRegimeClassifier:
    def update(self, context: StrategyContext) -> MarketRegime: ...
    def reset(self) -> None: ...

def classify_market_regimes(
    contexts: Sequence[StrategyContext],
    config: RegimeClassifierConfig | None = None,
) -> tuple[MarketRegime, ...]: ...
```

Lifecycle:

- First bar có thể có `bar_index` bất kỳ.
- Sau first bar, yêu cầu contiguous `N == last_N + 1` để trailing state không mất dữ liệu.
- Timestamp và `bar_close_time` phải tăng nghiêm ngặt.
- Same bar + full payload giống trả exact cached object.
- Same bar + payload khác raise `StrategyStateError`.
- Gap/backward/non-increasing time raise trước mutation.
- Mọi mutation dùng working copies và commit nguyên tử cuối update.
- `reset()` xóa toàn bộ buffers, seen-event keys và retry cache.

### 5.3. Bounded state

Classifier chỉ giữ:

- 20 close gần nhất.
- 100 ATR14 finite gần nhất.
- Structure/sweep identity trong trailing interval cần thiết, prune theo bar index.
- Last context payload/result cho idempotency.

Không giữ toàn bộ context hoặc stream. Complexity theo bar phải phụ thuộc vào các bounded collections, không tăng theo lịch sử.

### 5.4. Kaufman Efficiency Ratio

Với 20 close `[c0 ... c19]`:

```text
change = abs(c19 - c0)
denom  = sum(abs(c[i] - c[i-1]) for i=1..19)
ER     = 0.0 nếu denom == 0.0, ngược lại change / denom
```

- Clamp sai số floating nhỏ vào `[0,1]`.
- Lưu `efficiency_ratio` theo precision của `MarketRegime`.
- Không dùng centered rolling, future row hoặc full-sample normalization.

### 5.5. ATR percentile

- Buffer gồm 100 `atr14` finite gần nhất, bao gồm bar N.
- Dùng empirical mid-rank để xử lý tie deterministic:

```text
less  = count(v < current_atr)
equal = count(v == current_atr)
percentile = 100 * (less + 0.5 * equal) / count(buffer)
```

- Với 100 giá trị bằng nhau, percentile phải là 50.0, không phải 0 hoặc 100.
- Không dùng percentile của toàn dataset.

### 5.6. Event features

Trong `[N-19, N]`:

- `bullish_bos_count`: số BOS bullish, mode bằng `structure_mode`.
- `bearish_bos_count`: số BOS bearish, mode bằng `structure_mode`.
- CHoCH không được tính như BOS.
- Event rolling-buffer lặp lại qua nhiều context chỉ được đếm một lần bằng canonical identity:

```text
(mode, event_type, direction, index, broken_swing_index, structure_leg_id)
```

- `recent_sweep=True` nếu có sweep `valid=True`, `index/confirmed_at/swept_at <= N`, mode phù hợp và `0 <= N-sweep.index <= 20`.
- Sweep lặp lại trong rolling context không được nhân số lần.

### 5.7. Warm-up

Chỉ classifier output regime có ý nghĩa khi:

```text
close_count >= 20
AND finite_atr14_count >= 100
```

Thiếu một trong hai:

```python
MarketRegime(
    regime="uncertain",
    reason="insufficient_warmup_bars",
    ...
)
```

Không dùng `N >= 99` thay cho số phần tử hợp lệ.

### 5.8. Cây quyết định và priority

Sau warm-up, đánh giá đúng thứ tự sau; nhánh đầu tiên khớp là kết quả:

1. `bullish_trend`:
   - HTF bias bullish;
   - bullish BOS count >= 1;
   - bearish BOS count == 0;
   - ER >= 0.30.
2. `bearish_trend`: đối xứng hoàn toàn.
3. `volatile_reversal`:
   - ATR percentile >= 60.0;
   - recent sweep true.
4. `ranging`:
   - ER < 0.30;
   - bullish BOS count == 0;
   - bearish BOS count == 0;
   - ATR percentile < 60.0.
5. `uncertain`: mọi trường hợp còn lại.

Priority trend trước volatile reversal là contract bắt buộc khi nhiều điều kiện đồng thời đúng.

`MarketRegime.metrics` tối thiểu chứa close/ATR counts, ER inputs, ATR percentile inputs, BOS counts và recent-sweep identity. `timestamp` của regime dùng `context.bar_close_time`, không dùng wall clock.

---

## 6. Ma trận Regime 30 ô

Định nghĩa constant immutable, không viết chuỗi `if/elif` rải rác:

| Strategy | Direction | bullish_trend | bearish_trend | volatile_reversal | ranging | uncertain |
|---|---|---:|---:|---:|---:|---:|
| S05 | BUY  | 100 | 0 | 40 | 60 | 30 |
| S05 | SELL | 0 | 100 | 40 | 60 | 30 |
| S01 | BUY  | 75 | 0 | 100 | 60 | 30 |
| S01 | SELL | 0 | 75 | 100 | 60 | 30 |
| S09 | BUY  | 80 | 0 | 100 | 60 | 30 |
| S09 | SELL | 0 | 80 | 100 | 60 | 30 |

Contract:

- Chỉ ba strategy Wave 1 được chấp nhận.
- Mọi ô phải tồn tại; thiếu ô là config/programming error, không fallback.
- Điểm 0.0 là hard reject `wrong_regime`.
- `uncertain` điểm 30 vẫn ALLOW, kể cả warm-up chưa đủ; trạng thái uncertain sẽ bị penalize ở T53.8 chứ không hard reject.
- Điểm được giữ `[0,100]`, round 2 decimals.
- Matrix và lookup phải permutation-independent và read-only.

---

## 7. Eligibility Gate

### 7.1. API

```python
class EligibilityGate:
    def evaluate(
        self,
        candidate: CandidateSetup,
        context: StrategyContext,
        regime: MarketRegime,
        profile: StrategyProfile,
    ) -> StrategyEvaluation: ...

    def evaluate_registry_output(
        self,
        candidates_by_strategy: Mapping[str, tuple[CandidateSetup, ...]],
        context: StrategyContext,
        regime: MarketRegime,
        profiles: Mapping[str, StrategyProfile],
    ) -> tuple[StrategyEvaluation, ...]: ...
```

Gate stateless và không mutate candidate/context/profile/regime.

### 7.2. Preflight consistency

- `regime.bar_index == context.bar_index`.
- `regime.timestamp == context.bar_close_time`.
- Mapping key, `candidate.strategy_id` và `profile.strategy_id` phải giống nhau.
- Candidate direction thuộc `profile.allowed_directions`.
- Context timeframe thuộc `profile.timeframes`.
- Candidate/evidence không nằm trong tương lai:
  - `candidate.bar_index <= N`;
  - `candidate.timestamp <= context.bar_close_time`;
  - mọi `EvidenceRef.bar_index <= N`;
  - evidence time nếu có `<= context.bar_close_time`.
- Duplicate `setup_id` với payload khác là integrity error; không chọn một bản tùy ý.

Lỗi kiểu/schema/integrity raise exception. Điều kiện thị trường hợp lệ nhưng không đạt gate trả `REJECTED`, không raise.

### 7.3. Required evidence

Theo strategy:

- S01: `liquidity_sweep`, `structure_event`, `fair_value_gap`.
- S05: `structure_event`, `order_block`.
- S09: `liquidity_sweep`, `structure_event`, `fair_value_gap`, cùng window metadata bắt buộc.

Thiếu loại bắt buộc → `missing_required_evidence`.

Không yêu cầu evidence phải còn nằm trong `context.active_*` tại signal bar vì tracker có thể đã chuyển lifecycle sau touch. Candidate evidence immutable là source audit. Gate không được làm sống lại hoặc tự suy diễn lifecycle mà strategy không cung cấp.

### 7.4. Event ordering

Gate kiểm tra những quan hệ có thể chứng minh từ Candidate:

- Mọi evidence bar index `<= candidate.bar_index <= N`.
- S01/S09: sweep bar `<=` FVG middle bar `<` MSS bar `<` signal bar.
- S05: OB source candle có thể trước BOS; dùng details/source fields đã ghi trong evidence để xác minh `OB created/source event <= signal bar`, không so OB candle index như created time.
- Nếu dữ liệu bắt buộc để chứng minh ordering bị thiếu, dùng `missing_required_evidence`; nếu có nhưng sai dùng `invalid_event_order`.

Không thay đổi same-bar semantics đã khóa trong strategy templates.

### 7.5. HTF bias gate

- Missing bias: hard reject `missing_required_evidence`.
- Opposed bias: cả S01/S05/S09 reject `htf_bias_mismatch`.
- Neutral bias:
  - S05 reject `htf_bias_mismatch`.
  - S01/S09 allow.
- Aligned bias: allow.

### 7.6. Expiry, RR và strategy-aware checks

- Nếu `N > candidate.expiry_bar` → `expired_setup`.
- Nếu `candidate.planned_rr < max(1.5, profile.min_rr)` → `insufficient_rr`.
- Candidate geometry không được tính lại theo giá N+1 trong T53.7; model đã validate planned geometry.
- S01: sweep quá 20 bar tại candidate signal → `stale_liquidity_sweep`.
- S09: sweep age do WindowKey/grace của S09 quản lý; không áp hard cap 20 làm hỏng setup cuối grace. Gate kiểm tra window metadata và signal close-time không sau grace; sai → `outside_session` hoặc `expired_setup` theo trường hợp.
- S05 không yêu cầu liquidity sweep.
- `invalid_order_block`, `invalid_fvg`, `fvg_invalidated_by_close`, `opposite_structure_shift` chỉ được dùng khi Candidate evidence/details đủ dữ kiện xác định; không phỏng đoán từ object đã biến mất khỏi active collection.
- `cooldown_active`, `insufficient_rr_at_fill`, `geometry_violation_at_fill` thuộc T53.9 và không được emit tại đây.
- `insufficient_score`, `conflicting_direction` thuộc T53.8 final decision và không được biến candidate thành REJECTED tại đây.

### 7.7. Reason aggregation

- Không fail-fast sau lỗi thị trường đầu tiên; thu thập mọi rejection reason có thể chứng minh.
- Deduplicate reason.
- Sort theo canonical order của 18 reason codes trong `T53_WAVE1_SEMANTICS.md`.
- Không tạo reason text tự do thay cho code chuẩn.
- Không có reason → `ELIGIBLE`.
- Có ít nhất một reason → `REJECTED`.
- `details` lưu regime, matrix score, bias relation, evidence kinds, age/RR boundaries và gate version.

---

## 8. Evidence Deduplication

### 8.1. Canonical identity

Identity chính:

```text
(candidate.direction, candidate.evidence_cluster_id)
```

Không fuzzy-match bằng giá làm tròn, timestamp gần nhau hoặc object identity.

Hai candidate khác cluster ID chỉ được coi là cùng opportunity khi đồng thời có:

1. Cùng direction.
2. Cùng non-null structure leg lấy từ canonical evidence details.
3. Cùng zone evidence ID (`fair_value_gap` hoặc `order_block`).

Chỉ chung sweep hoặc chung structure event nhưng khác entry zone là overlap telemetry, không đủ để merge. Quy tắc này tránh transitive over-clustering.

S01 và S09 cùng MSS/FVG phải merge nhờ shared `evidence_cluster_id`. S05 dùng OB khác zone nên không tự động merge với FVG setup chỉ vì chung BOS.

### 8.2. Frozen output models

```python
@dataclass(frozen=True)
class EvidenceCluster:
    cluster_id: str
    direction: Literal["BUY", "SELL"]
    members: tuple[StrategyEvaluation, ...]
    strategy_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    overlap_evidence_ids: tuple[str, ...]
    meta: Mapping[str, Any]

@dataclass(frozen=True)
class DirectionConflict:
    bar_index: int
    buy_cluster_ids: tuple[str, ...]
    sell_cluster_ids: tuple[str, ...]
    reason: str = "conflicting_direction"

@dataclass(frozen=True)
class ConfluenceBatch:
    bar_index: int
    timestamp: pd.Timestamp
    regime: MarketRegime
    evaluations: tuple[StrategyEvaluation, ...]
    eligible_clusters: tuple[EvidenceCluster, ...]
    direction_conflict: DirectionConflict | None
    meta: Mapping[str, Any]
```

Các model phải strict, deeply immutable, exact JSON round-trip và reject unknown/malformed payload nếu convention của domain models hiện tại yêu cầu.

### 8.3. Membership và ordering

- Chỉ `ELIGIBLE` evaluations được đưa vào eligible clusters.
- `REJECTED` evaluations vẫn giữ nguyên trong `ConfluenceBatch.evaluations` để audit.
- Member sort canonical `(strategy_id, candidate.setup_id)`.
- `strategy_ids` unique/sorted.
- `evidence_ids` là union unique/sorted; không cộng trùng evidence.
- Cluster sort `(direction, cluster_id)`.
- Input mapping/list/set order không được ảnh hưởng serialized output.
- Exact duplicate cùng setup ID và cùng payload collapse idempotently.
- Cùng setup ID hoặc cluster ID nhưng payload/direction không tương thích → raise integrity error trước output; không merge âm thầm.

### 8.4. Chưa chọn owner

T53.7 không gán `primary_owner`, vì ADR yêu cầu owner là strategy có total score cao nhất và total score chỉ tồn tại ở T53.8. T53.7 chỉ giữ toàn bộ member theo canonical order. T53.8 sẽ chọn primary/supporting IDs sau scoring.

---

## 9. Direction Conflict Contract

- Conflict tồn tại khi sau eligibility/dedup có ít nhất một BUY cluster và ít nhất một SELL cluster tại cùng bar N.
- T53.7 tạo `DirectionConflict` deterministic với toàn bộ cluster IDs hai phía.
- Không đánh dấu các evaluation là `REJECTED` chỉ vì conflict.
- Không tự triệt tiêu hai phía.
- Không chọn bên có nhiều strategy hơn; số strategy không phải phiếu bầu độc lập.
- T53.8 tính best directional score và áp:

```text
score_gap >= 15 → chọn phía thắng
score_gap < 15  → NO_TRADE / conflicting_direction
```

Tách như trên bảo đảm T53.7 không phụ thuộc scoring chưa triển khai.

---

## 10. Atomicity, determinism và error policy

- Classifier là component stateful duy nhất của T53.7.
- Gate, deduplicator và conflict detector phải stateless/pure.
- Mọi collection output là tuple hoặc read-only mapping.
- Không phụ thuộc dict/set/hash iteration order.
- Không dùng Python `hash()` trong ID.
- Không swallow exception hoặc trả partial result.
- Nếu classifier lỗi, state trước call phải giữ nguyên.
- Nếu gate/dedup lỗi, registry/strategy/classifier state đã có không được mutate lại.
- JSON replay phải cho exact full-payload parity.

---

## 11. Test matrix tối thiểu

### A. Config và model contracts — 14 tests

1. Defaults exact.
2. JSON round-trip.
3. Reject bool-as-int/float.
4. Reject NaN/Inf.
5. Reject invalid lookback/threshold/mode.
6. Unknown config field reject.
7. EvidenceCluster deep immutability.
8. DirectionConflict validation.
9. ConfluenceBatch cross-field validation.
10. Duplicate strategy/setup IDs reject đúng contract.
11. Exact JSON round-trip ba models.
12. NumPy scalar normalization nếu được hỗ trợ.
13. Clean package exports.
14. Fresh-process import.

### B. Regime warm-up và math — 20 tests

15. 19 closes → uncertain warm-up.
16. 20 closes nhưng 99 ATR → uncertain.
17. Đủ đúng 20/100 pass warm-up.
18. Bar index lớn không thay thế finite count.
19. Flat close denominator → ER 0.
20. Monotonic close → ER 1.
21. Zigzag ER exact.
22. ER threshold 0.30 inclusive.
23. ATR equal values → percentile 50.
24. ATR percentile 60 inclusive.
25. Trailing-only ATR, future append invariant.
26. BOS rolling boundary inclusive.
27. Old BOS outside lookback excluded.
28. Rolling-buffer repeated BOS counted once.
29. CHoCH không tính BOS.
30. Wrong structure mode ignored.
31. Sweep age 20 inclusive.
32. Sweep age 21 excluded.
33. Invalid/future sweep rejected hoặc raises qua as-of guard.
34. Metrics payload exact và JSON-safe.

### C. Five-regime tree — 14 tests

35. Bullish trend happy path.
36. Bearish trend mirror.
37. Bull trend requires no bearish BOS.
38. Bear trend requires no bullish BOS.
39. Trend requires ER >=0.30.
40. Volatile reversal happy path.
41. Volatile reversal requires sweep.
42. Volatile reversal requires ATR percentile >=60.
43. Ranging happy path.
44. Ranging rejects BOS presence.
45. Ranging threshold boundaries.
46. Uncertain conflicting BOS.
47. Priority trend trước volatile reversal.
48. Missing/neutral HTF behavior deterministic.

### D. Classifier lifecycle — 12 tests

49. Incremental output length/parity.
50. Batch helper parity.
51. JSON context replay parity.
52. Same-bar identical retry returns cached object.
53. Same-bar conflict raises.
54. Gap raises.
55. Backward raises.
56. Timestamp non-increasing raises.
57. Fault injection rollback buffers/cache.
58. Reset equals fresh instance.
59. Long stream bounded state.
60. Input context immutable.

### E. Matrix và Eligibility Gate — 30+ tests

61–90. Mỗi ô trong matrix có test exact score/ALLOW-REJECT.
91. Unknown strategy fails closed/raises config error.
92. Regime/context bar mismatch.
93. Regime/context timestamp mismatch.
94. Mapping/profile/candidate strategy mismatch.
95. Direction/profile mismatch.
96. Timeframe mismatch.
97. Candidate future bar/time reject integrity.
98. Future evidence bar/time reject integrity.
99. S01 required evidence.
100. S05 required evidence.
101. S09 required evidence/window metadata.
102. S01/S09 event ordering valid.
103. Invalid ordering reason.
104. Missing HTF bias.
105. Opposed bias all strategies.
106. Neutral S05 reject.
107. Neutral S01/S09 allow.
108. Expiry exact boundary pass.
109. Bar after expiry reject.
110. Planned RR exact 1.5 pass.
111. RR below gate reject.
112. S01 stale sweep 20/21.
113. S09 long window/grace sweep không bị global stale-20 reject.
114. Multiple reasons aggregate canonical order.
115. Eligible fields and zeroed future scoring components.
116. Gate input permutation invariant.

### F. Dedup và conflict — 20 tests

117. One candidate → one cluster.
118. S01/S09 same cluster merge.
119. Shared evidence union unique.
120. Supporting strategy list unique/sorted.
121. Same strategy exact duplicate idempotent collapse.
122. Same setup ID/different payload integrity error.
123. Same cluster ID/opposite direction integrity error.
124. Shared sweep only, different zone → separate cluster.
125. Shared structure only, different zone → separate cluster.
126. Same leg+same zone alias IDs merge.
127. S05 OB và S01 FVG chung BOS vẫn tách cluster.
128. Rejected evaluation không vào eligible cluster.
129. Rejected evaluation vẫn còn trong audit.
130. BUY-only → no conflict.
131. SELL-only → no conflict.
132. BUY+SELL → DirectionConflict.
133. Conflict không đổi evaluation status.
134. Không dùng strategy count như vote.
135. Input permutation full-payload invariant.
136. JSON round-trip ConfluenceBatch.

### G. Production integration/QC — tối thiểu 10 tests

137. Registry S01/S05/S09 output đi qua gate.
138. Production ContextBuilder → classifier.
139. Production S01/S09 shared opportunity dedup thành một cluster.
140. Production S05 distinct OB opportunity giữ cluster riêng.
141. Full batch/incremental parity.
142. Full JSON replay parity.
143. Append future bars không đổi prefix regime/evaluation/clusters.
144. Fault injection không để partial ConfluenceBatch.
145. 10.000 bars: state bounded, output deterministic; timing chỉ báo cáo.
146. Existing full regression không lỗi.

Test count có thể tăng khi implementation phát hiện boundary mới; không được giảm coverage bằng cách gộp assertion hời hợt.

---

## 12. Benchmark policy

- Đo riêng classifier, gate và dedup trên 10.000 bar/candidate workload sau một warm-up, ba lần lặp.
- Ghi median và worst run vào walkthrough.
- Correctness gate bắt buộc: bounded state, không quadratic theo tổng stream, deterministic replay.
- Wall-clock là chỉ báo kỹ thuật, không tạo flaky default test. Có thể dùng opt-in benchmark theo ADR 19.
- Không thay workload, skip correctness hoặc giảm số candidate để tạo số đẹp.

---

## 13. Trình tự triển khai

### T53.7.0 — Gate A: khóa semantics

- QC plan này.
- Accept ADR 23 sau khi người dùng phê duyệt.
- Chưa code trước Gate A.

### T53.7.1 — Regime classifier

- Config, exact math, 5-state tree.
- Stateful atomic lifecycle và batch helper.
- Hoàn tất nhóm test A–D liên quan trước khi đi tiếp.

### T53.7.2 — Eligibility Gate

- 30-cell matrix.
- Strategy-aware hard gates và reason aggregation.
- Hoàn tất toàn bộ matrix/gate tests.

### T53.7.3 — Dedup & conflict detector

- Frozen result models.
- Exact/alias clustering, collision detection và canonical ordering.
- DirectionConflict chỉ mô tả, chưa chọn winner.

### T53.7.4 — Integration, exports và docs

- Ghép Registry output với T53.7 components trong test/integration helper.
- Batch/incremental/JSON/future-append parity.
- Full regression, compileall, diff-check, benchmark report.
- Chuyển `review`, không tự chuyển `done`.

Không chuyển subtask sau sang doing trước khi subtask hiện tại pass targeted tests.

---

## 14. Lệnh xác minh bắt buộc

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_smc_engine_regime -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_engine_eligibility -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_engine_confluence -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_engine_t53_7_integration -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_strategy_s01 tests.test_smc_strategy_s05 tests.test_smc_strategy_s09 tests.test_smc_engine_registry tests.test_smc_engine_context_qc -v
.\.venv\Scripts\python.exe -m unittest discover tests -v
.\.venv\Scripts\python.exe -m compileall -q server.py engine smc tests
git diff --check
```

Nếu có thay đổi frontend ngoài dự kiến thì phải giải thích scope; mặc định T53.7 không cần sửa frontend/Node.

---

## 15. Acceptance criteria cuối

- [ ] Regime classifier đúng warm-up 20 closes + 100 finite ATR values.
- [ ] ER denominator-zero và ATR tie-percentile có kết quả định nghĩa rõ.
- [ ] Năm regime mutually exclusive theo priority đã khóa.
- [ ] Không centered/full-sample lookahead; future append prefix invariant.
- [ ] 30/30 matrix cells có exact tests.
- [ ] HTF policy S01/S05/S09 thống nhất ADR 16.
- [ ] Gate aggregate reason canonical, không score cứu hard failure.
- [ ] T53.7 không emit fill-time/cooldown/final-score reasons sai tầng.
- [ ] S01/S09 cùng opportunity dedup thành một cluster.
- [ ] Khác zone không over-merge chỉ vì chung sweep/BOS.
- [ ] Evidence union không double-count.
- [ ] BUY/SELL conflict được mô tả đầy đủ nhưng chưa chọn winner.
- [ ] Không chọn primary/supporting strategy trước T53.8.
- [ ] Atomicity, idempotency, reset và bounded state pass.
- [ ] Batch/incremental/JSON/future-append full-payload parity pass.
- [ ] Existing S01/S05/S09/Registry/Context regressions pass.
- [ ] Full suite, `compileall` và `git diff --check` pass.
- [ ] Walkthrough ghi đúng số tests/skips và benchmark thực tế.
- [ ] T53.7 ở `review`, T53.8 vẫn `planned` khi bàn giao QC.

---

## 16. Gate phê duyệt

Plan này đề xuất ADR 23 với ba quyết định cần được QC trước implementation:

1. Regime V1 dùng swing BOS, trailing-only mid-rank ATR percentile và priority trend → volatile reversal → ranging → uncertain.
2. Dedup dùng direction + canonical cluster/leg+zone; chỉ chung sweep/BOS không đủ merge.
3. T53.7 chỉ phát hiện conflict và giữ member; owner/score-gap/final `NO_TRADE` thuộc T53.8.

Chỉ bắt đầu T53.7.1 sau khi Gate A được phê duyệt.
