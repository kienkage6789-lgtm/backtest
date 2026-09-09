# PLAN TRIỂN KHAI ORDER BLOCK

## 1. Mục tiêu

Xây dựng module Order Block event-driven trên nền SwingPoint, StructureEvent BOS/CHoCH và FVG.

Định nghĩa chính thức:

- Order Block là cây nến cuối cùng đi ngược hướng ngay trước displacement leg tạo BOS hoặc CHoCH.
- Strong Order Block là Order Block có FVG cùng hướng trong cùng displacement/structure leg.
- OB chỉ là vùng giá và context; không tự quyết định entry.

## 2. Phạm vi

Trong phase:

- Bullish và bearish OB.
- Liên kết OB với StructureEvent.
- Tìm candle nguồn.
- Liên kết OB với FVG cùng hướng.
- Phân loại base/strong/premium-candidate.
- Mitigation tracking.
- Invalidation tracking.
- Swing/internal OB.
- Batch detector.
- Incremental tracker.
- Serialization, chart payload và test.

Chưa làm:

- Breaker Block.
- Mitigation Block nâng cao.
- Propulsion Block.
- Machine learning ranking.
- Tự động vào lệnh.

## 3. Định nghĩa nghiệp vụ

### Bullish OB

Bullish OB là bearish candle cuối cùng trước bullish BOS hoặc bullish CHoCH.

Chuỗi:

- bearish candle;
- bullish displacement;
- bullish FVG;
- bullish BOS/CHoCH.

Vùng v1 là toàn bộ high-low của candle nguồn.

### Bearish OB

Bearish OB là bullish candle cuối cùng trước bearish BOS hoặc bearish CHoCH.

Vùng v1 cũng là toàn bộ high-low của candle nguồn.

### OB không phải entry

Không tạo entry chỉ vì giá chạm OB. Entry có thể cần HTF bias, liquidity sweep, structure confirmation, Kill Zone và risk/reward.

## 4. Phân loại chất lượng

### Base OB

- Có StructureEvent hợp lệ.
- Tìm được candle đối hướng trước event.
- Candle nằm trong lookback.
- OB chưa invalid.

### Strong OB

Base OB cộng:

- FVG cùng direction.
- FVG cùng mode.
- FVG được tạo sau OB.
- FVG được tạo trước hoặc tại StructureEvent.
- FVG thuộc cùng displacement/structure leg.
- Không có structure event ngược hướng chen giữa.

### Premium candidate

Strong OB cộng:

- StructureEvent có displacement.
- Có liquidity sweep trước event.
- HTF bias cùng hướng.
- OB chưa bị mitigation nhiều.

Ở phase này chỉ gắn metadata premium-candidate; không tự dùng để vào lệnh.

## 5. Tiêu chí OB có FVG

FVG chỉ liên kết với OB nếu:

1. Cùng hướng.
2. Cùng mode.
3. FVG.index lớn hơn OB.index.
4. FVG.index nhỏ hơn hoặc bằng StructureEvent.index nếu policy yêu cầu.
5. Khoảng cách nằm trong fvg_lookback.
6. FVG thuộc cùng structure leg.
7. FVG chưa invalid trước event.

V1 chọn FVG đầu tiên cùng hướng sau OB và trước event.

FVG không bắt buộc chồng trực tiếp lên OB. FVG chỉ cần thuộc cùng displacement leg. Overlap là metadata nâng cao.

Tham số mặc định:

- fvg_lookback = 5;
- require_fvg_before_event = True;
- require_same_mode = True;
- require_same_direction = True.

## 6. Data model

Mở rộng OrderBlock:

- index, time;
- direction;
- high, low, open, close;
- origin_type;
- mode;
- source_event_index;
- source_event_type;
- source_swing_index;
- quality: base, strong hoặc premium_candidate;
- source_fvg_index;
- source_fvg_top;
- source_fvg_bottom;
- mitigated;
- mitigated_at;
- mitigation_pct;
- valid;
- invalidated_at;
- invalidation_reason;
- retest_count.

Thêm to_dict() JSON-safe.

Không lưu object FVG trực tiếp trong OrderBlock; chỉ lưu index và range để tránh reference mutation.

## 7. File và API

Tạo:

- smc/zones/order_block.py
- smc/zones/fvg.py nếu FVG chưa tồn tại
- smc/zones/__init__.py
- tests/test_smc_order_block.py
- tests/test_smc_fvg.py nếu cần

Batch API:

detect_order_blocks(
    data,
    structure_events,
    fvgs=None,
    mode="swing",
    ob_lookback=20,
    fvg_lookback=5,
    require_fvg=False,
    mitigation_mode="wick",
    zone_mode="full_candle"
)

Incremental API:

OrderBlockTracker.update(candle, new_structure_events, new_fvgs)

Tracker phải có:

- get_active_blocks();
- get_all_blocks();
- event/state history;
- deduplication theo source event và source candle.

## 8. Thuật toán tạo OB

### Bullish event

1. Nhận bullish BOS hoặc CHoCH đã xác nhận.
2. Lấy event bar index.
3. Quét ngược tối đa ob_lookback.
4. Tìm bearish candle gần nhất với close < open.
5. Candle phải nằm trước event.
6. Tạo bullish OB từ full candle high-low.
7. Gắn source event và source swing.
8. Tìm bullish FVG trong cùng leg.
9. Gắn quality base hoặc strong.
10. Không tạo nếu đã tồn tại cùng source event/source candle.

### Bearish event

Làm đối xứng:

- tìm bullish candle với close > open;
- tạo bearish OB;
- tìm bearish FVG;
- gắn source event và quality.

Nếu không tìm thấy candle đối hướng, không tạo OB và ghi debug reason no_opposite_candle_within_lookback.

## 9. Zone policy

V1 dùng full candle:

- zone.low = candle.low;
- zone.high = candle.high.

Không triển khai refined zone trước khi full candle zone được verify.

V2 có thể hỗ trợ:

- bullish: low đến open;
- bearish: open đến high.

## 10. Mitigation

Mỗi candle sau khi OB được tạo phải cập nhật:

- có chạm vùng hay không;
- mitigation_pct;
- mitigated_at;
- retest_count.

V1 dùng wick mode.

Bullish:

- zone_height = high - low;
- penetration = high - candle.low;
- mitigation_pct = clamp(penetration / zone_height, 0, 1).

Bearish:

- penetration = candle.high - low;
- mitigation_pct = clamp(penetration / zone_height, 0, 1).

Zone height bằng 0 phải xử lý riêng, không được chia cho 0.

Mitigation không làm OB invalid ngay. OB vẫn valid nếu chưa bị close phá toàn vùng.

## 11. Invalidation

Bullish OB invalid khi:

- close < OB.low.

Bearish OB invalid khi:

- close > OB.high.

Wick xuyên vùng chưa đủ invalid trong v1.

Khi invalid:

- valid = False;
- invalidated_at = current_bar_index;
- invalidation_reason = close_break.

Không xóa OB khỏi history; chỉ loại khỏi active blocks.

## 12. Lifecycle và nhiều OB

Lifecycle:

- created;
- active;
- mitigated;
- retested;
- invalidated hoặc expired.

V1 chưa cần expiry; nếu thêm thì max_active_bars = 0 nghĩa là không expiry.

Có thể có nhiều OB cùng hướng. Giới hạn mặc định max_active_blocks = 20.

Không xóa block còn valid chỉ để giữ giới hạn. Nếu vượt giới hạn, ưu tiên loại block cũ đã invalid hoặc expired.

## 13. Batch detector

Batch detector phải deterministic và không mutate input.

Nếu nhận StructureEvent hoặc FVG từ caller:

- clone hoặc dùng local state;
- không sửa valid, mitigated hoặc broken của input;
- gọi hai lần với cùng input phải cho kết quả giống nhau.

Quy trình:

1. Normalize OHLCV.
2. Sort events và FVG theo index.
3. Tạo OB từ từng event.
4. Liên kết FVG.
5. Cập nhật mitigation từng bar sau creation.
6. Cập nhật invalidation.
7. Trả history và active blocks.

## 14. Incremental tracker

OrderBlockTracker phải xử lý mỗi candle một lần:

1. Nhận candle đã đóng.
2. Nhận event mới.
3. Nhận FVG mới.
4. Tạo OB mới.
5. Liên kết FVG hiện có.
6. Cập nhật mitigation active blocks.
7. Cập nhật invalidation.
8. Emit state change.

Không gọi batch detector trên toàn bộ lịch sử trong mỗi update.

Batch và incremental bắt buộc có parity test.

## 15. No-lookahead

Bắt buộc:

- Không tạo OB trước StructureEvent confirmation.
- Không dùng candle sau current bar để tìm source candle.
- Không dùng FVG chưa confirmed.
- Không dùng FVG sau event nếu require_fvg_before_event.
- Mitigation chỉ tính từ candle sau khi OB được tạo.
- Invalidation chỉ tính đến current bar.
- event_index và OB creation/availability index phải tách riêng.

Trong Replay, OB candle có thể đã xuất hiện trước, nhưng OB chỉ được công bố khi StructureEvent đã xác nhận.

## 16. Chart/debug payload

Mỗi OB cần serialize:

- source index/time;
- high, low, open, close;
- direction;
- mode;
- quality;
- source event index/type;
- source FVG index/range;
- mitigated;
- mitigation_pct;
- valid;
- invalidated_at;
- retest_count.

UI nên bật/tắt độc lập:

- swing bullish/bearish OB;
- internal bullish/bearish OB;
- base OB;
- strong OB;
- invalidated OB.

Strong OB nên có border hoặc opacity khác base OB.

## 17. Test plan

Tạo tests/test_smc_order_block.py.

Creation:

1. Bullish event chọn đúng bearish candle cuối.
2. Bearish event chọn đúng bullish candle cuối.
3. Không chọn candle sau event.
4. Không tìm thấy candle đối hướng thì không tạo OB.
5. ob_lookback giới hạn đúng.
6. Swing/internal isolation.
7. Source event và source candle đúng.
8. Full candle zone đúng.
9. Duplicate event không tạo duplicate OB.

FVG strength:

10. Không có FVG = base.
11. FVG cùng hướng = strong.
12. FVG khác hướng không nâng quality.
13. FVG sau event bị loại khi policy yêu cầu trước event.
14. FVG khác mode không liên kết.
15. FVG ngoài lookback không liên kết.
16. FVG + displacement = premium-candidate.
17. Overlap metadata đúng.

Mitigation:

18. Chưa chạm = 0.
19. Chạm một phần = pct trong 0..1.
20. Đi xuyên toàn vùng = 1.
21. Không mitigation trước creation.
22. Retest count tăng đúng.
23. Zone height bằng 0 không crash.

Invalidation:

24. Bullish close dưới low = invalid.
25. Bearish close trên high = invalid.
26. Wick phá nhưng close chưa vượt = vẫn valid.
27. invalidated_at đúng.
28. Invalid OB không nằm trong active blocks.

Determinism/no-lookahead:

29. Batch gọi hai lần cho cùng kết quả.
30. Input events/FVG không bị mutate.
31. Event chưa confirmed không tạo OB.
32. FVG chưa confirmed không nâng quality.
33. Replay không công bố OB trước event.
34. Batch và incremental parity.

Performance:

35. 10.000 bars incremental đạt benchmark.
36. Active block count được giới hạn.
37. Không tăng theo O(N²).

## 18. Synthetic datasets

### Base bullish OB

Bearish candle, bullish move, bullish BOS, không có FVG = base bullish OB.

### Strong bullish OB

Bearish OB candle, bullish displacement, bullish FVG, bullish BOS = strong bullish OB.

### Strong bearish OB

Bullish OB candle, bearish displacement, bearish FVG, bearish CHoCH = strong bearish OB.

### Wrong-direction FVG

Bullish OB và bullish BOS nhưng FVG bearish không liên quan = vẫn base OB.

### Invalidation

OB được tạo, giá retest, close phá zone ngược hướng = invalid.

### Replay delay

OB candle xuất hiện nhưng chưa có BOS = chưa công bố OB. Khi BOS được xác nhận = OB mới được tạo.

## 19. Integration với Confluence

Tín hiệu đề xuất:

- price_in_valid_ob;
- price_in_strong_ob;
- ob_has_fvg;
- ob_displacement_confirmed;
- ob_mitigated;
- ob_quality.

Nếu price_in_strong_ob đã bao gồm ob_has_fvg, phải tránh double-count trong scoring.

OB không tự quyết định entry.

## 20. Integration với BacktestEngine

Khi tích hợp:

- entry vẫn do BacktestEngine xử lý;
- không vào lệnh ngay tại bar tạo OB;
- setup chỉ xét khi giá retest sau creation;
- SL có thể đặt ngoài OB trong Risk module;
- trade record lưu source event, source OB, quality, source FVG và mitigation_pct.

## 21. Performance requirement

Incremental tracker phải:

- xử lý mỗi candle một lần;
- giữ active blocks bằng cấu trúc giới hạn;
- chỉ cập nhật OB còn active;
- không scan toàn bộ lịch sử mỗi bar;
- benchmark 10.000 bars dưới 1 giây;
- batch và incremental cho cùng kết quả.

## 22. Definition of Done

OB phase hoàn thành khi:

- Có base bullish/bearish OB.
- Strong OB cần FVG cùng hướng trong cùng displacement leg.
- Có source StructureEvent và source FVG.
- Có full candle zone baseline.
- Có mitigation_pct.
- Có invalidation theo close.
- Wick-only không invalid.
- Có swing/internal mode.
- Batch deterministic và không mutate input.
- Có incremental tracker.
- Không lookahead.
- Batch/incremental parity.
- Có chart/debug JSON.
- Có tối thiểu 37 test chính.
- 78 Python regression tests vẫn PASS.
- Có benchmark incremental.
- Chưa triển khai Breaker Block trước khi OB v1 được verify bằng chart.

## 23. Thứ tự triển khai

1. Chốt schema OrderBlock và quality policy.
2. Implement hoặc hoàn thiện FVG detector tối thiểu.
3. Implement batch OB creation từ StructureEvent.
4. Implement liên kết FVG và Strong OB.
5. Implement mitigation/invalidation.
6. Viết unit tests.
7. Implement incremental OrderBlockTracker.
8. Viết batch/incremental parity.
9. Thêm serialization và chart payload.
10. Benchmark.
11. Chạy full regression.
12. Verify trực quan trên chart.
13. Chỉ sau đó mới triển khai Confluence hoặc Breaker Block.

