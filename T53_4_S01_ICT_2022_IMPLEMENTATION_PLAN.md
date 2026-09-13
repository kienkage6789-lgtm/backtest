# T53.4 — Kế hoạch triển khai S01 ICT 2022 Reversal

> Trạng thái: READY FOR IMPLEMENTATION  
> Phạm vi: chỉ T53.4 — strategy template S01 và kiểm thử trực tiếp  
> Nguồn chuẩn: ADR 16/17/20, `T53_WAVE1_SEMANTICS.md`, domain models T53.1, context builder T53.2 và registry T53.3

## 1. Mục tiêu

Triển khai strategy `S01` theo chuỗi closed-bar, zero-lookahead:

```text
Liquidity Sweep
  → MSS/BOS/CHoCH có displacement
  → FVG cùng hướng, cùng structure leg
  → retest FVG sau MSS
  → CandidateSetup tại bar đóng N
```

Kết quả T53.4 phải là một `StrategyTemplate` stateful, deterministic, có Long/Short symmetry, chạy được qua `StrategyRegistry.evaluate_enabled()` và trả về `tuple[CandidateSetup, ...]`.

T53.4 không mở lệnh. Tín hiệu tại bar N chỉ được adapter T53.9 xét khớp ở Open N+1.

## 2. Ranh giới trách nhiệm

### Trong phạm vi

- Nhận `StrategyContext` bất biến as-of bar N.
- Theo dõi nhiều chuỗi S01 đang chờ theo từng Liquidity Sweep.
- Ghép Sweep → MSS → FVG bằng stable identity và structure leg.
- Áp dụng event ordering, timeout, expiry và invalidation.
- Xác nhận retest FVG trên nến N đã đóng.
- Tính entry, SL, target và `planned_rr` theo giá cấu trúc.
- Sinh `EvidenceRef`, cluster ID, setup ID và metadata deterministic.
- Chống duplicate delivery và phát trùng setup.
- Hỗ trợ `reset()` và replay từ context đã JSON round-trip.

### Ngoài phạm vi

- Không sửa detector, `StrategyContextBuilder`, registry hoặc BacktestEngine trừ khi phát hiện lỗi contract độc lập và có QC riêng.
- Không thực hiện fill N+1, spread/commission/slippage, cash-basis RR tại giá khớp, cancellation event hoặc position sizing; các việc này thuộc T53.9.
- Không kích hoạt cooldown khi phát candidate. ADR 16 quy định cooldown chỉ bắt đầu sau fill thật tại N+1; protocol hiện chưa có execution feedback.
- Không triển khai regime gate, reason-code evaluation, conflict/dedup giữa nhiều strategy hoặc scoring; thuộc T53.7–T53.8.
- Không tích hợp S05/S09 và không thay đổi `run_smc_strategy()` baseline.

### Diễn giải bắt buộc về state terminal

Strategy chỉ biết đến `ENTRY_PENDING`/candidate emission. `FILLED` không được giả lập bên trong S01. Sau khi phát candidate, chuỗi evidence được đánh dấu `EMITTED` và loại khỏi active state để không phát lại. Trạng thái fill/cancel thật do T53.9 quản lý.

## 3. File dự kiến

### File mới

- `smc/engine/strategies/__init__.py`
- `smc/engine/strategies/s01_ict_2022.py`
- `tests/test_smc_strategy_s01.py`

### File cập nhật

- `smc/engine/__init__.py`: export public `S01Config`, `S01ICT2022Strategy`.
- `.agent/TASKS.md`: cập nhật checklist và trạng thái T53.4 sau khi triển khai/QC.
- `.agent/CHANGELOG.md`: ghi thay đổi thực tế và kết quả verification.
- `walkthrough.md`: báo cáo implementation và QC cuối.

Không tạo dependency ngược từ domain package sang `engine/strategies.py` ở tầng platform.

## 4. Public API và cấu hình

### `S01Config`

Tạo `@dataclass(frozen=True)` với validation strict, không ép kiểu ngầm từ bool/string:

| Field | Default | Contract |
|---|---:|---|
| `mode` | `"internal"` | Chỉ `internal` hoặc `swing`; Sweep, MSS và FVG phải cùng mode này. |
| `sweep_to_mss_max_bars` | `20` | Số nguyên dương; MSS hợp lệ đến hết `sweep.index + 20`. |
| `fvg_to_mss_max_bars` | `10` | Số nguyên dương; `mss.index - fvg.index <= 10`. |
| `entry_expiry_bars` | `15` | Số nguyên dương; retest hợp lệ đến hết `ready_at + 15`. |
| `entry_level` | `"proximal"` | Chỉ `proximal` hoặc `ce_50`. |
| `sl_buffer_price` | `0.20` | Float hữu hạn, `> 0`, đơn vị giá XAU/USD. |
| `min_rr` | `1.50` | Float hữu hạn, `> 0`. |
| `fallback_rr` | `2.00` | Float hữu hạn và `>= min_rr`. |
| `require_displacement` | `True` | Bool strict; mặc định MSS phải có `displacement=True`. |

`S01Config` phải có `to_dict()`/`from_dict()` exact JSON round-trip để config có thể audit và tái tạo.

### `S01ICT2022Strategy`

- `strategy_id = "S01"`.
- `profile = StrategyProfile(...)` bất biến:
  - `name="ICT 2022 Reversal"`
  - `version="1.0.0"`
  - `style="reversal"`
  - `allowed_directions=("BUY", "SELL")`
  - `timeframes=("M1", "M5", "M15")`
  - `max_setup_age_bars=config.entry_expiry_bars`
  - `cooldown_bars=3` chỉ là metadata cho tầng execution sau này
  - `min_rr=config.min_rr`
  - `params=config.to_dict()`
- Public methods đúng protocol:
  - `evaluate(context) -> tuple[CandidateSetup, ...]`
  - `reset() -> None`

Constructor chỉ nhận `S01Config | None`. Không nhận detector mutable hoặc callback ngoài.

## 5. Mô hình state nội bộ

### Một state cho mỗi sweep

Không dùng một state global duy nhất. Dùng mapping nội bộ theo `sweep_key` để nhiều narrative hợp lệ có thể cùng tồn tại:

```text
SWEEP_SEEN → MSS_CONFIRMED/FVG_READY → EMITTED | EXPIRED | INVALIDATED
```

Mỗi record nội bộ tối thiểu lưu:

- `sweep_key`, `direction`, `sweep` snapshot.
- `mss_key`, `mss` snapshot hoặc `None`.
- `fvg_key`, `fvg` snapshot hoặc `None`.
- `ready_at`: bar MSS xác nhận và FVG được ghép thành công.
- `expiry_bar = ready_at + entry_expiry_bars`.
- `terminal_reason` chỉ dùng nội bộ/test/debug, không tự phát minh public rejection model.

State phải sở hữu snapshot immutable, không giữ object detector mutable.

### Stable identity nội bộ

- Sweep key: `(mode, direction, index, tuple(sorted(pool_indices)))`.
- MSS key: `(mode, event_type, direction, index, broken_swing_index)`.
- FVG key: `(mode, direction, index)`.
- Pool key: `(mode, kind, tuple(sorted(indices)))`.

Không dùng `id(object)`, hash ngẫu nhiên hoặc thứ tự input.

### Bounded state

- Narrative chưa có MSS bị loại sau `sweep.index + 20`.
- Narrative đã FVG_READY bị loại sau `ready_at + 15`.
- State terminal bị xóa ngay sau khi hoàn tất bar hiện tại.
- Set chống duplicate emission chỉ giữ cluster trong cửa sổ cần thiết; prune khi không còn narrative liên quan và quá expiry.
- Complexity mỗi bar bị chặn bởi các collection đã bounded trong `StrategyContext` và active narratives còn trong timeout; không quét lịch sử vô hạn.

## 6. Pipeline xử lý mỗi closed bar N

Thứ tự dưới đây là bắt buộc để tránh lỗi boundary:

1. Validate `context` là `StrategyContext` và timeframe nằm trong profile.
2. Guard monotonic:
   - Lần đầu có thể bắt đầu ở bất kỳ `bar_index >= 0`.
   - Context mới phải tăng đúng một bar; gap hoặc bar lùi phải raise `StrategyStateError`, vì không thể chứng minh invalidation của các nến bị bỏ qua.
   - Gọi lại cùng bar với payload giống hệt trả đúng cached tuple, không advance state.
   - Cùng bar nhưng payload khác phải raise `StrategyStateError`.
3. Kiểm tra future-leak phòng thủ trên mọi evidence dùng trong bar; `index/confirmed_at/filled_at > N` phải fail fast, không silently defer dữ liệu vi phạm `StrategyContext`.
4. Ingest sweep mới tại N theo canonical sort.
5. Với narrative `SWEEP_SEEN`, tìm MSS mới hợp lệ và ghép FVG đã xác nhận as-of MSS.
6. Với narrative FVG_READY cũ, xử lý invalidation/opposite shift/expiry trước khi xét entry.
7. Chỉ xét retest khi `N > mss.index`.
8. Tạo candidate, đánh dấu cluster đã emit và retire narrative.
9. Sort candidates theo `(direction, evidence_cluster_id, setup_id)` rồi trả tuple.
10. Chỉ commit state/cache sau khi toàn bộ bar thành công; exception không được để lại partial state.

Để đạt atomicity ở bước 10, tính transition trên bản working copy nhỏ của state hoặc chuẩn bị mutations rồi commit một lần cuối.

## 7. Quy tắc ghép evidence

### 7.1 Sweep

- Chỉ nhận `valid=True`, `mode == config.mode`, `confirmed_at <= N`, `swept_at <= N`.
- `direction="bullish"` tương ứng BUY, chỉ hợp lệ với `equal_lows`/`swing_low`.
- `direction="bearish"` tương ứng SELL, chỉ hợp lệ với `equal_highs`/`swing_high`.
- Chỉ ingest sweep có `index == N`. Sweep cũ trong `recent_sweeps` không được dùng để warm-start narrative vì context hiện tại không đủ OHLC trung gian để chứng minh lịch sử invalidation; replay chuẩn phải feed liên tục từ trước sweep.

### 7.2 MSS/BOS/CHoCH

MSS hợp lệ cho narrative iff:

- `mss.mode == sweep.mode == config.mode`.
- `mss.direction == sweep.direction`.
- `mss.event_type in {"BOS", "CHoCH"}`.
- `mss.break_type == "close"`.
- Nếu `require_displacement=True` thì `mss.displacement is True`.
- `sweep.index < mss.index <= sweep.index + 20`.
- `mss.index <= N`.

Nếu có nhiều MSS hợp lệ, chọn deterministic MSS sớm nhất theo `(index, CHoCH-before-BOS, broken_swing_index, structure_leg_id-or-empty)`.

### 7.3 FVG cùng displacement leg

FVG hợp lệ cho cặp sweep/MSS iff:

- `fvg.mode == mss.mode`.
- `fvg.direction == mss.direction`.
- `sweep.index <= fvg.index < mss.index`.
- `fvg.confirmed_at <= mss.index`.
- `mss.index - fvg.index <= 10`.
- `fvg.structure_leg_id` và `mss.structure_leg_id` đều không `None` và bằng nhau.
- Không có structure event ngược hướng trong đoạn đóng `fvg.index <= event.index <= mss.index`.
- FVG chưa fill trước MSS: `filled_at is None` tại snapshot MSS; mọi `filled_at <= mss.index` đều reject.

Nếu nhiều FVG cùng leg hợp lệ, chọn FVG gần MSS nhất theo key `(-fvg.index, -fvg.confirmed_at, top, bottom)`; không phụ thuộc input order.

`ready_at` được khóa bằng `mss.index`, vì FVG chỉ trở thành entry zone sau khi MSS xác nhận. Do đó:

```text
expiry_bar = mss.index + 15
valid:   N <= expiry_bar
expired: N == expiry_bar + 1
```

`expiry_bar` khóa bar phát tín hiệu. Candidate phát hợp lệ tại đúng `expiry_bar` vẫn được adapter T53.9 xét ở Open N+1 theo contract signal N/fill N+1; adapter không được tự biến boundary inclusive thành exclusive.

## 8. Invalidation và retest

### Invalidation trước entry

Narrative FVG_READY bị hủy nếu một trong các điều kiện xảy ra:

- BUY: `context.close < sweep.price_wick`.
- SELL: `context.close > sweep.price_wick`.
- Có BOS hoặc CHoCH ngược hướng với `mss.index < event.index <= N`.
- FVG đã full-fill ở bar trước N.
- `N > expiry_bar`.

Opposite structure ở chính bar N phải invalidate trước retest cùng bar N.

### Theo dõi FVG sau khi đã ghép

Không phụ thuộc việc FVG còn xuất hiện trong `context.active_fvgs`, vì collection có cap và FVG bị loại khỏi active list sau fill. State đã giữ snapshot zone và tự cập nhật lifecycle bằng OHLC đóng từng bar:

- Bullish FVG full-fill tại bar N khi `context.low <= fvg.bottom`.
- Bearish FVG full-fill tại bar N khi `context.high >= fvg.top`.

Nếu full-fill xảy ra ở chính N, candidate vẫn chỉ có thể hợp lệ nếu close-respect bên dưới đạt; sau bar N narrative luôn terminal.

### Retest hợp lệ tại N

Điều kiện chung:

- `N > mss.index`.
- `N <= expiry_bar`.
- Candle overlap zone: `context.low <= fvg.top and context.high >= fvg.bottom`.
- FVG không được đã fill trước N.

Close-respect:

- BUY: `context.close >= fvg.bottom`.
- SELL: `context.close <= fvg.top`.

Nếu nến N full-fill nhưng close-respect đạt, `filled_at == N` được phép phát candidate đúng ADR 16. Nếu close xuyên zone, invalidate và không emit.

## 9. Entry, SL, target và RR

### Entry

- BUY proximal: `fvg.top`.
- SELL proximal: `fvg.bottom`.
- `ce_50`: `(fvg.top + fvg.bottom) / 2` cho cả hai hướng.

### Stop loss

- BUY: `min(sweep.price_wick, mss.broken_swing_price) - sl_buffer_price`.
- SELL: `max(sweep.price_wick, mss.broken_swing_price) + sl_buffer_price`.

### Target chính

Chọn pool đối diện gần entry nhất trong `context.active_pools`:

- BUY: `kind in {equal_highs, swing_high}` và `pool.price > entry`.
- SELL: `kind in {equal_lows, swing_low}` và `pool.price < entry`.
- Pool phải `valid=True`, `swept=False`, `confirmed_at <= N`, đúng `mode`.
- Sort theo `(absolute distance to entry, confirmed_at, kind, sorted indices)`.

Dùng `pool.price` làm structural TP. Nếu pool gần nhất tạo RR sau rounding `< min_rr`, không tìm pool xa hơn; dùng fallback fixed RR theo ADR.

### Fixed-RR fallback

- BUY: `tp = entry + fallback_rr * (entry - sl)`.
- SELL: `tp = entry - fallback_rr * (sl - entry)`.
- `target_type="fixed_rr"`.

### Precision và validation

1. Tính raw entry/SL/TP.
2. Round cả ba mức về 3 decimals.
3. Kiểm tra strict geometry trên giá đã round.
4. Tính lại `planned_rr` từ giá đã round và round RR 2 decimals.
5. Chỉ emit nếu `planned_rr >= min_rr`.

`planned_rr` ở T53.4 là structural pre-fill RR. Cash-basis RR chính xác không thể biết trước actual Open N+1; T53.9 bắt buộc revalidate sau spread và commission trước fill. Không được gắn nhãn structural RR là cash RR.

## 10. Evidence, ID và metadata

### Safe ID components

Không đưa raw `structure_leg_id` có dấu `:` vào helper base-token. Dùng component tái tạo được từ MSS:

- `leg_component = "leg-{mode}-{direction}-{broken_swing_index}"`.
- `zone_component = "fvg-{mode}-{direction}-{fvg.index}"`.
- `cluster_id = make_cluster_id(direction, leg_component, zone_component)`.
- `setup_id = make_setup_id("S01", direction, N, cluster_id)`.

### Evidence bắt buộc

Candidate phải có đúng thứ tự canonical:

1. Liquidity Sweep — `kind="liquidity_sweep"`, price=`price_wick`.
2. Structure Event — `kind="structure_event"`, price=`broken_swing_price`.
3. FVG — `kind="fair_value_gap"`, price=`entry_price`.
4. Liquidity Pool target nếu dùng target chính — `kind="liquidity_pool"`, price=`pool.price`.

Mỗi `EvidenceRef` dùng `make_evidence_id()` với sub-key chỉ chứa `[A-Za-z0-9_.-]`. `details` lưu source fields cần audit nhưng không chứa object mutable.

### Candidate metadata tối thiểu

- `signal_bar`, `available_from_bar=N+1`.
- `sweep_key`, `mss_key`, `fvg_key` ở dạng JSON-safe list/string.
- raw `structure_leg_id` để audit.
- `ready_at`, `expiry_bar`, `entry_level`.
- `stop_anchor="sweep_or_broken_swing_extreme"`, `sl_buffer_price`.
- `target_source`, `target_pool_key` hoặc `fixed_rr`.
- `htf_bias` tại N.
- `rr_basis="structural_pre_fill"`.

`quality_scores` để trống trong T53.4; T53.8 chịu trách nhiệm scoring.

## 11. HTF bias policy

Áp dụng tại bar retest trước khi emit:

- BUY + bearish bias → không emit.
- SELL + bullish bias → không emit.
- Bias aligned → cho phép.
- Bias neutral → cho phép.
- `htf_bias is None` → fail closed, không emit.

Không tự cộng 60/30 điểm trong strategy. Số điểm context thuộc T53.8.

## 12. Atomicity, idempotency và reset

- `evaluate()` không mutate `StrategyContext` hoặc snapshot con.
- Duplicate evidence trong các tuple input được dedup bằng stable key trước matching.
- Context input permutation không đổi candidate output.
- Cùng context gọi lại trực tiếp trả chính tuple cached.
- Một FVG cluster chỉ emit một lần trong một lifecycle strategy.
- Nếu bất kỳ validation/output construction nào raise, state trước bar phải được giữ nguyên.
- `reset()` xóa narratives, emitted set, last-bar cache và poisoned/error state; replay lại cùng contexts phải cho serialized output y hệt.

## 13. Kế hoạch test chi tiết

### A. Config và protocol

1. Default config/profile đúng toàn bộ giá trị khóa.
2. Config JSON round-trip exact.
3. Reject bool ở field số; reject NaN/Inf/0/âm; reject mode/entry level lạ.
4. Strategy pass `validate_strategy_template()` và chạy qua registry.
5. Export từ `smc.engine` và fresh-process import không circular dependency.

### B. Happy path và symmetry

6. BUY: bullish sweep low → bullish displacement MSS → bullish same-leg FVG → retest → đúng một candidate.
7. SELL mirror toàn bộ case BUY.
8. So sánh geometry, RR, expiry, evidence và metadata Long/Short theo phép phản chiếu giá.
9. Proximal entry đúng top/bottom; `ce_50` đúng midpoint.

### C. Missing/wrong evidence

10. Không sweep, không MSS hoặc không FVG → không candidate.
11. Sweep direction không khớp pool kind → reject.
12. Wrong mode từng evidence → reject.
13. Wrong FVG direction → reject.
14. `structure_leg_id` thiếu một phía hoặc mismatch → reject.
15. MSS wick break/non-close → reject.
16. MSS không displacement khi config yêu cầu → reject.

### D. Ordering và boundary

17. MSS trước/cùng sweep → reject.
18. FVG trước sweep → reject.
19. FVG index bằng MSS index hoặc sau MSS → reject.
20. `fvg.confirmed_at > mss.index` → reject/defer, không lookahead.
21. Sweep→MSS tại +19, +20 pass; +21 fail.
22. FVG→MSS lag 9, 10 pass; 11 fail.
23. Retest cùng bar MSS reject; bar MSS+1 pass.
24. Expiry tại `ready+14`, `ready+15` pass; `ready+16` fail.

### E. FVG lifecycle/no-lookahead

25. `filled_at < mss.index` reject.
26. `filled_at > N` trong context giả lập phải raise future-leak error.
27. FVG filled trước retest không còn trong active list vẫn invalidate nhờ OHLC state nội bộ.
28. Full-fill tại chính N + close-respect pass.
29. Touch tại N nhưng close xuyên bottom/top reject.
30. Không overlap zone → không candidate.
31. FVG bị context cap khỏi active list sau khi đã linked không làm mất lifecycle correctness.

### F. Structure/sweep invalidation

32. Opposite CHoCH sau MSS hủy.
33. Opposite BOS sau MSS hủy.
34. Opposite event chính bar retest thắng retest và hủy candidate.
35. Opposite event trước đoạn FVG→MSS hoặc chen giữa đoạn đó reject linkage.
36. BUY close dưới sweep extreme / SELL close trên sweep extreme hủy.
37. Same-direction structure event không hủy.

### G. Target, precision và RR

38. Chọn đúng opposing pool gần nhất bất kể input order.
39. Loại pool sai phía, sai kind, invalid, swept, future hoặc wrong mode.
40. Không có pool → fixed 2R.
41. Pool gần nhất RR <1.5 → fixed 2R, không nhảy sang pool xa hơn.
42. Post-rounding geometry collapse do mức giá thị trường quá sát → fail closed, retire narrative với internal reason và không emit; không để state partial.
43. `planned_rr` được tính lại từ ba mức đã round.
44. HTF aligned/neutral pass; opposed và missing fail closed.

### H. Duplicate, concurrency và deterministic output

45. Duplicate sweep/MSS/FVG trong tuple không tạo narrative/candidate trùng.
46. Hai sweep đồng thời được theo dõi độc lập.
47. Nhiều MSS/FVG hợp lệ chọn đúng tie-break deterministic.
48. Nhiều candidate cùng bar được sort canonical.
49. Một cluster retest nhiều bar chỉ emit lần đầu.
50. Same-bar identical retry trả output y hệt; conflicting retry raise.
51. Bar lùi hoặc bar gap raise trước mutation.
52. `reset()` rồi replay tạo output y hệt.

### I. Parity và regression

53. Feed context trực tiếp incremental.
54. Feed context batch từ `build_strategy_contexts()`.
55. JSON round-trip từng `StrategyContext` rồi replay bằng strategy mới.
56. So sánh toàn bộ `[candidate.to_dict()]`, không chỉ vài field.
57. Đổi thứ tự evidence input cho output serialized y hệt.
58. Kiểm tra deep immutability trước/sau evaluate.
59. Full Python regression, Node regression, `compileall`, `git diff --check`.

## 14. Thứ tự triển khai

### T53.4.1 — Skeleton và config

- Tạo package strategies, config strict, profile và exports.
- Viết tests A trước; chưa viết matching logic.

### T53.4.2 — Stable identities và state lifecycle

- Tạo internal state record, dedup keys, monotonic/idempotent cache, reset và atomic commit.
- Viết tests D/H cho lifecycle trước khi tạo candidate.

### T53.4.3 — Sweep/MSS/FVG matcher

- Implement một logic direction-neutral với helper map BUY/SELL, không copy-paste hai nhánh lớn.
- Khóa same-leg, ordering, lag, opposite-event guard và deterministic tie-break.
- Hoàn thành tests B/C/D/E linkage.

### T53.4.4 — Retest/invalidation

- Theo dõi FVG lifecycle từ OHLC sau linkage.
- Áp dụng expiry inclusive, close-respect, sweep-extreme và opposite shift.
- Hoàn thành tests E/F.

### T53.4.5 — Candidate builder

- Entry/SL/target/fallback/RR post-rounding.
- Evidence IDs, cluster/setup IDs và metadata.
- Hoàn thành tests G và serialized parity.

### T53.4.6 — Registry/replay integration và QC

- Chạy qua registry với context thật từ builder.
- Chạy targeted + full regression + quality checks.
- Cập nhật `.agent` và walkthrough theo kết quả thực tế.
- Chỉ chuyển T53.4 sang `done` khi independent QC không còn P0/P1.

## 15. Lệnh verification bắt buộc

```powershell
python -m unittest tests.test_smc_strategy_s01 -v
python -m unittest tests.test_smc_engine_registry tests.test_smc_engine_context_qc tests.test_smc_engine_models -v
python -m unittest discover tests -v
python -m compileall -q smc tests
node --test tests/test_drawings.test.js tests/test_ui_structure.test.js tests/test_drawer.test.js
git diff --check
```

Nếu môi trường Python/dependency không chạy được test, báo blocker thật; không tuyên bố PASS dựa trên compile-only.

## 16. Definition of Done / Gate C-S01

T53.4 chỉ đạt khi:

- [ ] Public strategy/config đúng protocol và import sạch.
- [ ] Sweep→MSS→FVG→retest đúng closed-bar, no-lookahead.
- [ ] Same-leg strict, event ordering và các boundary 20/10/15 pass.
- [ ] FVG fill/retest as-of, opposite structure và sweep-extreme invalidation pass.
- [ ] Entry/SL/target/RR đúng sau precision rounding.
- [ ] Long/Short symmetry pass.
- [ ] Multi-narrative, duplicate/idempotence, reset và atomicity pass.
- [ ] Batch/incremental/JSON replay parity so sánh toàn serialized candidate pass.
- [ ] Full regression không phát sinh lỗi mới.
- [ ] Independent QC kết luận không còn P0/P1.

Sau Gate C-S01 mới chuyển sang T53.5. Không triển khai trước selector/gate để bù cho lỗi trong S01.
