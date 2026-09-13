# T53.6 — S09 ICT Silver Bullet Implementation Plan

> Trạng thái: **PLANNED — chờ QC plan trước khi code**  
> Phụ thuộc: T53.0, T53.1, T53.2, T53.3, T53.4, T53.5  
> Authority: ADR 16 [ACCEPTED], protocol/registry ADR 20, domain/context contracts hiện có  
> Phạm vi: chỉ strategy template S09; không fill lệnh, regime, selector hay backtest adapter

---

## 1. Mục tiêu

Triển khai strategy deterministic `S09` phát `CandidateSetup` khi chuỗi Silver Bullet hoàn chỉnh:

```text
Silver Bullet window mở
→ liquidity sweep được xác nhận trong window
→ MSS/BOS/CHoCH cùng hướng được xác nhận trong cùng window
→ displacement FVG cùng structure leg được xác nhận trong cùng window
→ giá retest FVG trước hoặc đúng window_end + 15 phút
→ emit tối đa một CandidateSetup cho window
```

Mục tiêu bắt buộc:

- Zero-lookahead tại mọi transition.
- DST-safe theo `America/New_York`.
- Window identity không va chạm giữa ngày/session.
- Long/Short đối xứng.
- Batch, incremental và JSON replay có full-payload parity.
- State atomic, bounded theo context/window retention và reset được.
- Cluster ID tương thích S01 để T53.7 deduplicate cùng sweep/MSS/FVG.

---

## 2. Phạm vi

### 2.1. Trong T53.6

- Tạo `S09Config` frozen, strict validation, exact JSON round-trip.
- Tạo ba Silver Bullet windows canonical theo giờ New York.
- Tạo state machine nhiều narrative, có ownership theo window.
- Kiểm tra sweep → MSS → FVG → retest.
- Xử lý grace 15 phút bằng `StrategyContext.bar_close_time`.
- HTF bias policy: aligned/neutral pass, opposed/missing fail closed.
- Entry, SL, target, RR, precision, evidence, IDs và metadata.
- Export S09 qua `smc.engine.strategies` và `smc.engine`.
- Unit, boundary, DST, integration, parity, atomicity và bounded-state tests.

### 2.2. Ngoài T53.6

- Không fill tại Open N+1, spread, commission, cash-RR hay cooldown; thuộc T53.9.
- Không regime scoring, eligibility reason aggregation, cross-strategy conflict hay selector; thuộc T53.7–T53.8.
- Không tạo Session Range/Asian High-Low detector.
- Không thêm range-break thay cho liquidity sweep.
- Không thay đổi S01/S05 semantics.
- Không sửa detector/context builder trừ khi có lỗi production được tách thành QC finding riêng.
- Không cho phép custom timezone trong Wave 1; canonical timezone là `America/New_York`.

---

## 3. Khảo sát contract thật

S09 phải dùng các API/model đã tồn tại:

- `StrategyContext.timestamp`: thời gian mở nến UTC đã normalize.
- `StrategyContext.bar_close_time`: thời gian đóng nến; là authority cho event confirmation và grace.
- `recent_sweeps`: `LiquiditySweepSnapshot`, có `index`, `time`, `confirmed_at`, `swept_at`, `direction`, `pool_kind`, `price_wick`, `pool_indices`, `mode`, `valid`.
- `recent_structures`: `StructureEventSnapshot`, có `index`, `time`, `event_type`, `direction`, `break_type`, `displacement`, `broken_swing_index`, `broken_swing_price`, `structure_leg_id`.
- `active_fvgs`: `FairValueGapSnapshot`, có `index`, `time`, `confirmed_at`, `filled_at`, `top`, `bottom`, `direction`, `mode`, `structure_leg_id`.
- `active_pools`: opposing target pool.
- `htf_bias`: `bullish`, `bearish`, `neutral` hoặc `None`.
- `SessionWindow`/session module đã có IANA timezone và DST, boundary `start <= t < end`.
- `CandidateSetup` bắt buộc price geometry sau rounding, evidence không trùng, `expiry_bar >= bar_index`.

Không được giả định `SessionDecision.session_name` luôn là Silver Bullet window: builder có thể được cấu hình session khác. S09 sở hữu canonical window schedule và tính window identity từ timestamp; `session_decision` chỉ là context/audit evidence, không là nguồn duy nhất cho grace.

---

## 4. Canonical Silver Bullet windows

Timezone cố định: `America/New_York`.

| Window name | Local start | Local end | Days |
|---|---:|---:|---|
| `silver_bullet_london` | 03:00 | 04:00 | Monday–Friday |
| `silver_bullet_ny_am` | 10:00 | 11:00 | Monday–Friday |
| `silver_bullet_ny_pm` | 14:00 | 15:00 | Monday–Friday |

Boundary:

- Event confirmation inside window iff `window_start_utc <= confirmation_close_time < window_end_utc`.
- Retest signal iff `mss.index < N` và `context.bar_close_time <= window_end_utc + 15 minutes`.
- Event đóng đúng `window_end` bị reject.
- Retest đóng đúng `grace_expiry` được accept.
- Retest đóng sau grace dù nến mở trước grace vẫn reject.
- Window date là local calendar date tại New York, không phải UTC date.

Canonical identity:

```python
WindowKey = tuple[str, str, str]
# (window_name, local_date_iso, utc_start_iso)
```

`utc_start_iso` chống mơ hồ DST và giúp audit. Ba khung giờ không nằm trong repeated 01:00 hour, nhưng vẫn phải test cả DST offset -05:00 và -04:00.

Helper nội bộ dự kiến:

- `_window_bounds_for_local_date(local_date, window_name)`.
- `_window_containing_close(close_time)`.
- `_window_by_key(window_key)` hoặc immutable bounds lưu ngay trong narrative.
- `_is_close_inside_window(close_time, bounds)`.
- `_is_within_grace(close_time, bounds)`.

Mọi timestamp phải timezone-aware. Naive/mixed timezone fail closed bằng `StrategyValidationError`/`StrategyStateError`, không tự gán timezone.

---

## 5. Xác định thời gian event không lookahead

### 5.1. Bar-close cache

S09 duy trì working cache:

```python
_bar_close_times: dict[int, pd.Timestamp]
```

Tại evaluate bar N, thêm `N -> context.bar_close_time` vào working copy. Cache chỉ giữ các bar có thể thuộc active windows/narratives; prune sau grace cộng safety lookback `fvg_to_mss_max_bars`. Không giữ toàn bộ stream.

Mục đích:

- Sweep/MSS chỉ ingest khi `index == N`, nên confirmation time là `context.bar_close_time`.
- FVG có `confirmed_at` quá khứ; lấy exact close time từ `_bar_close_times[fvg.confirmed_at]`.
- Cấm suy diễn `fvg.time + k * timeframe` vì gap/weekend/missing bar có thể làm sai window.
- Nếu cache không có `confirmed_at`, pair đó fail closed; không đoán timestamp.

### 5.2. Ingestion rule

- Sweep: `sweep.index == sweep.confirmed_at == sweep.swept_at == N`, valid, mode khớp, direction/pool-kind khớp, close time trong window.
- MSS: chỉ transition bằng event `mss.index == N`; không hồi tố event cũ từ rolling buffer.
- FVG: `confirmed_at <= mss.index`, confirmation close cùng WindowKey, chưa fill tại/trước MSS.

---

## 6. Config và public API

File mới:

`smc/engine/strategies/s09_ict_silver_bullet.py`

### 6.1. `S09Config`

Frozen dataclass dự kiến:

```python
@dataclass(frozen=True)
class S09Config:
    mode: str = "internal"
    enabled_windows: tuple[str, ...] = (
        "silver_bullet_london",
        "silver_bullet_ny_am",
        "silver_bullet_ny_pm",
    )
    grace_minutes: int = 15
    fvg_to_mss_max_bars: int = 10
    entry_level: str = "proximal"
    sl_buffer_price: float = 0.20
    min_rr: float = 1.50
    fallback_rr: float = 2.00
    require_displacement: bool = True
```

Validation:

- Strict type, không silent cast, bool không được lọt vào numeric.
- `mode in {internal, swing}`.
- `enabled_windows` là tuple/list các canonical name, không rỗng, không duplicate; normalize thành tuple theo canonical schedule order, không theo input order.
- `grace_minutes == 15` trong Wave 1. Giữ field trong serialization/audit nhưng reject giá trị khác để không vô tình đổi ADR.
- `fvg_to_mss_max_bars > 0`.
- `entry_level in {proximal, ce_50}`.
- Buffer/RR finite, dương; `fallback_rr >= min_rr`.
- `require_displacement` là bool thật.
- `to_dict()/from_dict()` exact JSON round-trip; reject unknown field.

### 6.2. Strategy profile

```text
strategy_id: S09
name: ICT Silver Bullet
style: time_based
allowed_directions: BUY, SELL
timeframes: M1, M5, M15
max_setup_age_bars: 15
cooldown_bars: 3
min_rr: config.min_rr
```

`StrategyProfile.max_setup_age_bars=15` giữ đúng Wave 1 profile contract và validation hiện có (`> 0`). Candidate S09 vẫn có `expiry_bar=N`: nó chỉ được selector xử lý tại signal bar; fill N+1 do T53.9 thực hiện. Grace chỉ điều khiển thời gian chờ retest trước khi emit, không biến Candidate thành pending order nhiều bar.

Constructor fail-fast nếu config không phải `S09Config | None`.

---

## 7. State machine

```text
SWEEP_SEEN
  ├─→ FVG_READY
  ├─→ INVALIDATED
  └─→ WINDOW_EXPIRED

FVG_READY
  ├─→ EMITTED
  ├─→ INVALIDATED
  └─→ WINDOW_EXPIRED
```

Không cần persistent `OUTSIDE_WINDOW`/`WINDOW_OPEN`; window được tính deterministic từ clock. Không tạo empty state cho mọi window nếu không có sweep.

```python
SweepKey = tuple[str, str, int, tuple[int, ...]]

@dataclass
class S09Narrative:
    window_key: WindowKey
    window_start_utc: pd.Timestamp
    window_end_utc: pd.Timestamp
    grace_expiry_utc: pd.Timestamp
    sweep_key: SweepKey
    direction: str
    sweep: LiquiditySweepSnapshot
    stage: S09NarrativeStage
    mss: StructureEventSnapshot | None
    fvg: FairValueGapSnapshot | None
    ready_at: int | None
    fvg_filled_internally: bool
    fvg_filled_bar: int | None
    terminal_reason: str | None
```

Persistent state:

- `_narratives: dict[(WindowKey, SweepKey), S09Narrative]`.
- `_consumed_windows: dict[WindowKey, grace_expiry_utc]`.
- `_emitted_clusters: dict[str, grace_expiry_utc]`.
- `_bar_close_times: dict[int, Timestamp]` bounded.
- Last-bar idempotency cache giống S01/S05.

Terminal narrative phải prune tại atomic commit. Consumed window/cluster prune khi `current_bar_close_time > grace_expiry_utc`.

---

## 8. Canonical algorithm mỗi closed bar N

Thứ tự bắt buộc:

1. Validate `StrategyContext`, timeframe, timezone-aware timestamps và `bar_close_time >= timestamp`.
2. Monotonic guard:
   - first bar hợp lệ;
   - same bar + same full payload trả cached result;
   - same bar + payload khác raise;
   - backward hoặc gap bar raise;
   - timestamp/bar-close không tăng raise.
3. Zero-future-leak guard trên sweep, structure, FVG, pool, bias, session.
4. Tạo working copies cho toàn bộ mutable state.
5. Add bar-close mapping cho N; prune consumed window, cluster và cache cũ trên working copies.
6. Tính canonical window chứa `context.bar_close_time`.
7. Ingest sweep mới chỉ khi sweep confirmation đúng N và close nằm trong enabled window.
8. Với mỗi active narrative theo canonical sort:
   - expire nếu `bar_close_time > grace_expiry`;
   - reject nếu window đã consumed;
   - kiểm tra bias không opposed/missing;
   - kiểm tra sweep extreme và opposite structure invalidation;
   - nếu `SWEEP_SEEN`, chỉ tìm MSS index N và FVG pair cùng window;
   - nếu `FVG_READY`, refresh/track FVG lifecycle as-of N;
   - retest chỉ xét khi `N > mss.index` và close time trong grace.
9. Gom proposal theo WindowKey.
10. Mỗi window chỉ chọn một canonical proposal. Nếu proposal đầu không build được do geometry/RR, thử proposal tiếp theo cùng deterministic rank; chỉ consume window khi Candidate thực sự được emit.
11. Sort candidates canonical; tối đa một candidate/window, có thể nhiều candidate trong một bar nếu bar đó thuộc các window khác nhau (thực tế canonical windows không overlap).
12. Atomic commit toàn bộ state/cache/result.

Không commit pruning, consumed-window hay bar-close cache nếu bất kỳ helper nào raise.

---

## 9. Sweep, bias và MSS/FVG linkage

### 9.1. Sweep direction

- `bullish` sweep + `equal_lows|swing_low` → BUY.
- `bearish` sweep + `equal_highs|swing_high` → SELL.
- Mismatch direction/pool kind reject.
- Chỉ nhận `valid=True`, mode khớp và event đúng current bar.

### 9.2. HTF bias

- BUY + bearish bias: terminal `htf_bias_mismatch`.
- SELL + bullish bias: terminal `htf_bias_mismatch`.
- Aligned pass.
- Neutral pass; scoring penalty thuộc T53.8.
- `htf_bias is None` fail closed; không emit.
- Kiểm tra tại admission và mọi bar narrative active; opposed không hồi sinh trong cùng window.

### 9.3. MSS/FVG pair

MSS candidate:

- `mss.index == N`.
- `event_type in {CHoCH, BOS}`; CHoCH ưu tiên nếu cùng bar.
- `break_type == close`.
- mode/direction khớp sweep.
- displacement theo config.
- confirmation close nằm trong cùng WindowKey.
- `mss.structure_leg_id is not None`.

FVG candidate:

- mode/direction khớp MSS.
- `sweep.index <= fvg.index < mss.index`.
- `fvg.confirmed_at <= mss.index`.
- `mss.index - fvg.index <= fvg_to_mss_max_bars`.
- non-null `fvg.structure_leg_id == mss.structure_leg_id`.
- confirmation close của `fvg.confirmed_at` nằm trong cùng WindowKey.
- `filled_at is None or filled_at > mss.index`; future state `filled_at > N` trong as-of context là error, không dùng để ra quyết định.
- Không có opposite BOS/CHoCH trong interval inclusive `[fvg.index, mss.index]`.

Tie-break pair:

1. MSS index ascending.
2. CHoCH trước BOS.
3. Broken swing index ascending.
4. Structure leg ID ascending.
5. FVG index descending (gần MSS nhất).
6. FVG confirmed_at descending.
7. FVG top, bottom ascending.

Không dùng input order.

---

## 10. Retest và invalidation

FVG retest BUY:

- Zone overlap: `context.low <= fvg.top and context.high >= fvg.bottom`.
- Close respect: `context.close >= fvg.bottom`.
- Full fill tại chính N được phép nếu close respect.
- Fill trước N reject.

SELL đối xứng:

- Zone overlap như trên.
- Close respect: `context.close <= fvg.top`.
- Full fill tại N được phép nếu close respect.

Invalidation trước emission, theo thứ tự:

1. Grace expired.
2. Window consumed.
3. HTF bias opposed/missing.
4. Sweep extreme close violation:
   - BUY: `close < sweep.price_wick`.
   - SELL: `close > sweep.price_wick`.
5. Opposite BOS/CHoCH sau MSS: `mss.index < event.index <= N`.
6. FVG fill từ bar trước.
7. FVG close-through tại N.
8. Retest/time boundary.

Event tại chính retest bar N phải invalidate trước candidate.

---

## 11. Entry, SL, target và precision

- Entry proximal:
  - BUY: `fvg.top`.
  - SELL: `fvg.bottom`.
- CE50: `(top + bottom) / 2`.
- SL:
  - BUY: `sweep.price_wick - sl_buffer_price`.
  - SELL: `sweep.price_wick + sl_buffer_price`.
- Target pool:
  - BUY: nearest eligible `equal_highs|swing_high` có `price > entry`.
  - SELL: nearest eligible `equal_lows|swing_low` có `price < entry`.
  - valid, unswept, correct mode, confirmed as-of N.
  - tie-break `(distance, confirmed_at, kind, sorted(indices))`.
- Nếu nearest pool sai geometry hoặc RR dưới min, dùng fixed `fallback_rr`; không nhảy sang pool xa.
- Round entry/SL/TP 3 decimals trước geometry.
- Tính lại planned RR từ rounded levels, round 2 decimals.
- Geometry fail hoặc risk/reward không dương → không emit, không consume window.

Cash-basis RR tại fill không thuộc task này.

---

## 12. Evidence, cluster và setup IDs

Canonical evidence order:

```text
Liquidity Sweep → MSS → FVG [→ Target Pool]
```

Không thêm session EvidenceRef chỉ để trang trí, vì model bắt buộc price trong khi window không có source price. Window audit nằm trong candidate `meta`.

S09 phải dùng cùng evidence-ID và cluster-ID convention với S01 cho cùng canonical sweep/MSS/FVG:

```python
leg_component = f"leg-{mode}-{direction}-{mss.broken_swing_index}"
zone_component = f"fvg-{mode}-{direction}-{fvg.index}"
cluster_id = make_cluster_id(direction, leg_component, zone_component)
setup_id = make_setup_id("S09", direction, N, cluster_id)
```

Yêu cầu:

- S01 và S09 cùng market opportunity → cùng `evidence_cluster_id`, khác `setup_id` do strategy ID.
- ID không dùng float, local UTC offset hay Python hash.
- WindowKey/meta dùng canonical ISO strings.
- Evidence timestamp giữ source timestamp; pool không có time thì `None`, không gán signal time giả.

Metadata tối thiểu:

- `signal_bar`, `available_from_bar=N+1`.
- `window_key`, `window_name`, local date.
- `window_start_utc`, `window_end_utc`, `grace_expiry_utc`.
- `signal_bar_close_time`.
- sweep/MSS/FVG keys, structure leg.
- entry policy, SL anchor/buffer, target policy.
- HTF bias at signal.
- `session_time_basis="bar_close_time"`.

---

## 13. Deterministic one-setup-per-window policy

- Chỉ candidate emit thành công mới consume WindowKey.
- Sau khi consume, toàn bộ narrative còn lại trong window terminal `window_already_consumed`.
- Các proposal cùng bar/window xếp hạng:
  1. Sweep index ascending (cơ hội xác nhận sớm hơn).
  2. `clean` trước `wick_only`.
  3. Pool kind.
  4. Sorted pool indices.
  5. MSS/FVG canonical pair rank.
  6. Cluster ID.
- Thử theo rank đến candidate đầu tiên build hợp lệ.
- Input permutation không thay đổi winner.

---

## 14. Atomicity, idempotency và bounded state

### 14.1. Atomicity

Working-copy bắt buộc cho:

- narratives;
- consumed windows;
- emitted clusters;
- bar-close cache;
- last-bar result/cache.

Fault injection sau ingestion, trong pair matcher và trong candidate builder phải chứng minh persistent state không đổi.

### 14.2. Idempotency

- Same bar/full payload giống: trả exact cached tuple, không evaluate lại.
- Same bar/payload khác: raise `StrategyStateError`.
- Gap/backward bar hoặc non-increasing timestamps: raise trước mutation.

### 14.3. Bound

- Narrative tồn tại tối đa từ sweep đến grace expiry của cùng window.
- Terminal narrative prune ngay cuối successful bar.
- Window/cluster tombstone prune sau grace.
- Bar-close cache chỉ giữ active-window/grace + linkage lookback.
- Với 3 non-overlapping windows/ngày và bounded context collection, state O(1) theo chiều dài stream; báo complexity theo event-rate/config, không tuyên bố hard bound không có giả định.

`reset()` xóa tất cả state và replay phải cho output giống instance mới.

---

## 15. File thay đổi dự kiến

| File | Thay đổi |
|---|---|
| `smc/engine/strategies/s09_ict_silver_bullet.py` | Config, windows, state machine, candidate builder |
| `smc/engine/strategies/__init__.py` | Export S09 |
| `smc/engine/__init__.py` | Public export S09 |
| `tests/test_smc_strategy_s09.py` | Toàn bộ unit/integration/QC tests |
| `.agent/DECISIONS.md` | Accept ADR 22 sau QC plan |
| `.agent/TASKS.md` | T53.6 status/acceptance |
| `.agent/CHANGELOG.md` | Ghi implementation/test thật |
| `walkthrough.md` hoặc artifact walkthrough | Báo cáo sau implementation |

Không dự kiến sửa `smc/engine/context.py`, model chung, detector hay BacktestEngine.

---

## 16. Test matrix bắt buộc

Tạo tối thiểu các nhóm sau; số test có thể tăng khi implementation phát hiện boundary mới.

### A. Config/profile/protocol

1. Defaults exact.
2. JSON round-trip exact.
3. Reject bool trong numeric.
4. Reject NaN/Inf/zero/negative.
5. Reject unknown mode/window/entry level.
6. Reject duplicate/empty enabled windows.
7. Canonicalize window order.
8. Reject grace khác 15 trong Wave 1.
9. Fail-fast wrong config type.
10. Protocol, registry và clean exports.

### B. Window/timezone/DST

11. Ba window canonical exact.
12. Start inclusive.
13. Event end exclusive.
14. Retest grace exact inclusive.
15. Retest 1 microsecond sau grace reject.
16. Bar open trước grace nhưng close sau grace reject.
17. Local NY date khác UTC date không collision.
18. Weekends reject.
19. Winter offset EST.
20. Summer offset EDT.
21. Spring DST transition date.
22. Fall DST transition date.
23. WindowKey deterministic/reversible.
24. Hai ngày cùng window name không share state.
25. Window disabled không ingest.

### C. Sweep admission/bias

26. Bullish low sweep → BUY.
27. Bearish high sweep → SELL.
28. Direction/pool mismatch reject.
29. Invalid/wrong-mode sweep reject.
30. Old rolling-buffer sweep không ingest hồi tố.
31. Sweep close trước start reject.
32. Sweep close đúng start pass.
33. Sweep close đúng end reject.
34. Aligned bias pass.
35. Neutral bias pass.
36. Opposed bias reject terminal.
37. Missing bias fail closed.
38. Bias chuyển opposed trước signal không hồi sinh.

### D. MSS/FVG linkage

39. Valid sweep→CHoCH→FVG pair.
40. BOS được dùng làm MSS khi hợp lệ.
41. Wick break reject.
42. Displacement false reject/default; config behavior.
43. Wrong direction/mode reject.
44. MSS old trong rolling buffer không transition hồi tố.
45. MSS end boundary reject.
46. FVG confirm cùng window pass.
47. FVG formed trong window nhưng confirmed đúng/sau end reject.
48. Thiếu bar-close mapping cho FVG fail closed.
49. FVG before sweep reject.
50. FVG index tại MSS reject.
51. Lag 10 pass, lag 11 reject.
52. Null/mismatch structure leg reject.
53. Opposite event inclusive trong FVG–MSS reject.
54. FVG filled tại/trước MSS reject.
55. Pair tie-break permutation invariant.

### E. Retest/invalidation

56. Retest sau MSS emits.
57. Retest cùng MSS bar reject.
58. Wick overlap + close respect BUY.
59. SELL mirror.
60. Filled_at None pass khi touch.
61. Filled_at N pass nếu close respect.
62. Filled_at < N reject.
63. Filled_at > N future leak raise.
64. Close-through same bar reject.
65. Sweep extreme violation reject.
66. Opposite shift trước retest reject.
67. Opposite shift đúng retest bar reject trước emission.
68. Grace exact pass.
69. Sau grace reject/prune.
70. Missing FVG snapshot không tự suy diễn invalid; internal lifecycle vẫn an toàn.

### F. Pricing/evidence/IDs

71. Proximal BUY/SELL.
72. CE50 BUY/SELL.
73. Sweep-extreme SL ±0.20.
74. Nearest opposing pool independent input order.
75. Ineligible pool ignored.
76. Nearest pool RR thấp → fixed fallback.
77. Post-rounding geometry fail closed.
78. Planned RR tính từ rounded levels.
79. Evidence order/source timestamps.
80. S09 cluster ID bằng S01 cho cùng sweep/MSS/FVG.
81. Setup ID khác S01.
82. IDs injective/float-free.
83. Window audit metadata exact JSON safe.
84. Candidate `expiry_bar == signal_bar`, `available_from_bar=N+1`.

### G. One-per-window/state/atomicity

85. Hai valid narratives cùng window chỉ emit một.
86. Candidate đầu fail geometry không consume; candidate hợp lệ tiếp theo emit.
87. Sau emission, later setup cùng window reject.
88. Window khác trong cùng ngày được emit độc lập.
89. Ngày kế tiếp reset ownership tự nhiên.
90. Same-bar permutation cho cùng winner.
91. Identical retry cached.
92. Conflicting retry raise.
93. Gap/backward/non-increasing time raise và không mutate.
94. Fault trong matcher rollback toàn state.
95. Fault trong candidate builder rollback pruning/consumed/cache.
96. Reset/replay parity.
97. Long stream giữ narratives/windows/clusters/bar-close cache bounded.

### H. Production integration/parity

98. Registry evaluation.
99. `StrategyContextBuilder` thật tạo sweep/MSS/FVG/retest trong window.
100. Batch contexts → S09.
101. Incremental builder → S09.
102. JSON context replay.
103. Full `[candidate.to_dict()]` parity ba mode.
104. Append future candles: context prefix và candidate prefix không đổi.
105. DST production fixture winter/summer cho cùng local window semantics.
106. Context/snapshots không mutate.

Integration tests 99–105 phải dùng production builder, không chỉ DTO dựng tay. Nếu detector fixture quá giòn, tách rõ test detector integration và strategy boundary nhưng không được gắn nhãn “end-to-end” cho DTO-only test.

---

## 17. Independent QC probes

Sau unit tests, QC tối thiểu:

1. Event tại start/end boundary.
2. M15 open trước grace, close sau grace.
3. Winter/summer cùng 10:00 NY map sang UTC khác nhau nhưng cùng kết quả.
4. FVG confirm ngoài window dù middle candle trong window.
5. Future `filled_at` guard trước mutation.
6. Opposite structure ngay signal bar.
7. Hai setup cùng window.
8. Atomic exception rollback.
9. S01/S09 shared cluster ID.
10. Batch/incremental/JSON/future-append parity.
11. Long workload state bound cho cả bốn persistent maps.

Probe quan trọng phải chuyển thành regression test. Không dẫn script tạm không tồn tại trong walkthrough.

---

## 18. Trình tự triển khai

### T53.6.1 — Config, canonical windows, time helpers

- Tạo module, config, constants, WindowKey/bounds helpers.
- Test A/B trước.

### T53.6.2 — Stateful lifecycle foundation

- Narrative model, monotonic/idempotent/future guard, atomic copies.
- Bar-close cache và pruning.
- Sweep admission/bias/window expiry.

### T53.6.3 — MSS/FVG matcher

- Current-bar MSS only.
- Same-window FVG confirmation mapping.
- Same-leg/opposite-event/tie-break.

### T53.6.4 — Retest, candidate, one-per-window

- FVG lifecycle/retest/invalidation.
- Entry/SL/target/RR/evidence/IDs/meta.
- Canonical winner và consumed-window state.

### T53.6.5 — Integration/QC/docs

- Production builder, registry, DST, parity, future append, long workload.
- Full regression, compile, diff check.
- Walkthrough trung thực và QC độc lập.

Không chuyển subtask sau sang `doing` trước khi subtask trước pass targeted tests.

---

## 19. Verification commands

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_smc_strategy_s09 -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_strategy_s01 tests.test_smc_strategy_s05 tests.test_smc_engine_registry -v
.\.venv\Scripts\python.exe -m unittest discover tests -v
.\.venv\Scripts\python.exe -m compileall -q smc tests
git diff --check
```

Timing chỉ báo cáo tham khảo nếu có script/test tái lập. Correctness, no-lookahead, parity và bounded state là gate bắt buộc.

---

## 20. Definition of Done — Gate C-S09

- [ ] ADR 22 implementation details được QC/accept.
- [ ] Config/profile/protocol/export strict và deterministic.
- [ ] Ba window New York + weekdays + DST boundaries pass.
- [ ] Sweep/MSS/FVG được xác nhận trong cùng WindowKey theo bar-close time.
- [ ] FVG confirmation time không suy diễn bằng bar arithmetic.
- [ ] Grace exact 15 phút dùng `bar_close_time`; exact-boundary tests pass.
- [ ] HTF opposed/missing fail closed; neutral policy pass.
- [ ] Retest, invalidation, entry, SL, target, RR và rounding pass Long/Short.
- [ ] Tối đa một emitted setup/window; deterministic ownership.
- [ ] S01/S09 shared opportunity có cùng cluster ID.
- [ ] Atomicity, idempotency, reset và bounded state pass.
- [ ] Production builder/registry integration thật pass.
- [ ] Batch/incremental/JSON/future-append full parity pass.
- [ ] Existing S01/S05/context/registry và full regression không hỏng.
- [ ] `compileall` và `git diff --check` pass.
- [ ] Independent QC không còn P0/P1.
- [ ] Tài liệu chỉ nêu bằng chứng thực sự tái lập được.

T53.6 chỉ chuyển `done` sau QC; sau đó mới mở T53.7.
