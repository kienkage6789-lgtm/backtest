# T53.5 — Kế hoạch triển khai S05 BOS → Order Block First Retest

> Phiên bản: 1.0 — 2026-09-10  
> Trạng thái: READY FOR QC — chỉ là kế hoạch, chưa triển khai production code  
> Gate: C-S05 thuộc Wave 1  
> Phụ thuộc đã hoàn thành: T53.1 Domain Models, T53.2 StrategyContext, T53.3 Protocol/Registry, T53.4 S01  
> Nguồn chuẩn: ADR 16, ADR 17, ADR 19, ADR 20, `T53_WAVE1_SEMANTICS.md`, `SMC_MULTI_STRATEGY_IMPLEMENTATION_PLAN.md`

---

## 1. Mục tiêu

Triển khai strategy template `S05` cho mô hình continuation:

```text
HTF bias aligned
→ BOS cùng hướng được xác nhận
→ Order Block liên kết đúng BOS/structure leg
→ không có opposite structure shift
→ OB được chạm lần đầu nhưng vẫn valid sau khi nến đóng
→ phát CandidateSetup tại closed bar N
```

S05 chỉ phát ứng viên giao dịch. Nó không khớp lệnh, không chấm regime, không chọn winner giữa nhiều strategy và không sửa detector.

Mục tiêu chất lượng:

- Zero-lookahead tuyệt đối.
- Không dùng trạng thái cuối batch để quyết định quá khứ.
- Batch, incremental và JSON replay cho output giống nhau hoàn toàn.
- Long/Short dùng cùng một thuật toán direction-neutral.
- State hữu hạn, deterministic, reset được và atomic khi có exception.
- Không tạo lệnh trùng cho cùng BOS/OB cluster.

---

## 2. Ranh giới trách nhiệm

### 2.1. Trong phạm vi T53.5

- `S05Config` bất biến, validate kiểu nghiêm ngặt và JSON round-trip exact.
- `S05BOSOBRetestStrategy` tuân thủ `StrategyTemplate`.
- Multi-narrative state theo từng BOS.
- Ghép BOS với OB bằng exact source-event hoặc same-leg fallback có kiểm soát.
- HTF bias aligned tại lúc nhận BOS và tại signal bar.
- First-retest airtight boundary dựa trên snapshot sau detector update.
- Expiry 25 bar inclusive.
- Invalidation bởi opposite BOS/CHoCH, bias mất alignment và OB mất hiệu lực.
- Entry proximal/CE50, SL tại OB distal ± buffer, target pool/fixed RR.
- Stable Evidence/Cluster/Setup IDs và audit metadata.
- Exports package, registry compatibility, parity và regression tests.

### 2.2. Ngoài phạm vi

- Fill tại Open N+1, spread, commission, slippage và cash-basis RR: T53.9.
- Cooldown sau khi vị thế thực sự mở: T53.9.
- Market regime, reason-code gate, scoring và conflict: T53.7–T53.8.
- Dedup liên chiến lược S05/S08: T53.7.
- Sửa `OrderBlockTracker`, BOS detector hoặc `StrategyContextBuilder`, trừ khi probe chứng minh contract đầu vào sai; khi đó phải tách task/QC riêng.
- Thêm SessionRange, premium/discount engine hoặc entry limit-order intrabar.
- Tối ưu benchmark context đã defer theo ADR 19.

### 2.3. Thứ tự ưu tiên tài liệu

Nếu tài liệu có câu chữ khác nhau, áp dụng thứ tự:

1. ADR đã ACCEPTED trong `.agent/DECISIONS.md`.
2. Contract model/protocol đang chạy và test hiện tại.
3. Kế hoạch T53.5 này.
4. `T53_WAVE1_SEMANTICS.md` và master plan.
5. Strategy catalog mang tính nghiên cứu.

Không thay đổi semantics đã ACCEPTED chỉ để làm test dễ hơn.

---

## 3. Các quyết định implementation được khóa trong plan

### 3.1. Closed-bar và availability

- `evaluate(context)` chỉ nhận `StrategyContext` của closed bar `N`.
- Chỉ dùng evidence có timestamp/index/lifecycle không vượt `N` và `bar_close_time`.
- BOS chỉ khả dụng khi `event.index <= N`; event phải là `event_type == "BOS"`.
- OB chỉ khả dụng khi `effective_created_at <= N`, với:

```python
effective_created_at = (
    ob.created_at if ob.created_at != -1 else ob.source_event_index
)
```

- Candidate sinh tại bar `N`; `available_from_bar = N + 1` chỉ ghi metadata. S05 không tự fill.

### 3.2. Direction mapping

| BOS/OB direction | Candidate | HTF bias bắt buộc | Opposite shift |
|---|---|---|---|
| `bullish` | `BUY` | `bullish` | `bearish` BOS/CHoCH |
| `bearish` | `SELL` | `bearish` | `bullish` BOS/CHoCH |

Không được tạo hai nhánh thuật toán copy-paste. Dùng helper mapping direction/boundary.

### 3.3. BOS contract

BOS hợp lệ khi đồng thời:

- `event_type == "BOS"`.
- `event.mode == config.mode`.
- Direction hợp lệ và aligned với `context.htf_bias.bias` tại bar BOS được ingest.
- `break_type == "close"`; wick-only break bị loại.
- `displacement is True` khi `require_displacement=True`.
- `event.index == N` khi narrative mới được ingest, tránh hồi tố BOS cũ từ rolling buffer.
- Không có future field trong HTF bias hoặc evidence.

Mặc định `require_displacement=True`. Có thể tắt bằng config để nghiên cứu, nhưng default production giữ BOS chất lượng cao.

### 3.4. HTF bias lifecycle

S05 bắt buộc bias aligned:

- Lúc BOS được nhận: thiếu bias, neutral hoặc opposed → không tạo narrative.
- Sau khi đã tạo narrative: nếu bias tại closed bar hiện tại không còn aligned, narrative chuyển terminal `INVALIDATED` với internal reason `htf_bias_mismatch`.
- Bias trở lại aligned ở bar sau không hồi sinh narrative cũ; cần BOS mới.
- `as_of`, `source_event_time`, `timestamp` của bias không được vượt `context.bar_close_time`; vi phạm raise `StrategyStateError` trước mọi mutation.

Quy tắc này loại bỏ việc giữ tín hiệu continuation xuyên qua giai đoạn HTF thesis đã mất hiệu lực.

### 3.5. BOS ↔ OB linkage

Một cặp `(bos, ob)` hợp lệ khi:

- `ob.direction == bos.direction`.
- `ob.mode == bos.mode == config.mode`.
- `ob.origin_type == "BOS"` hoặc `ob.source_event_type == "BOS"`.
- `effective_created_at >= bos.index` và `effective_created_at <= N`.
- Quan hệ nguồn thỏa một trong hai:
  1. Exact source match: `ob.source_event_index == bos.index`.
  2. Same-leg fallback: cả hai `structure_leg_id` khác `None` và bằng nhau.

Quy tắc ưu tiên:

- Exact source match luôn xếp trước same-leg fallback.
- Same-leg fallback chỉ dùng khi không có exact-match OB hợp lệ cho BOS đó.
- Nếu OB có `source_event_index` trỏ rõ sang một BOS khác và leg cũng khác, reject.
- Không coi hai giá trị `structure_leg_id=None` là cùng leg.

Canonical rank cho OB của một BOS:

```text
1. exact source match trước fallback
2. effective_created_at nhỏ hơn trước
3. quality: premium_candidate > strong > base
4. ob.index lớn hơn trước (source candle gần BOS hơn)
5. ob.low tăng dần
6. ob.high tăng dần
7. source_event_index tăng dần
```

Chỉ chọn một OB canonical cho mỗi BOS narrative. Một OB có thể khớp nhiều BOS qua same-leg fallback; ownership được khóa bằng pair rank toàn cục để một OB không phát nhiều candidate cùng bar:

```text
1. exact source-event pair trước same-leg fallback pair
2. bos.index lớn hơn trước
3. bos.broken_swing_index lớn hơn trước
4. BOSKey lexicographical
```

### 3.6. OB quality

- Quality order: `base < strong < premium_candidate`.
- `min_ob_quality` là config, mặc định `"base"` để không thay đổi coverage của detector hiện tại.
- Quality chỉ là filter tối thiểu trong S05; điểm số 60/75/90 thuộc T53.8.
- Không tự suy ra quality lại từ FVG trong strategy.

### 3.7. First-retest airtight boundary

Nến `N` là retest hợp lệ chỉ khi snapshot OB sau tracker update thỏa đủ:

```python
ob.mitigated_at == N
and ob.retest_count == 1
and ob.valid is True
and ob.invalidated_at is None
```

Ngoài ra:

- `N > effective_created_at`; không cho tạo candidate ngay bar OB trở nên available.
- `N <= expiry_bar`.
- Bias vẫn aligned.
- Không có opposite BOS/CHoCH trong closed interval từ BOS đến bar retest.
- Không được thay bốn điều kiện trên bằng phép kiểm tra OHLC thủ công.
- Nếu candle vừa chạm vừa close-break khiến OB invalid tại N, `active_obs` sẽ không chứa OB đó hoặc snapshot có `valid=False`; cả hai trường hợp đều không emit.
- OB có `retest_count > 1`, `mitigated_at < N`, invalid hoặc missing đều không hợp lệ.

### 3.8. Opposite structure invalidation

- Cả opposite `BOS` và opposite `CHoCH` đều invalid theo ADR 16 general contract.
- Khoảng kiểm tra là closed interval:

```text
bos.index <= opposite_event.index <= N
```

- Không tính chính BOS nguồn vì direction giống narrative.
- Opposite event tại đúng bar BOS làm bar đó ambiguous và ngăn tạo narrative.
- Opposite event tại đúng bar retest được xử lý trước candidate emission.
- Event cùng direction không invalid.
- Event mode khác không ảnh hưởng.

### 3.9. Expiry

- `max_ob_age_bars=25`.
- `expiry_bar = effective_created_at + max_ob_age_bars`.
- Boundary inclusive: retest tại `N == expiry_bar` hợp lệ.
- Tại `N == expiry_bar + 1`, narrative terminal `EXPIRED` trước matching.
- Nếu OB được tạo muộn hợp lệ, tuổi tính từ lúc OB thật sự available, không từ source candle `ob.index`.

### 3.10. Entry, SL, target và RR

Entry:

- `entry_level="proximal"` mặc định:
  - BUY: `ob.high`.
  - SELL: `ob.low`.
- `entry_level="ce_50"`:
  - `(ob.high + ob.low) / 2` cho cả hai direction.

Stop loss:

- BUY: `ob.low - sl_buffer_price`.
- SELL: `ob.high + sl_buffer_price`.
- Default buffer: `0.20` price unit.

Target:

1. Lọc active opposing liquidity pools còn valid, chưa swept, đúng mode và `confirmed_at <= N`.
2. BUY nhận `equal_highs`/`swing_high` với `price > entry`.
3. SELL nhận `equal_lows`/`swing_low` với `price < entry`.
4. Chọn pool gần entry nhất; tie-break `(distance, confirmed_at, kind, sorted(indices))`.
5. Nếu target pool sai geometry hoặc planned RR sau rounding `< min_rr`, dùng fixed-RR fallback.
6. Không thử pool xa hơn sau khi nearest pool fail; giữ parity với S01 và global Wave 1 contract.
7. Không dùng “đỉnh/đáy cú BOS” làm target riêng trong Wave 1 vì model hiện không có canonical BOS impulse-extreme field; `broken_swing_price` không được suy diễn thành target nếu không có ADR mới.

Fallback:

- BUY: `entry + fallback_rr * (entry - stop)`.
- SELL: `entry - fallback_rr * (stop - entry)`.
- Default `fallback_rr=2.0` và phải `fallback_rr >= min_rr`.

Precision:

- Làm tròn entry/SL/TP 3 chữ số trước geometry check.
- BUY: `SL < Entry < TP`; SELL: `TP < Entry < SL`.
- `planned_rr` tính lại từ mức giá đã làm tròn, rồi round 2 chữ số.
- Geometry collapse, risk `<=0`, NaN/Inf hoặc RR dưới min → không tạo candidate.
- Cash-basis RR tại fill không thuộc S05; T53.9 revalidate.

---

## 4. Public API dự kiến

### 4.1. File

File mới:

- `smc/engine/strategies/s05_bos_ob_retest.py`
- `tests/test_smc_strategy_s05.py`

File cập nhật:

- `smc/engine/strategies/__init__.py`
- `smc/engine/__init__.py`
- `.agent/TASKS.md`
- `.agent/CHANGELOG.md`
- `walkthrough.md` sau implementation/QC

Không sửa `smc/engine/models.py` nếu các model hiện tại đã đủ contract.

### 4.2. `S05Config`

```python
@dataclass(frozen=True)
class S05Config:
    mode: str = "internal"
    max_ob_age_bars: int = 25
    entry_level: str = "proximal"
    sl_buffer_price: float = 0.20
    min_rr: float = 1.50
    fallback_rr: float = 2.00
    require_displacement: bool = True
    min_ob_quality: str = "base"
```

Validation:

- Không ép kiểu ngầm.
- Reject `bool` trong trường số.
- Reject string/số trong trường bool.
- `mode in {"internal", "swing"}`.
- `max_ob_age_bars > 0`.
- `entry_level in {"proximal", "ce_50"}`.
- `sl_buffer_price > 0`, finite.
- `min_rr > 0`, finite.
- `fallback_rr >= min_rr`, finite.
- `min_ob_quality in {"base", "strong", "premium_candidate"}`.
- `to_dict()` JSON-safe; `from_dict()` strict unknown/missing/type rejection; exact JSON round-trip.

### 4.3. `S05BOSOBRetestStrategy`

```python
class S05BOSOBRetestStrategy:
    strategy_id = "S05"

    @property
    def config(self) -> S05Config: ...

    @property
    def profile(self) -> StrategyProfile: ...

    def evaluate(
        self,
        context: StrategyContext,
    ) -> tuple[CandidateSetup, ...]: ...

    def reset(self) -> None: ...
```

Profile:

```text
strategy_id: S05
name: BOS Order Block First Retest
version: 1.0.0
style: continuation
allowed_directions: BUY, SELL
timeframes: M1, M5, M15
max_setup_age_bars: config.max_ob_age_bars
cooldown_bars: 3
min_rr: config.min_rr
params: config.to_dict()
```

---

## 5. State model nội bộ

### 5.1. Stages

```python
class S05NarrativeStage(str, Enum):
    BOS_SEEN = "BOS_SEEN"
    OB_READY = "OB_READY"
    EMITTED = "EMITTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
```

`BIAS_ALIGNED` không cần stage riêng: đó là admission guard khi BOS đến và invariant khi narrative còn active.

### 5.2. Stable keys

```python
BOSKey = tuple[
    str,  # mode
    str,  # direction
    int,  # bos.index
    int,  # broken_swing_index
    str | None,  # structure_leg_id
]

OBKey = tuple[
    str,  # mode
    str,  # direction
    int,  # source_event_index
    int,  # source candle index
]
```

Không dùng Python object identity, `id()`, process hash hoặc float làm identity.

### 5.3. Narrative record

```python
@dataclass
class S05Narrative:
    bos_key: BOSKey
    direction: Literal["BUY", "SELL"]
    bos: StructureEventSnapshot
    stage: S05NarrativeStage
    ob: OrderBlockSnapshot | None = None
    ob_key: OBKey | None = None
    ready_at: int | None = None
    expiry_bar: int | None = None
    terminal_reason: str | None = None
```

### 5.4. Bounded state

- `_narratives: dict[BOSKey, S05Narrative]` chỉ giữ `BOS_SEEN` và `OB_READY` chưa quá hạn.
- `_emitted_clusters: dict[str, int]` lưu `cluster_id -> expiry_bar` và prune khi `N > expiry_bar`.
- BOS narrative không có OB cũng hết hạn tại `bos.index + max_ob_age_bars` để không tăng vô hạn.
- Từ bounded context + age 25, state phải O(1) theo chiều dài stream.
- `reset()` xóa narratives, emitted clusters và toàn bộ last-bar cache.

---

## 6. Pipeline `evaluate()` tại mỗi bar N

Thứ tự bắt buộc:

1. Validate `context` type.
2. Monotonic/idempotent guard:
   - first bar được chấp nhận;
   - same bar + exact payload trả cache;
   - same bar + payload khác raise;
   - backward hoặc gap `>1` raise;
   - bar tăng nhưng timestamp không tăng raise.
3. Zero-future-leak validation cho structures, OBs, pools và HTF bias.
4. Tạo working copy của narratives và emitted-cluster map; prune chỉ trên working copy.
5. Ingest BOS mới tại đúng bar `N`, chỉ khi bias aligned và BOS contract pass.
6. Reject ambiguous same-bar BOS nếu có opposite BOS/CHoCH cùng mode tại N.
7. Với từng active narrative theo canonical order:
   - kiểm tra expiry;
   - kiểm tra bias còn aligned;
   - kiểm tra opposite shift đến N inclusive;
   - nếu chưa có OB, tìm canonical OB pair;
   - nếu đã có OB, tìm snapshot mới nhất cùng OB identity để refresh lifecycle;
   - nếu OB biến mất: chưa kết luận invalid chỉ vì context bounded, nhưng không emit; narrative tiếp tục đến expiry trừ khi có explicit invalidation/opposite/bias change;
   - nếu snapshot explicit invalid: terminal;
   - kiểm tra exact first-retest four-field contract;
   - build candidate.
8. Global same-OB ownership: nếu nhiều narratives muốn emit cùng OB, chỉ canonical winner được build; narratives còn lại terminal `duplicate_ob_ownership`.
9. Sort candidate `(direction, evidence_cluster_id, setup_id)`.
10. Chỉ khi toàn bar thành công mới commit narratives, emitted clusters và cache.

Mọi exception trước bước 10 phải giữ nguyên state cũ 100%.

---

## 7. Refresh lifecycle và tránh stale snapshot

Khi narrative đã gắn OB, không giữ clone cũ rồi dùng mãi. Mỗi bar:

- Tìm OB hiện tại theo `OBKey` trong `context.active_obs`.
- Nếu tìm thấy, thay snapshot trong working narrative trước khi đánh giá lifecycle.
- Candidate chỉ dùng snapshot của bar N có `mitigated_at == N`.
- Không được dùng `narrative.ob.valid` từ bar OB_READY để quyết định bar retest.
- Không scan detector internals; chỉ dùng immutable context.

Điểm này là regression bắt buộc vì OB tracker trước đây từng gặp stale-state bug.

---

## 8. Evidence, cluster và setup IDs

### 8.1. Evidence bắt buộc

Theo thứ tự canonical:

1. BOS `structure_event`.
2. Order Block `order_block`.
3. Optional opposing `liquidity_pool` nếu dùng target structural.

Không đưa HTF bias thành evidence độc lập; bias nằm trong metadata vì đây là context gate.

### 8.2. Safe ID components

```text
BOS evidence:
structure_event:{mode}:{bos.index}:BOS_{direction}_{broken_swing_index}

OB evidence:
order_block:{mode}:{effective_created_at}:ob_{direction}_{source_event_index}_{ob.index}

Pool evidence:
liquidity_pool:{mode}:{confirmed_at}:target_pool_{kind}_{sorted_indices}
```

Không đưa raw float vào ID.

### 8.3. Cluster

```python
leg_component = (
    f"leg-{mode}-{direction}-{bos.index}-{bos.broken_swing_index}"
)
zone_component = (
    f"ob-{mode}-{direction}-{ob.source_event_index}-{ob.index}"
)
cluster_id = make_cluster_id(direction, leg_component, zone_component)
setup_id = make_setup_id("S05", direction, N, cluster_id)
```

Actual `structure_leg_id` được lưu metadata/evidence details, không nhét trực tiếp vào ID vì detector có thể dùng dấu `:` không phù hợp base-token grammar.

### 8.4. Metadata tối thiểu

```text
signal_bar
available_from_bar
bos_key
ob_key
linkage_method: exact_source_event | same_leg_fallback
structure_leg_id
ready_at
expiry_bar
first_retest: true
ob_quality
mitigated_at
retest_count
entry_level
stop_anchor: ob_distal
sl_buffer_price
target_source: opposing_pool | fixed_rr
target_pool_key | null
fixed_rr | null
htf_bias
rr_basis: structural_pre_fill
```

---

## 9. Determinism và canonical ordering

- Evidence input permutation không đổi output.
- BOS sort key:

```text
(index, direction, broken_swing_index, structure_leg_id-or-empty)
```

- OB candidate rank theo mục 3.5.
- Pool rank giống S01.
- Candidate sort giống S01.
- Duplicate identical evidence được dedup.
- Hai payload khác nhau cùng identity phải fail fast ở context layer; strategy không âm thầm chọn một bản.
- Không dùng set iteration để quyết định winner.

---

## 10. Error và terminal semantics

Raise exception cho contract violation:

- Sai type context/config.
- Future evidence hoặc future HTF bias.
- Bar gap/backward/non-increasing timestamp.
- Same-bar payload conflict.
- Unsupported enum/value/NaN/Inf.

Trả tuple rỗng và terminal/prune cho market-condition rejection:

- Bias không aligned.
- Không có BOS/OB phù hợp.
- OB quality dưới minimum.
- Retest không phải lần đầu.
- OB invalid/close-break.
- Opposite shift.
- Expired.
- Geometry/RR không đạt.
- Duplicate cluster.

Reason code public cho rejection thuộc T53.7; T53.5 chỉ giữ `terminal_reason` nội bộ và candidate metadata.

---

## 11. Kế hoạch test chi tiết

Tạo `tests/test_smc_strategy_s05.py`. Mục tiêu tối thiểu 60 tests, đánh số ổn định.

### Nhóm A — Config, profile, protocol (1–8)

1. Default config đúng 25/0.20/1.5/2.0/proximal/internal.
2. Exact `to_dict/from_dict/json` parity.
3. Reject bool trong numeric fields.
4. Reject NaN/Inf/zero/negative.
5. Reject unknown mode, entry level, quality.
6. Reject fallback RR thấp hơn min RR.
7. Strategy/profile/protocol exact.
8. Clean package exports và fresh-process import.

### Nhóm B — Happy path và symmetry (9–15)

9. BUY happy path với bullish bias/BOS/OB first retest.
10. SELL mirror happy path.
11. Reflection symmetry qua trục giá: entry/SL/TP/RR đối xứng.
12. CE50 entry BUY.
13. CE50 entry SELL.
14. Structural pool target.
15. Fixed 2R fallback.

### Nhóm C — BOS validation (16–24)

16. Thiếu BOS không candidate.
17. CHoCH không được dùng thay BOS.
18. Direction BOS và bias mismatch.
19. Neutral/missing bias reject.
20. Mode mismatch reject.
21. Wick break reject.
22. Displacement false reject mặc định.
23. Displacement false pass khi config cho phép.
24. BOS rolling-buffer cũ không được ingest hồi tố.

### Nhóm D — BOS/OB linkage (25–35)

25. Exact source-event match pass.
26. Same-leg fallback pass khi không có exact match.
27. `None == None` không được coi là same-leg.
28. Wrong direction reject.
29. Wrong mode reject.
30. Wrong origin/source event type reject.
31. OB created trước BOS reject.
32. Future created_at raise.
33. Exact match ưu tiên fallback.
34. Multiple OB canonical tie-break deterministic.
35. Một OB không thuộc đồng thời hai BOS narratives.

### Nhóm E — First retest airtight boundary (36–47)

36. Exact four-field contract pass.
37. `mitigated_at < N` reject.
38. `mitigated_at > N` future leak raise.
39. `retest_count == 0` reject.
40. `retest_count > 1` reject.
41. `valid=False` reject.
42. `invalidated_at=N` reject.
43. Touch + close-break cùng bar không emit.
44. Retest tại bar created_at reject; bar sau pass.
45. Snapshot stale cũ không được dùng khi lifecycle bar N thay đổi.
46. OB bị thiếu khỏi bounded context không tạo candidate giả.
47. Duplicate OB snapshot không tạo duplicate candidate.

### Nhóm F — Expiry và invalidation (48–57)

48. Age 24 pass.
49. Age 25 inclusive pass.
50. Age 26 expired.
51. Opposite CHoCH sau BOS invalidates.
52. Opposite BOS sau BOS invalidates.
53. Opposite event đúng bar retest thắng retest.
54. Opposite event đúng bar BOS làm ambiguous/reject.
55. Same-direction event không invalid.
56. Different-mode opposite event không invalid.
57. Bias đổi neutral/opposed terminal; aligned trở lại không hồi sinh.

### Nhóm G — Entry, target, precision, IDs (58–69)

58. BUY distal SL = low - buffer.
59. SELL distal SL = high + buffer.
60. Nearest opposing pool selected independent of order.
61. Ineligible/swept/invalid/future/wrong-mode pools ignored.
62. Nearest pool RR thấp → fixed fallback, không nhảy sang pool xa.
63. Post-rounding geometry collapse reject.
64. Planned RR tính từ rounded levels.
65. Evidence order BOS→OB→Pool.
66. BOS evidence IDs injective.
67. OB evidence IDs injective và không dùng float.
68. Pool ID permutation invariant/collision-free qua production path.
69. Cluster/setup IDs deterministic và duplicate cluster bị chặn.

### Nhóm H — State, concurrency, atomicity (70–79)

70. Hai BOS độc lập được track đồng thời.
71. BUY và SELL narratives cùng tồn tại nhưng bias chỉ cho đúng phía.
72. Candidate cùng bar sort canonical.
73. Identical retry trả exact cached tuple.
74. Conflicting retry raise và không mutate.
75. Gap/backward timestamp raise và giữ state/cache.
76. Exception khi matching OB không commit.
77. Exception khi build candidate không commit cluster prune/narrative/cache.
78. Reset xóa toàn bộ state và replay giống hệt.
79. Long workload giữ narratives/emitted clusters bounded.

### Nhóm I — Integration và parity (80–87)

80. Strategy chạy qua `StrategyRegistry`.
81. Context builder thật tạo OB first-retest snapshot đúng signal bar.
82. Batch contexts → sequential strategy evaluation.
83. Incremental builder → strategy evaluation.
84. JSON serialize/deserialize context replay.
85. So sánh full `[candidate.to_dict()]` từng bar giữa ba mode.
86. Append future candles không đổi candidate lịch sử.
87. Input snapshots và context không bị mutate.

Các test số 81–86 phải dùng production `StrategyContextBuilder`; không được chỉ tạo DTO thủ công rồi gọi helper ID.

---

## 12. Probe QC độc lập bắt buộc

QC phải tự dựng probe ngoài logic test helper để kiểm tra ít nhất:

1. **Close-break same bar**: OB chạm tại N nhưng invalidated_at=N → zero candidate.
2. **Stale snapshot**: OB valid ở bar trước, invalid ở N → không dùng clone cũ.
3. **First retest only**: batch và incremental đều chỉ emit khi count=1.
4. **Same-leg collision**: hai BOS cùng leg, một OB → tối đa một candidate.
5. **Opposite event boundary**: opposite CHoCH đúng N chặn emission.
6. **Expiry boundary**: 24/25/26.
7. **Future leak**: created_at/mitigated_at/invalidated_at hoặc bias timestamp >N raise trước mutation.
8. **Atomicity**: injected exception giữ nguyên narratives/clusters/cache.
9. **Production-path IDs**: OB khác identity không collision; permutation không đổi output.
10. **Parity**: batch/incremental/JSON replay full payload exact.

Nếu tạo script probe tạm, không được dẫn nó trong walkthrough như artifact chính trừ khi script được commit trong repository. Regression quan trọng phải nằm trong `tests/test_smc_strategy_s05.py`.

---

## 13. Thứ tự triển khai

### T53.5.0 — Gate plan và baseline

- QC plan này với code/model thật.
- Chốt các implementation decisions: displacement default, quality default, source-event fallback, expiry anchor, target policy.
- Chạy baseline targeted/full tests trước khi sửa.

### T53.5.1 — Config, profile và exports

- Viết tests nhóm A trước.
- Tạo `S05Config`, strategy skeleton và exports.
- Chưa implement candidate logic.

### T53.5.2 — State, guards và BOS ingestion

- Monotonicity/idempotency/future-leak/atomic working state.
- BOS admission + bias policy + bounded narratives.
- Hoàn thành nhóm C và state guard tương ứng.

### T53.5.3 — OB matcher và lifecycle refresh

- Exact source/same-leg matching.
- Canonical ownership/ranking.
- Refresh snapshot mỗi bar, expiry và first-retest boundary.
- Hoàn thành nhóm D/E/F.

### T53.5.4 — Candidate builder

- Entry/SL/target/fallback/rounding/RR.
- Evidence, IDs, metadata và dedup.
- Hoàn thành nhóm B/G.

### T53.5.5 — Integration, parity và bounded-state tests

- Context builder thật, registry, JSON replay, future append.
- Atomic exception probes và long workload.
- Hoàn thành nhóm H/I.

### T53.5.6 — Review và Gate C-S05

- Chạy verification đầy đủ.
- Independent read-only QC tập trung P0/P1.
- Sửa phát hiện rồi QC lại.
- Cập nhật `.agent` và walkthrough bằng kết quả chạy thật.
- Chỉ đánh dấu `done` khi không còn P0/P1.

Không bắt đầu T53.6 trong cùng lượt implementation T53.5.

---

## 14. Verification bắt buộc

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_smc_strategy_s05 -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_strategy_s01 tests.test_smc_engine_registry tests.test_smc_engine_context_qc -v
.\.venv\Scripts\python.exe -m unittest discover tests -v
.\.venv\Scripts\python.exe -m compileall -q smc tests
node --test tests/test_drawings.test.js tests/test_ui_structure.test.js tests/test_drawer.test.js
git diff --check
```

Performance:

- Đo riêng S05 trên 10.000 contexts đã dựng sẵn để tách strategy cost khỏi context-builder cost.
- Không đặt hard wall-clock gate trong default suite nếu bị nhiễu theo ADR 19.
- Luôn kiểm tra state bound bằng assertion deterministic.
- Benchmark fail không được diễn giải thành correctness fail, nhưng phải báo đúng số đo.

---

## 15. Rủi ro và biện pháp chặn

| Rủi ro | Hậu quả | Biện pháp |
|---|---|---|
| Dùng OB state cũ | Emit trên OB đã invalid | Refresh snapshot theo OBKey mỗi bar |
| Chỉ kiểm `retest_count==1` | Candle close-break vẫn vào lệnh | Four-field airtight boundary |
| `None == None` same-leg | Ghép sai BOS/OB | Chỉ fallback khi hai leg ID khác None |
| Một OB ghép nhiều BOS | Duplicate candidate/risk | Canonical ownership + cluster dedup |
| Hồi tố BOS từ rolling buffer | Lookahead/parity lệch | Chỉ ingest event.index == N |
| Bias chỉ kiểm lúc signal | Giữ narrative sinh khi bias sai | Admission + lifecycle alignment |
| Opposite event cùng retest | Vào lệnh sau thesis invalid | Invalidation trước emission, interval inclusive |
| Expiry tính từ source candle | Sai tuổi khi OB available muộn | Anchor effective_created_at |
| Float trong IDs | Collision/platform drift | Chỉ dùng integer/string components |
| Test helper tự tạo ID | Không bắt regression production | Kiểm EvidenceRef từ candidate thật |
| Exception prune state | Retry không deterministic | Working copy + atomic commit test |
| Context cap làm OB biến mất | Suy diễn invalid sai | Missing means unavailable; không emit, không tự tạo state |

---

## 16. Definition of Done — Gate C-S05

T53.5 chỉ hoàn thành khi:

- [ ] `S05Config` strict, immutable và exact JSON parity.
- [ ] Strategy/profile tuân thủ protocol và import sạch.
- [ ] BOS chỉ được ingest đúng bar, đúng mode/direction/bias và không future leak.
- [ ] Exact source-event/same-leg fallback không ghép nhầm hoặc duplicate ownership.
- [ ] OB lifecycle được refresh theo context bar N, không dùng stale clone.
- [ ] First-retest four-field boundary và close-break same-bar pass.
- [ ] Expiry 25 inclusive và opposite BOS/CHoCH boundaries pass.
- [ ] Bias neutral/opposed không tạo hoặc duy trì S05 narrative.
- [ ] Entry/SL/target/fallback/RR đúng sau rounding.
- [ ] Evidence/cluster/setup IDs deterministic, injective và production-path tested.
- [ ] Long/Short symmetry pass.
- [ ] Atomicity, idempotency, reset và bounded state pass.
- [ ] Batch/incremental/JSON replay full serialized parity pass.
- [ ] Existing S01/context/registry regressions không hỏng.
- [ ] Full correctness suite không có lỗi mới; timing variance được báo riêng.
- [ ] Independent QC không còn P0/P1.
- [ ] `.agent/TASKS.md`, `.agent/CHANGELOG.md`, `walkthrough.md` phản ánh kết quả thật.

Sau Gate C-S05 mới chuyển sang T53.6 — S09 ICT Silver Bullet.
