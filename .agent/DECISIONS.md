# DECISIONS.md

> Ghi nhận các quyết định kiến trúc và kỹ thuật quan trọng (ADRs).

## 2026-09-03 - ADR 01: Khởi động dự án Web Trading Backtest với SQLite Dataset
- **Bối cảnh**: Dự án bắt đầu với file `plan.md` và cơ sở dữ liệu `data/XAUUSD.db` chứa ~3.3 triệu nến M1 vàng (XAUUSD) từ 2014 đến nay. Cần xây dựng nền tảng backtest đồ thị dạng TradingView.
- **Các phương án đã xét**:
  - A: Chuyển toàn bộ sang PostgreSQL / TimescaleDB (mất công setup server DB nặng, cồng kềnh cho người dùng local).
  - B: Tận dụng trực tiếp SQLite `data/XAUUSD.db` với index tối ưu trên cột `time` và tính toán nến resample (M5, M15, H1, D1) theo lô/cache.
- **Đã chọn**: Phương án B.
- **Lý do**: SQLite cục bộ cực kỳ gọn nhẹ, zero-configuration, tốc độ đọc theo index siêu nhanh khi chạy trên ổ SSD, phù hợp hoàn hảo với kiến trúc web app chạy local/desktop.

## 2026-09-03 - ADR 02: Lựa chọn thư viện biểu đồ nến
- **Bối cảnh**: Cần một thư viện biểu đồ nến đẹp chuẩn phong cách TradingView, tương tác mượt mà với hàng chục ngàn nến mà không giật lag.
- **Các phương án đã xét**:
  - A: Chart.js hoặc ApexCharts (không tối ưu cho khối lượng nến lớn, UX không giống TradingView).
  - B: TradingView Charting Library (mã nguồn đóng, cần đăng ký license công khai).
  - C: TradingView Lightweight Charts (thư viện chính thức mã nguồn mở của chính TradingView, cực nhẹ ~40KB, hiệu năng canvas 60fps).
- **Đã chọn**: Phương án C (Lightweight Charts).
- **Lý do**: Chuẩn giao diện TradingView, mã nguồn mở miễn phí, hỗ trợ nến Candlestick + Volume + Markers + Histogram/Line indicators cực tốt.

## 2026-09-04 - ADR 03: Lấy schema database hiện tại làm nguồn chuẩn
- **Bối cảnh**: QC xác nhận `data/XAUUSD.db` hiện chỉ có bảng `XAUUSD_M1`; tên `XAUUSDc_M1` trong code/tài liệu không khớp và các bảng H1/D1 chưa tồn tại.
- **Quyết định**: Sửa DataFeed theo schema thực tế của DB (`XAUUSD_M1`), đồng thời thiết kế H1/D1 từ nguồn M1 theo một cơ chế nhất quán; không đổi hoặc xóa dữ liệu gốc trong quá trình fix.
- **Lý do**: Tránh phụ thuộc vào tên bảng của dataset khác và bảo đảm app chạy được với artifact đang nằm trong workspace.

## 2026-09-04 - ADR 04: Khóa quy ước khớp lệnh để loại bỏ lookahead
- **Bối cảnh**: Engine hiện phát hiện tín hiệu từ close của nến N rồi vào lệnh ngay trên close N, làm kết quả backtest lạc quan giả.
- **Quyết định**: Tín hiệu tại nến N được khớp tại nến N+1 theo giá mở cửa và mô hình spread đã ghi rõ; mọi thay đổi phải có test synthetic.
- **Lý do**: Đây là quy ước bar-by-bar an toàn và có thể kiểm chứng, phù hợp dữ liệu OHLC không có tick.

## 2026-09-04 - ADR 05: Khớp Short SL/TP theo giá Ask và ưu tiên Stop Loss khi nến quét 2 đầu
- **Bối cảnh**: Lệnh Short đóng lệnh bằng việc mua lại ở giá Ask (Bid + Spread). Do các cột High/Low trong database là giá Bid, việc so sánh trực tiếp High/Low với SL/TP sẽ bỏ qua chi phí spread. Đồng thời, khi nến biến động cực lớn chạm cả SL và TP, cần quy ước rõ ràng.
- **Quyết định**:
  - Với lệnh Short: Stop Loss kích hoạt khi `High + Spread >= sl_price`; Take Profit kích hoạt khi `Low + Spread <= tp_price`.
  - Khi một cây nến chạm đồng thời cả Stop Loss và Take Profit, hệ thống quy ước ưu tiên Stop Loss trước theo nguyên tắc phòng ngừa rủi ro bảo toàn vốn trong giao dịch thực tế (conservative risk modeling).
- **Lý do**: Đảm bảo kết quả backtest phản ánh chính xác chi phí trượt giá/spread của lệnh Short và không bị lạc quan hóa khi thị trường giật mạnh hai chiều.

## 2026-09-04 - ADR 06: Chính sách xử lý Partial Candle trong Bar Replay
- **Bối cảnh**: Khi người dùng chọn điểm cắt `cut_time` ở khung thời gian lớn (M5, M15, H1, D1), mốc thời gian có thể rơi vào giữa chu kỳ của một cây nến (ví dụ `14:07` thuộc nến H1 `14:00-15:00`).
- **Quyết định**:
  - Nếu `cut_time` trùng khớp thời điểm bắt đầu cây nến (hoặc nến đã trọn vẹn): nến được tính vào lịch sử (`history`).
  - Nếu `cut_time` rơi vào giữa cây nến (partial candle): loại bỏ cây nến dở dang khỏi `history` (nến history cuối cùng là nến đóng hoàn chỉnh trước đó), và đưa nguyên cây nến đầy đủ đó vào làm phần tử đầu tiên của `future` queue.
- **Lý do**: Đảm bảo tính toàn vẹn dữ liệu: mọi phút M1 đều được tổng hợp đầy đủ vào nến resample tương ứng, không có phút nào bị biến mất, không có nến dở dang gây sai lệch chỉ báo kỹ thuật, và không bao giờ trùng lặp timestamp giữa history và future.

## 2026-09-04 - ADR 07: Xây dựng Drawing Engine custom trên Lightweight Charts
- **Bối cảnh**: Lightweight Charts không có bộ công cụ vẽ hình học tích hợp sẵn. Cần xây dựng lớp overlay độc lập để hỗ trợ các công cụ phân tích kỹ thuật.
- **Quyết định**: Xây dựng lớp drawing overlay bằng SVG độc lập, lưu anchor theo `time + price`, dùng coordinate conversion của chart (`timeToCoordinate`, `priceToCoordinate`) để render và hit-test; không vẽ bằng pixel cố định.
- **Lý do**: Drawing giữ đúng vị trí khi zoom, pan, resize và đổi khung thời gian, hoàn toàn không phụ thuộc hay làm thay đổi dữ liệu nến.

## 2026-09-04 - ADR 08: Định nghĩa phạm vi công cụ vẽ phổ biến (Parity Scope)
- **Bối cảnh**: Hệ thống TradingView Charting Library có license hỗ trợ hàng trăm công cụ và chỉ báo phức tạp. Cần xác định rõ ranh giới cho bản offline.
- **Quyết định**: Tập trung xây dựng bộ công cụ vẽ và phân tích kỹ thuật cốt lõi: Trend Line, Ray, Extended, Horizontal/Vertical Line, Rectangle, Channel, Fibonacci Retracement/Extension, Ruler/Measure, Text/Arrow, Style Bar, Undo/Redo, LocalStorage, Replay & Dual Chart. Không triển khai cloud sync, Pine Script, Alert hay social sharing.
- **Lý do**: Đảm bảo hoàn thành đúng cam kết với chất lượng cao nhất, chạy mượt mà 100% offline.

## 2026-09-04 - ADR 09: State Machine SVG Overlay & Lưu trữ theo Symbol Layout
- **Bối cảnh**: SVG overlay nếu chặn toàn bộ chuột sẽ làm tê liệt pan/zoom của chart; nếu lưu theo timeframe sẽ làm mất drawing khi đổi timeframe.
- **Quyết định**:
  - State machine: Ở Select mode, SVG container `pointer-events: none`, các shape/anchor `pointer-events: stroke / all`. Ở Draw mode, SVG container `pointer-events: all`, sau khi đặt đủ anchor tự động chuyển về Select mode.
  - Lưu trữ: Lưu theo `drawings:XAUUSD:layout` với `scope: "symbol" | "timeframe"` (mặc định `symbol`) để drawing tồn tại xuyên suốt các khung thời gian.
- **Lý do**: Tương tác mượt mà chuẩn TradingView, giải quyết triệt để mâu thuẫn lưu trữ và tránh chặn tương tác biểu đồ.

## 2026-09-05 - ADR 10: Cơ chế Vẽ Vô Hạn Tương Lai bằng Logical Index và Đồng Bộ Đa Trục qua Container Pointer Tracker & rAF
- **Bối cảnh**:
  1. Lightweight Charts v4 giới hạn `coordinateToTime(x)` trả về `null` trong whitespace. Nếu nến cuối bị cuộn ra khỏi màn hình bên trái (`timeToCoordinate(last.time) === null`), việc phụ thuộc vào `lastX` khiến việc ngoại suy tương lai bị hỏng. Ngoài ra `rightOffset: 10` cố định làm giới hạn không gian tương lai.
  2. Lightweight Charts không có event công khai cho trục giá (`subscribePriceRangeChange`). Sử dụng `attachPrimitive` có nguy cơ gây render loop nếu `updateAllViews` được gọi trong chính frame vẽ của chart.
  3. Tính interval giữa 2 nến cuối dễ bị sai lệch nếu rơi vào gap cuối tuần hoặc dữ liệu nghỉ lễ.
- **Quyết định**:
  1. **Ngoại suy 2 chiều hoàn toàn qua Logical Index & Nới rightOffset động**:
     - Chuyển đổi `x -> logical -> time`: Sử dụng `timeScale.coordinateToLogical(x)`. Khi `logical > candles.length - 1`, tính `diffBars = logical - (candles.length - 1)` và `time = Math.round(last.time + diffBars * interval)`.
     - Chuyển đổi `time -> logical -> x`: Khi `time > last.time`, tính `targetLogical = (candles.length - 1) + (time - last.time) / interval` và `x = timeScale.logicalToCoordinate(targetLogical)`. Hoàn toàn không phụ thuộc vào `timeToCoordinate(last.time)` và hoạt động cả khi nến cuối đã bị cuộn ra ngoài màn hình.
     - Tự động nới `rightOffset` của `timeScale` khi người dùng đặt điểm hoặc kéo vẽ vượt xa mép tương lai hiện tại để đảm bảo tính vô hạn thực tế.
     - Mở rộng viewport bounding box trong `render()` bằng `getVisibleLogicalRange()` để không cull nhầm các nét vẽ tương lai.
  2. **Tính Interval bằng Median đa nến (Multi-Candle Median)**:
     - Lấy delta thời gian của nhóm 20-30 nến gần nhất, loại bỏ delta <= 0 hoặc bất thường, lấy median.
     - Fallback theo `currentTimeframe` (M1: 60s, M5: 300s, M15: 900s, H1: 3600s, D1: 86400s...) nếu không đủ nến. Tuyệt đối không hard-code 60 giây và không thêm nến giả vào OHLC.
  3. **Đồng bộ đa trục an toàn qua Container Pointer Tracker & rAF Batching**:
     - Không dùng `attachPrimitive()` làm cơ chế chính để loại bỏ 100% rủi ro render loop.
     - Lắng nghe `pointerdown`/`pointermove`/`pointerup` trên `this.container` và `window` để bắt trọn vẹn thao tác kéo thanh giá (Price Scale) và thanh thời gian (Time Scale) ở mọi chế độ (kể cả Select Mode).
     - Kết hợp với `subscribeVisibleLogicalRangeChange`, `wheel`, `dblclick`, `ResizeObserver`.
     - Toàn bộ sự kiện được gom lại qua `requestAnimationFrame` có cờ `renderPending`, đảm bảo 1 frame chỉ render tối đa 1 lần, hủy sạch rAF ID khi `destroy()`.
- **Lý do**: Giải quyết triệt để 2 lỗi vẽ, loại bỏ hoàn toàn rủi ro render loop, hoạt động ổn định trên mọi loại thiết bị và màn hình.


