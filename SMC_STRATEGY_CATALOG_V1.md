# DANH MỤC 10 CHIẾN LƯỢC SMC/ICT CÓ KHẢ NĂNG CODE HÓA CAO

> Phiên bản: 1.0 — 2026-09-09  
> Trạng thái: Tài liệu nghiên cứu và đặc tả sơ bộ; chưa phải cam kết rằng chiến lược có lợi nhuận.  
> Mục đích: Làm nguồn tra cứu thống nhất trước khi thiết kế Multi-Strategy Confluence & Selection Engine.

---

## 1. Nguyên tắc sử dụng tài liệu

Các tên gọi SMC/ICT trên Internet không có một tiêu chuẩn kỹ thuật duy nhất. Tài liệu này chuẩn hóa chúng thành các chuỗi sự kiện có thể kiểm thử, nhưng mọi giá trị timeout, lookback, session, entry, SL và TP vẫn phải được chốt bằng ADR trước khi code production.

Mọi chiến lược trong danh mục phải tuân thủ:

- Chỉ dùng candle đã đóng.
- Tại bar `N`, chỉ dùng dữ liệu đã biết đến bar `N`.
- Lệnh sinh tại bar `N` chỉ được khớp sớm nhất từ Open bar `N+1`, trừ limit order đã được mô hình hóa riêng và vẫn không dùng dữ liệu tương lai.
- Swing chỉ khả dụng từ `confirmed_at`.
- Structure event, OB, FVG, liquidity pool và sweep phải còn hợp lệ tại thời điểm setup.
- Không dùng trạng thái cuối batch để quyết định tại một bar quá khứ.
- Batch, incremental và replay phải có cùng semantics.
- Mỗi setup phải có `strategy_id`, `setup_id`, direction, source IDs, timestamps, reason codes và expiry.
- `NO_TRADE` là kết quả hợp lệ.
- Không coi các tín hiệu dùng chung cùng một event là bằng chứng độc lập.

Tài liệu này mô tả logic đối xứng. Phần Long được ghi chi tiết; phần Short đảo toàn bộ direction, high/low, premium/discount và buy-side/sell-side liquidity.

---

## 2. Thành phần hệ thống hiện đã có

| Thành phần | Module hiện tại | Vai trò |
|---|---|---|
| Swing | `smc/structure/swings.py` | Swing/internal pivot và confirmation lag |
| BOS/CHoCH | `smc/structure/bos_choch.py` | Structure continuation/reversal và displacement |
| Order Block | `smc/zones/order_block.py` | Zone, mitigation, invalidation, structure leg |
| FVG | `smc/zones/fvg.py` | Imbalance, confirmation và fill lifecycle |
| Liquidity | `smc/liquidity/detector.py` | Equal highs/lows, pool và sweep |
| Session | `smc/context/session.py` | Asian/London/New York/Kill Zone, timezone và DST |
| HTF Bias | `smc/context/htf_bias.py` | Bias as-of timestamp, không nhìn HTF tương lai |
| Signal | `smc/models.py` | Đầu ra tín hiệu chuẩn hóa |

Các phần còn cần xây chung cho cả 10 chiến lược:

- `TradeSetup`.
- `StrategyProfile`.
- Strategy state machine/template.
- Setup expiry/cooldown.
- Deduplication và conflict resolution.
- Risk gate và target selection.
- Multi-strategy selector.

---

## 3. Bảng so sánh nhanh

| ID | Tên chiến lược | Họ chiến lược | Trigger lõi | Entry zone | Context quan trọng |
|---|---|---|---|---|---|
| S01 | ICT 2022 Reversal | Reversal | Sweep → MSS/CHoCH | FVG | HTF draw/bias |
| S02 | Sweep → CHoCH → OB | Reversal | Sweep → CHoCH | Order Block | HTF bias, location |
| S03 | Sweep → CHoCH → FVG | Reversal | Sweep → CHoCH | FVG | Displacement |
| S04 | Sweep → CHoCH → OB+FVG | Reversal | Sweep → CHoCH | OB/FVG overlap | Cùng structure leg |
| S05 | BOS → OB Retest | Continuation | BOS cùng bias | Order Block | Trend rõ |
| S06 | BOS → FVG Retest | Continuation | BOS/displacement | FVG | Trend rõ |
| S07 | FVG Continuation | Continuation | Displacement/FVG | FVG first retest | Bias và momentum |
| S08 | OB Continuation | Continuation | BOS tạo OB | Order Block | Bias và OB quality |
| S09 | ICT Silver Bullet | Time-based | Sweep → MSS | FVG | Cửa sổ thời gian |
| S10 | Asian Range Sweep | Session reversal | Sweep Asian H/L | FVG hoặc OB | London/New York |

---

## 4. Contract chung cho strategy template

Mỗi strategy nên nhận một snapshot bất biến:

```python
StrategyContext(
    bar_index,
    timestamp,
    candle,
    htf_bias,
    session,
    structure_events,
    order_blocks,
    fvgs,
    liquidity_pools,
    liquidity_sweeps,
)
```

Mỗi template trả về `CandidateSetup` hoặc lý do không tạo setup:

```python
CandidateSetup(
    strategy_id,
    setup_id,
    direction,
    signal_bar,
    available_at,
    entry_zone,
    stop_anchor,
    target_candidates,
    expires_at,
    required_evidence,
    optional_evidence,
    source_ids,
    reason_codes,
)
```

State machine tổng quát:

```text
IDLE
  → CONTEXT_ELIGIBLE
  → TRIGGER_DETECTED
  → STRUCTURE_CONFIRMED
  → ENTRY_ZONE_READY
  → ORDER_PENDING
  → FILLED | EXPIRED | INVALIDATED | CANCELLED
```

Không bắt buộc mọi strategy dùng đủ mọi state, nhưng transition phải đơn hướng, timestamp rõ và deterministic.

---

## 5. S01 — ICT 2022 Reversal

### Ý tưởng

Giá hướng đến một vùng thanh khoản, quét vùng đó, đảo chiều bằng displacement phá internal structure và tạo FVG. Entry chờ retracement vào FVG theo hướng mới. Đây là sequence gần với mô hình được trình bày trong ICT 2022 Mentorship về FVG, market structure shift và daily bias.

### Chuỗi Long đề xuất

```text
HTF bias/draw bullish hoặc cho phép reversal tại HTF discount
→ Sell-side liquidity pool tồn tại
→ Giá sweep xuống dưới pool và close quay lại
→ Bullish displacement
→ Bullish MSS/CHoCH phá internal swing high
→ Bullish FVG thuộc displacement leg
→ Giá retrace vào FVG
→ Long, target buy-side liquidity
```

### Bắt buộc

- Sweep xảy ra trước MSS/CHoCH.
- MSS/CHoCH được xác nhận bằng close, không dùng wick-only break.
- FVG được xác nhận sau sweep và liên quan displacement/MSS leg.
- Entry không xảy ra trước `fvg.confirmed_at`.
- Target liquidity đối diện phải tồn tại và cho RR hợp lệ.

### Tùy chọn tăng chất lượng

- HTF bias aligned.
- Kill Zone.
- FVG nằm trong hoặc overlap bullish OB.
- Entry ở discount.
- Displacement body/ATR vượt ngưỡng.

### Invalidation/expiry

- Giá close xuyên sweep extreme trước khi entry.
- Opposite MSS xuất hiện.
- FVG bị fill/invalid theo policy trước khi đặt lệnh.
- Quá `max_entry_delay_bars` kể từ MSS hoặc FVG confirmation.

### Tham số cần chốt

- `sweep_to_mss_max_bars`.
- `mss_to_fvg_max_bars`.
- `entry_expiry_bars`.
- FVG entry: proximal, midpoint hay full fill.
- SL: sweep extreme hay FVG/OB extreme.
- Target: nearest external liquidity hay RR cố định.

---

## 6. S02 — Sweep → CHoCH → Order Block

### Ý tưởng

Liquidity sweep cung cấp nguyên nhân đảo chiều, CHoCH xác nhận structure đổi hướng, và Order Block của displacement leg làm vùng entry.

### Chuỗi Long đề xuất

```text
Sell-side liquidity pool
→ Bullish sweep
→ Bullish CHoCH
→ Chọn bullish OB tạo ra CHoCH
→ OB còn valid và chưa mitigation vượt ngưỡng
→ Giá retest OB
→ Long
```

### Bắt buộc

- Sweep phải xảy ra trước CHoCH trong cửa sổ cấu hình.
- OB phải có `source_event_index` trỏ về CHoCH hoặc displacement leg được chấp nhận.
- OB còn valid tại signal bar.
- Không dùng OB được tạo hoặc invalid ở tương lai.

### Tùy chọn

- OB có FVG cùng structure leg.
- HTF bias aligned.
- Kill Zone.
- OB chưa từng retest hoặc `retest_count` dưới ngưỡng.

### Invalidation/expiry

- OB close-break invalidation.
- Opposite structure shift.
- Sweep quá cũ.
- Retest sâu hoặc số lần retest vượt policy.

### Quyết định còn mở

- Full-candle OB hay refined zone.
- Cho phép entry ở retest thứ mấy.
- CHoCH bắt buộc displacement hay không.
- SL ngoài OB hay ngoài sweep extreme.

---

## 7. S03 — Sweep → CHoCH → FVG

### Ý tưởng

Giống S02 nhưng dùng FVG làm vùng entry. Phù hợp khi displacement rõ nhưng source OB quá rộng hoặc không thuận lợi cho RR.

### Chuỗi Long đề xuất

```text
Sell-side liquidity sweep
→ Bullish CHoCH có displacement
→ Bullish FVG được tạo trong displacement leg
→ FVG còn khả dụng
→ Giá retrace vào FVG
→ Long
```

### Bắt buộc

- Đúng event ordering.
- FVG cùng direction/mode/structure leg.
- FVG chưa fill trước thời điểm setup.
- CHoCH và FVG đều đã confirmed.

### Tùy chọn

- HTF bias.
- Kill Zone.
- FVG overlap OB.
- FVG size/ATR nằm trong khoảng hợp lệ.

### Invalidation/expiry

- FVG bị fill theo threshold đã chốt trước entry.
- Close phá sweep extreme.
- Opposite CHoCH.
- Entry đến quá muộn.

### Quyết định còn mở

- Fill threshold 50% hay 100%.
- Entry tại FVG boundary hay consequent encroachment 50%.
- Chọn FVG đầu tiên, gần nhất hay rank theo quality.

---

## 8. S04 — Sweep → CHoCH → OB + FVG Overlap

### Ý tưởng

Phiên bản confluence chặt của S02/S03. Entry chỉ hợp lệ khi OB và FVG của cùng displacement leg overlap về giá.

### Chuỗi Long đề xuất

```text
Sell-side sweep
→ Bullish CHoCH/displacement
→ Bullish OB và bullish FVG cùng structure leg
→ Tính vùng giao nhau OB ∩ FVG
→ Giá retest overlap zone
→ Long
```

### Bắt buộc

- `OB.direction == FVG.direction == setup.direction`.
- Cùng `structure_leg_id` hoặc cùng source event theo contract rõ ràng.
- Vùng overlap có chiều cao dương.
- Cả OB và FVG còn hợp lệ tại setup bar.

### Tùy chọn

- Premium-candidate OB.
- HTF bias aligned.
- Sweep external liquidity.
- Kill Zone.

### Invalidation/expiry

- Một trong hai zone invalid trước entry.
- Overlap không còn hợp lệ theo fill/mitigation policy.
- Opposite structure event chen giữa.

### Lưu ý deduplication

S04 có thể cùng lúc thỏa S02 và S03. Đây phải là một `SetupCluster`, không phải ba lệnh và không được cộng lặp sweep/CHoCH.

---

## 9. S05 — BOS → Order Block Retest

### Ý tưởng

Chiến lược continuation: xu hướng HTF rõ, giá tạo BOS cùng hướng và quay lại Order Block đã tạo displacement/BOS.

### Chuỗi Long đề xuất

```text
HTF bias bullish
→ Bullish BOS
→ Bullish OB liên kết BOS
→ Pullback nhưng chưa có bearish CHoCH hợp lệ
→ Retest OB còn valid
→ Long theo continuation
```

### Bắt buộc

- BOS cùng HTF bias.
- OB liên kết đúng BOS/structure leg.
- Không có opposite CHoCH sau BOS trước entry.
- OB còn valid.
- RR đến external buy-side liquidity đạt ngưỡng.

### Tùy chọn

- BOS có displacement.
- OB có FVG cùng leg.
- Entry ở discount của impulse leg.
- Pullback quét internal sell-side liquidity.

### Invalidation/expiry

- Bearish CHoCH/MSS.
- Close dưới bullish OB.
- HTF bias đổi bearish/neutral theo policy.
- Pullback kéo dài quá giới hạn.

---

## 10. S06 — BOS → FVG Retest

### Ý tưởng

Continuation entry tại FVG do displacement tạo BOS để lại.

### Chuỗi Long đề xuất

```text
HTF bias bullish
→ Bullish BOS có displacement
→ Bullish FVG cùng BOS leg
→ Pullback vào FVG
→ Không có bearish CHoCH
→ Long
```

### Bắt buộc

- Bias và BOS cùng hướng.
- FVG thuộc BOS leg và được confirmed.
- FVG chưa invalid/fill trước khi setup khả dụng.
- Không dùng FVG hình thành sau cửa sổ BOS cho phép.

### Tùy chọn

- FVG overlap OB.
- Pullback vào discount.
- Kill Zone/session expansion.
- FVG size phù hợp ATR.

### Invalidation/expiry

- Opposite CHoCH.
- FVG lifecycle hết hiệu lực.
- HTF bias đổi hướng.
- Không còn target liquidity hợp lệ.

---

## 11. S07 — FVG Continuation

### Ý tưởng

Phiên bản continuation gọn hơn S06: không bắt buộc BOS mới ở ngay trước FVG, nhưng cần bias và displacement đủ mạnh. Dùng để bắt first retracement trong một trend đang chạy.

### Chuỗi Long đề xuất

```text
HTF/LTF trend bullish
→ Bullish displacement
→ Bullish FVG
→ FVG đạt quality filter
→ First valid retracement
→ Long
```

### Bắt buộc

- Bias bullish tại `confirmed_at` và signal bar.
- FVG do displacement, không phải gap nhiễu nhỏ.
- Không có bearish CHoCH trước retest.
- Chỉ dùng lần retest/fill được policy cho phép.

### Tùy chọn

- BOS trong lookback gần.
- FVG ở discount.
- Session filter.
- External liquidity target.

### Rủi ro thiết kế

Nếu điều kiện trend/displacement quá lỏng, chiến lược trở thành “trade mọi FVG”. Vì vậy cần minimum displacement, trend persistence và target-distance filter.

---

## 12. S08 — Order Block Continuation

### Ý tưởng

Trade pullback về OB trong xu hướng đang tiếp diễn. Khác S05 ở chỗ BOS có thể nằm trong lookback thay vì là trigger ngay lập tức.

### Chuỗi Long đề xuất

```text
HTF trend bullish
→ Bullish OB chất lượng cao còn active
→ Không có bearish structure shift kể từ OB source event
→ Giá pullback/retest OB
→ Long
```

### Bắt buộc

- OB có nguồn structure rõ ràng.
- OB còn valid tại bar hiện tại.
- Bias cùng hướng.
- Không vượt age/retest limit.

### Tùy chọn

- `quality` strong/premium candidate.
- FVG cùng leg.
- Internal liquidity sweep trước retest.
- Session filter.

### Invalidation/expiry

- Close-break OB.
- Opposite CHoCH.
- Quá `max_ob_age_bars`.
- `retest_count` hoặc `mitigation_pct` vượt ngưỡng.

### Rủi ro thiết kế

S05 và S08 có overlap lớn. Khi cùng phát setup, selector phải gom chung source OB/event và chỉ giữ một candidate.

---

## 13. S09 — ICT Silver Bullet

### Ý tưởng

Mô hình time-based: trong cửa sổ thời gian định trước, chờ liquidity raid, displacement/MSS và retracement vào FVG. Thời gian là điều kiện bắt buộc, không chỉ là điểm cộng.

### Chuỗi Long đề xuất

```text
Đang trong Silver Bullet window
→ Sell-side liquidity bị sweep
→ Bullish displacement/MSS
→ Bullish FVG hình thành trong window
→ Retracement vào FVG trước expiry
→ Long, target buy-side liquidity
```

### Bắt buộc

- Session timezone và DST được cấu hình rõ.
- Sweep, MSS và FVG tuân thủ chính sách thời gian đã chốt.
- FVG entry phải xảy ra trong window hoặc grace period rõ ràng.
- Không lấy một FVG cũ trước cửa sổ nếu policy không cho phép.

### Tùy chọn

- Daily/HTF bias aligned.
- Previous session high/low làm liquidity target.
- FVG overlap OB.
- Minimum displacement.

### Invalidation/expiry

- Hết time window/grace period.
- Opposite MSS.
- Sweep extreme bị phá.
- FVG invalid trước entry.

### Quyết định còn mở

- Những window nào được hỗ trợ và timezone chuẩn.
- Setup phải hoàn thành toàn bộ trong window hay chỉ trigger trong window.
- Mỗi window tối đa bao nhiêu setup/lệnh.

---

## 14. S10 — Asian Range Sweep

### Ý tưởng

Asian session tạo range tham chiếu. London hoặc New York quét một biên range, sau đó structure shift và retracement tạo entry về hướng đối diện.

### Chuỗi Long đề xuất

```text
Asian session đóng và range được khóa
→ London/NY sweep Asian low
→ Candle close quay lại trên Asian low
→ Bullish CHoCH/MSS
→ Bullish FVG hoặc bullish OB
→ Long, target Asian high hoặc external buy-side liquidity
```

### Bắt buộc

- Asian range chỉ khóa sau khi session kết thúc.
- Không cập nhật lại Asian high/low sau khi đã khóa.
- Sweep xảy ra trong London/NY window được phép.
- MSS và entry zone được xác nhận sau sweep.
- Target và RR hợp lệ.

### Tùy chọn

- Daily bias aligned.
- Sweep trùng PDH/PDL hoặc equal highs/lows.
- OB/FVG overlap.
- London hoặc New York profile riêng.

### Invalidation/expiry

- Giá close tiếp diễn ngoài Asian range theo hướng breakout và không reclaim.
- Quét cả hai phía theo ambiguous policy.
- Không có MSS trước hết cửa sổ.
- Sang trading day/session mới.

### Thành phần cần bổ sung

SessionFilter đã có, nhưng cần model `SessionRange` bất biến chứa:

- Session start/end.
- High/low và source bar indices.
- `locked_at`.
- Trading-date attribution cho session qua midnight.
- Swept side và lifecycle.

---

## 15. Quan hệ và chống trùng giữa 10 chiến lược

Các nhóm dễ phát hiện cùng một setup:

| Nhóm overlap | Cách xử lý đề xuất |
|---|---|
| S01, S03 | Nếu cùng sweep/MSS/FVG thì gom một cluster; S01 là narrative đầy đủ hơn |
| S02, S03, S04 | S04 không tạo lệnh thứ ba; dùng overlap làm quality của cùng setup |
| S05, S08 | Gom theo source OB và BOS leg |
| S06, S07 | Nếu cùng FVG thì S06 bổ sung evidence BOS cho S07 |
| S09 với S01/S03 | Silver Bullet là time-qualified variant, không phải evidence hoàn toàn độc lập |
| S10 với S01/S02/S03 | Asian range xác định source liquidity/session, không tạo lệnh trùng |

Khóa deduplication đề xuất:

```text
(direction, liquidity_sweep_id, structure_event_id, entry_zone_id, signal_window)
```

Nếu hai template dùng cùng ba source ID đầu tiên, chúng phải được xem là cùng một cơ hội giao dịch.

---

## 16. Regime phù hợp sơ bộ

| Strategy | Trend | Range | Reversal forming | Session expansion |
|---|---:|---:|---:|---:|
| S01 ICT 2022 | Trung bình | Cao | Rất cao | Cao |
| S02 Sweep/CHoCH/OB | Thấp | Cao | Rất cao | Cao |
| S03 Sweep/CHoCH/FVG | Thấp | Cao | Rất cao | Cao |
| S04 OB+FVG overlap | Thấp | Cao | Rất cao | Cao |
| S05 BOS/OB | Rất cao | Thấp | Thấp | Cao |
| S06 BOS/FVG | Rất cao | Thấp | Thấp | Cao |
| S07 FVG Continuation | Cao | Thấp | Thấp | Cao |
| S08 OB Continuation | Cao | Thấp | Trung bình | Trung bình |
| S09 Silver Bullet | Trung bình | Trung bình | Cao | Rất cao |
| S10 Asian Range Sweep | Thấp | Cao | Cao | Rất cao |

Bảng trên là giả thuyết thiết kế để backtest, không phải kết luận hiệu suất.

---

## 17. Bộ test chung bắt buộc

Mỗi strategy cần tối thiểu:

1. Long/short symmetry.
2. Happy path tạo đúng một setup.
3. Thiếu từng điều kiện bắt buộc thì không có setup.
4. Event ordering sai thì reject.
5. Source event tương lai thì reject.
6. Zone invalid tại signal bar thì reject.
7. Expiry boundary inclusive/exclusive được chốt rõ.
8. Duplicate event không tạo setup mới.
9. Hai strategy cùng evidence được cluster, không mở hai lệnh.
10. Batch/incremental/replay parity qua toàn bộ `to_dict()`.
11. Input immutability.
12. Signal bar `N` chỉ thực thi từ bar `N+1`.
13. Spread/commission/slippage làm RR dưới ngưỡng thì reject.
14. Không có target hợp lệ thì `NO_TRADE`.
15. Benchmark với cả normal và worst-case active candidates.

---

## 18. Thứ tự triển khai đề xuất

### Wave 1 — Ba họ chiến lược đại diện

1. S01 ICT 2022 Reversal.
2. S05 BOS → OB Retest.
3. S09 Silver Bullet.

Ba strategy này kiểm chứng ba dạng logic: reversal sequence, trend continuation và time-based setup.

### Wave 2 — Các biến thể dùng module hiện có

4. S03 Sweep → CHoCH → FVG.
5. S02 Sweep → CHoCH → OB.
6. S06 BOS → FVG Retest.
7. S10 Asian Range Sweep.

### Wave 3 — Confluence chặt và generic continuation

8. S04 OB + FVG overlap.
9. S07 FVG Continuation.
10. S08 OB Continuation.

Sau mỗi wave phải backtest độc lập và kiểm tra overlap trước khi thêm chiến lược tiếp theo.

---

## 19. Nguồn tham khảo

Nguồn gốc và diễn giải nền tảng:

- ICT 2022 Mentorship Episode 6 — FVG và Market Structure Shift: https://www.youtube.com/watch?v=Bkt8B3kLATQ
- ICT 2022 Mentorship Episode 7 — Daily Bias: https://www.youtube.com/watch?v=G8-z91acgG4
- ICT Silver Bullet Time Based Trading Model: https://www.youtube.com/watch?v=tRq1hyGGtl4
- Liquidity → MSS sequence overview: https://liquidityscan.io/blog/liquidity-sweep-then-structure-shift-the-stop-hunt-to-reversal-sequence
- SMC sequence overview: https://candlune.com/setup/smart-money-concept
- SMC market structure, liquidity, OB, FVG, premium/discount và AMD: https://tradingwyckoff.com/en/smart-money-concepts/

Các nguồn trên được dùng để tham khảo thuật ngữ và sequence. Codebase phải sử dụng semantics đã chốt trong `.agent/DECISIONS.md`, không phụ thuộc vào tên gọi hoặc tuyên bố lợi nhuận của bất kỳ nguồn nào.

---

## 20. Câu hỏi phải chốt trước khi code T53

1. Sweep nào được chấp nhận: equal highs/lows, swing, session high/low, PDH/PDL?
2. MSS và CHoCH có được coi tương đương trong từng strategy không?
3. Structure event phải có displacement hay chỉ là filter tăng điểm?
4. Khoảng thời gian tối đa giữa sweep → structure → zone → entry?
5. Zone entry dùng touch, midpoint, close confirmation hay limit?
6. FVG fill threshold và OB mitigation threshold?
7. SL ưu tiên sweep extreme, OB, FVG hay swing?
8. Target ưu tiên liquidity hay RR cố định?
9. Khi nhiều strategy overlap, strategy nào sở hữu trade record?
10. Mỗi strategy được phép bao nhiêu setup/lệnh trong một session?
11. Có cho counter-trend hay không?
12. Regime là eligibility gate hay chỉ là điểm số?

Chỉ sau khi các câu hỏi này được chốt mới chuyển từng mô tả thành acceptance criteria và code production.
