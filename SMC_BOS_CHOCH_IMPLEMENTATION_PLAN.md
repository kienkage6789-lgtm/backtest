# PLAN TRIỂN KHAI BOS / CHoCH

## 1. Mục tiêu

Xây dựng module BOS/CHoCH độc lập trên nền SwingPoint và SwingDetectorState hiện có.

Module phải:

- Theo dõi trend state theo thời gian.
- Phát hiện bullish/bearish BOS và CHoCH.
- Chỉ dùng swing đã được xác nhận.
- Chỉ xác nhận break bằng giá đóng cửa.
- Không phát lại cùng một break.
- Hỗ trợ độc lập swing structure và internal structure.
- Chạy được cho batch backtest, Replay và realtime.
- Trả về StructureEvent cho Order Block, Confluence và chart.

Nguồn tham chiếu hành vi là logic LuxAlgo đã kiểm tra: pivot có cờ crossed, break bằng crossover/crossunder của close, trend bias quyết định BOS hay CHoCH. Chỉ kế thừa hành vi cần thiết, không sao chép nguyên văn source.

## 2. Phạm vi

### Trong phase này

- State machine xu hướng.
- Theo dõi swing high/low chưa bị phá.
- BOS bullish/bearish.
- CHoCH bullish/bearish.
- Close confirmation.
- Displacement flag.
- Swing/internal structure.
- Event deduplication.
- Batch và incremental API.
- JSON debug payload.

### Chưa làm

- Order Block.
- Breaker Block.
- FVG.
- Liquidity Sweep.
- HTF bias.
- Confluence scoring.
- Tự động vào lệnh.

Các module sau chỉ đọc StructureEvent, không tự tính lại BOS/CHoCH.

## 3. Quy ước nghiệp vụ

### BOS

BOS là break cùng hướng trend:

- trend bullish + close phá swing high = bullish BOS.
- trend bearish + close phá swing low = bearish BOS.

### CHoCH

CHoCH là break ngược hướng trend:

- trend bullish + close phá swing low = bearish CHoCH.
- trend bearish + close phá swing high = bullish CHoCH.

### Trend chưa khởi tạo

Nếu trend chưa có giá trị, break đầu tiên được coi là BOS theo hướng break. Không gắn CHoCH cho event đầu tiên.

### Close strict

- Bullish break: close phải lớn hơn swing high.
- Bearish break: close phải nhỏ hơn swing low.
- Close đúng bằng mức swing không tạo break.
- Wick vượt mức nhưng close quay lại không tạo BOS/CHoCH. Tình huống đó để Liquidity Sweep xử lý về sau.

## 4. File và kiến trúc

Tạo:

- smc/structure/bos_choch.py
- smc/structure/state.py nếu cần tách state dùng chung
- tests/test_smc_bos_choch.py

Không đặt logic BOS/CHoCH vào swings.py.

Luồng chuẩn:

1. SwingDetectorState nhận candle mới.
2. Swing detector trả swing mới đã confirmed.
3. StructureTracker nhận candle và confirmed swings.
4. Tracker trả StructureEvent hoặc danh sách rỗng.
5. Event được lưu cho debug và dùng bởi phase sau.

## 5. Data model

### SwingPoint

BOS/CHoCH dùng các field:

- index
- time
- price
- kind
- confirmed_at
- mode
- broken
- broken_at

index là bar nơi swing hình thành. confirmed_at là bar đầu tiên được phép sử dụng swing.

### StructureEvent

Giữ model hiện có:

- index
- time
- event_type: BOS hoặc CHoCH
- direction: bullish hoặc bearish
- broken_swing_index
- broken_swing_price
- close_price
- displacement

Nên bổ sung metadata hoặc field:

- mode
- confirmed_swing_at
- body_size
- atr_value
- break_type = close

## 6. State machine

### State tối thiểu

StructureState cần có:

- mode: swing hoặc internal
- trend: bullish, bearish hoặc None
- active_high: SwingPoint hoặc None
- active_low: SwingPoint hoặc None
- known_swing_keys
- events
- last_processed_bar

Có thể lưu thêm lịch sử swing để debug, nhưng quyết định break chỉ được dùng active swing hợp lệ.

### Nạp swing

Ở mỗi bar:

1. Chỉ nhận swing có confirmed_at nhỏ hơn hoặc bằng current_bar_index.
2. Chỉ nhận swing đúng mode.
3. Deduplicate theo mode, index và kind.
4. Chọn active high/low theo policy cố định.
5. Không biến swing thành active trước thời điểm xác nhận.

### Chọn active swing

V1 dùng swing mới nhất chưa bị phá theo từng loại:

- bullish break kiểm tra active_high.
- bearish break kiểm tra active_low.

Nếu có nhiều candidate, dùng candidate mới nhất theo confirmed_at rồi index. Policy này phải có test.

## 7. Quy tắc tạo break

### Bullish break

Nếu close lớn hơn active_high.price và active_high chưa broken:

- direction = bullish.
- event_type = CHoCH nếu trend hiện tại là bearish, ngược lại là BOS.
- trend chuyển thành bullish.
- đánh dấu active_high broken.

### Bearish break

Nếu close nhỏ hơn active_low.price và active_low chưa broken:

- direction = bearish.
- event_type = CHoCH nếu trend hiện tại là bullish, ngược lại là BOS.
- trend chuyển thành bearish.
- đánh dấu active_low broken.

### Đánh dấu swing đã phá

Khi event được tạo:

- broken = True.
- broken_at = current_bar_index.

Một swing không được tạo event lần thứ hai. Wick-only break không đánh dấu broken.

### Nến phá cả hai phía

Nếu dữ liệu làm cho một nến vừa phá active high vừa phá active low:

- policy mặc định là skip.
- không tạo event.
- không đổi trend.
- ghi debug metadata nếu cần.

Sau này có thể hỗ trợ policy bullish hoặc bearish nhưng phải có test riêng.

## 8. Batch detector

API đề xuất:

detect_structure_events(
    data,
    swings=None,
    strength=5,
    mode="swing",
    current_bar_index=None,
    atr_period=14,
    displacement_multiplier=1.5,
    ambiguous_policy="skip"
)

Quy trình:

1. Normalize OHLCV.
2. Dùng swings truyền vào hoặc gọi detect_swings.
3. Lọc đúng mode.
4. Duyệt nến theo thứ tự tăng dần.
5. Nạp swing theo confirmed_at.
6. Kiểm tra close break.
7. Tạo event tối đa theo policy.
8. Cập nhật trend và broken state.
9. Trả event theo index tăng dần.

Nếu có current_bar_index, không trả event sau cutoff.

## 9. Incremental StructureTracker

API đề xuất:

- Khởi tạo với mode, atr_period, displacement_multiplier và ambiguous_policy.
- update(candle, confirmed_swings) trả các event mới.
- get_state() trả state hiện tại.
- get_events() trả event history.

Quy tắc:

- Chỉ xử lý bar mới hơn last_processed_bar.
- Bar trùng hoặc đi lùi phải bị reject hoặc xử lý theo policy rõ ràng.
- Không gọi detect batch trên toàn bộ lịch sử trong mỗi update.
- Nhận swing từ SwingDetectorState.update.
- Emit event đúng bar close break.
- Gọi lại cùng bar không được duplicate event.
- State phải giữ active high/low và trend giữa các lần update.

## 10. Displacement

Điều kiện:

- body_size = abs(close - open).
- displacement = body_size lớn hơn ATR tại bar break nhân multiplier.

Quy tắc v1:

- ATR period mặc định 14.
- Dùng thân nến, không dùng toàn bộ range.
- ATR chỉ dùng dữ liệu đến bar break.
- ATR chưa đủ dữ liệu thì displacement là False.
- ATR bằng 0 thì displacement là False.
- multiplier phải lớn hơn 0.

Không dùng ATR tính từ các bar sau event.

## 11. Tie-breaking và edge cases

Bắt buộc có policy và test cho:

- Close đúng bằng swing price.
- Wick vượt nhưng close không vượt.
- Một nến phá nhiều swing high hoặc low.
- Một nến phá cả hai phía.
- Swing được xác nhận đúng tại bar break.
- Hai swing xác nhận cùng một bar.
- Swing đã broken nhưng vẫn xuất hiện trong input.
- Duplicate timestamp hoặc bar index không tăng.
- Dữ liệu thiếu bar.
- Mode không hợp lệ.
- ATR period hoặc multiplier không hợp lệ.

## 12. Output chart và debug

Mỗi event phải serialize được các trường:

- index
- time
- event_type
- direction
- broken_swing_index
- broken_swing_price
- close_price
- displacement
- mode

Chart payload nên hỗ trợ:

- đường từ swing đến bar break.
- label BOS hoặc CHoCH.
- màu bullish/bearish.
- phân biệt swing line và internal line.
- tooltip gồm swing price, break close, confirmation time và displacement.

## 13. Test plan

Tạo tests/test_smc_bos_choch.py.

Unit tests tối thiểu:

1. Break lên khi chưa có trend = bullish BOS.
2. Break xuống khi chưa có trend = bearish BOS.
3. Bullish trend phá swing low = bearish CHoCH.
4. Bearish trend phá swing high = bullish CHoCH.
5. BOS continuation không bị gắn nhầm CHoCH.
6. Close bằng swing price không tạo event.
7. Wick-only break không tạo event.
8. Một swing chỉ bị phá một lần.
9. broken_at đúng bar.
10. Event giữ đúng swing index và price.
11. Swing chưa xác nhận không được dùng.
12. current_bar_index chặn event tương lai.
13. Swing mode không đọc internal swing.
14. Internal mode không đọc swing mode.
15. Displacement true.
16. Displacement false.
17. ATR chưa đủ dữ liệu.
18. ATR bằng 0.
19. Hai swing cùng confirmation bar có ordering deterministic.
20. Nến phá cả hai phía theo ambiguous_policy.
21. Batch và incremental cho cùng event sequence.
22. Gọi update cùng bar hai lần không duplicate.
23. Bar index đi lùi bị reject.
24. Mode và tham số ATR không hợp lệ bị reject.

## 14. Synthetic datasets

### Dataset A — Initial bullish BOS

- Có swing high được xác nhận.
- Close phá swing high.
- Trend ban đầu None.
- Kết quả là bullish BOS.

### Dataset B — Bullish continuation

- Đã có bullish BOS.
- Hình thành swing high mới.
- Close phá swing high mới.
- Kết quả là bullish BOS.

### Dataset C — Bullish reversal

- Trend bullish.
- Hình thành swing low.
- Close phá swing low.
- Kết quả là bearish CHoCH.

### Dataset D — Bearish reversal

- Trend bearish.
- Hình thành swing high.
- Close phá swing high.
- Kết quả là bullish CHoCH.

### Dataset E — Wick sweep candidate

- Wick xuyên swing.
- Close quay lại phía trong.
- Kết quả là không có BOS/CHoCH.

## 15. Integration với phase sau

### Order Block

Order Block nhận StructureEvent trực tiếp và sử dụng:

- event index
- direction
- broken swing
- close price
- displacement
- mode

Order Block không được tự tính lại BOS/CHoCH.

### BacktestEngine

Khi tích hợp:

- signal chỉ phát sau event đã xác nhận.
- entry vẫn thực thi ở Open nến kế tiếp.
- event_index và execution_index lưu riêng.
- trade metadata lưu event type, direction và swing index.
- không dùng event xuất hiện sau execution bar.

## 16. Performance requirement

Không lặp lại toàn bộ lịch sử trong mỗi StructureTracker.update.

Incremental tracker phải:

- lưu active high/low.
- lưu trend state.
- append-only event history.
- xử lý mỗi nến một lần.
- dùng rolling ATR buffer.

Acceptance benchmark:

- 10.000 synthetic bars incremental dưới 1 giây trên máy phát triển.
- Batch và incremental cho cùng event sequence.
- Thời gian không tăng theo O(N²).

## 17. Definition of Done

BOS/CHoCH phase hoàn thành khi:

- Có batch detector.
- Có incremental StructureTracker.
- BOS/CHoCH đúng theo trend state.
- Chỉ dùng close break.
- Không dùng swing chưa xác nhận.
- Không duplicate event.
- Có displacement flag không lookahead.
- Swing/internal độc lập.
- Có ít nhất 24 test chính.
- Batch và incremental cho cùng kết quả.
- Có JSON debug payload.
- 50 Python regression tests hiện tại vẫn PASS.
- Benchmark incremental không bị O(N²).

## 18. Thứ tự triển khai

1. Chốt policy: initial trend, close strict, multiple break, ambiguous candle, displacement.
2. Viết batch state machine.
3. Viết unit tests synthetic.
4. Viết incremental StructureTracker.
5. Viết parity test batch/incremental.
6. Thêm ATR và displacement.
7. Thêm serialization và visualization payload.
8. Chạy regression và benchmark.
9. Chỉ sau khi pass mới bắt đầu Order Block.

