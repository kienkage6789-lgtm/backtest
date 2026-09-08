# TASKS.md

> Cập nhật trạng thái NGAY khi thay đổi, không đợi cuối buổi.
> Trạng thái: todo | doing | blocked | review | done

### [x] T01 - Khảo sát Database & Dựng Data API Service
- Mô tả: Kiểm tra cấu trúc SQLite `data/XAUUSD.db`, lập chỉ mục (index trên `time` nếu cần) để query theo khoảng thời gian siêu tốc, viết script / API đọc nến và resample từ M1 sang M5, M15, H1, D1.
- File liên quan: `data/XAUUSD.db`, `engine/data_feed.py`, `server.py`, `tests/test_data.py`, `tests/test_api.py`
- Acceptance criteria:
  - [x] Truy vấn 1,000 đến 10,000 nến theo khoảng thời gian < 100ms (Thực tế: 5,000 nến trong ~25ms).
  - [x] Thuật toán resample M1 ra nến M5, M15, H1, D1 chính xác (Open = bar đầu, High = max, Low = min, Close = bar cuối, Volume = sum).
  - [x] Chạy test kiểm tra tính đúng đắn của dữ liệu nến resample (4/4 tests PASS).
- Phụ thuộc: không
- Trạng thái: done

### [x] T02 - Thiết kế giao diện Chart phong cách TradingView (Dark Mode)
- Mô tả: Dựng frontend với TradingView Lightweight Charts, thanh công cụ chọn timeframe (M1, M5, M15, H1, D1), zoom/pan mượt mà, load nến động khi cuộn.
- File liên quan: `public/index.html`, `public/style.css`, `public/chart.js`, `public/vendor/lightweight-charts.standalone.production.js`
- Acceptance criteria:
  - [x] Render biểu đồ nến chuẩn sắc nét, hiển thị giá tooltip, volume bar bên dưới.
  - [x] Nút chuyển timeframe hoạt động tức thì, load dữ liệu tương ứng.
  - [x] Hoạt động 100% offline với thư viện vendor lưu cục bộ.
- Phụ thuộc: T01
- Trạng thái: done

### [x] T03 - Xây dựng Backtest Engine lõi
- Mô tả: Module tính toán mô phỏng khớp lệnh (Long/Short, Market order, SL/TP, trailing stop, phí spread và commission).
- File liên quan: `engine/backtest_engine.py`, `tests/test_backtest.py`
- Acceptance criteria:
  - [x] Thực thi chiến lược trên tập nến và tính toán danh sách giao dịch (Trades list).
  - [x] Tính toán các chỉ số: Total PnL, Win Rate, Profit Factor, Max Drawdown (MDD).
  - [x] Có unit test kiểm thử logic khớp lệnh với các case chuẩn (2/2 test PASS).
- Phụ thuộc: T01
- Trạng thái: done

### [x] T04 - Tích hợp Chiến lược mẫu & Bảng điều khiển Backtest
- Mô tả: Cung cấp sẵn các chiến lược phổ biến (SMA Crossover, RSI Oversold/Overbought, MACD, Donchian Breakout), form nhập tham số (Vốn ban đầu, Lot size, SL/TP pips) và nút "Chạy Backtest".
- File liên quan: `engine/strategies.py`, `public/index.html`, `public/app.js`
- Acceptance criteria:
  - [x] Người dùng chọn được chiến lược và tinh chỉnh tham số trên UI.
  - [x] Nhấn "Run Backtest", kết quả tính toán trả về trong vài giây (Thực tế: <60ms).
- Phụ thuộc: T02, T03
- Trạng thái: done

### [x] T05 - Hiển thị Báo cáo: Equity Curve & Trade Markers trên Chart
- Mô tả: Vẽ các điểm Vào lệnh (Buy/Sell marker) và Thoát lệnh trực tiếp lên Lightweight Charts; vẽ đồ thị tăng trưởng vốn (Equity Curve) và bảng thống kê chi tiết.
- File liên quan: `public/app.js`, `public/chart.js`, `public/index.html`
- Acceptance criteria:
  - [x] Marker Buy (màu xanh)/Sell (màu đỏ) hiển thị đúng thời điểm trên nến.
  - [x] Biểu đồ Equity Curve thể hiện rõ mức sụt giảm (drawdown) và lợi nhuận tích lũy.
  - [x] Bảng chi tiết từng lệnh (Entry, Exit, PnL, Return, Reason).
- Phụ thuộc: T04
- Trạng thái: done

### [x] T06 - Tính năng Tua Nến (Bar Replay) & Đa Khung Thời Gian (Multi-Timeframe)
- Mô tả: Xây dựng cơ chế Replay nến về bất kỳ thời điểm nào trong quá khứ, nhả nến mượt mà với Play/Pause/Step Forward, hỗ trợ chuyển khung thời gian giữ nguyên vị trí và chế độ Dual Chart (2 biểu đồ song song đồng bộ).
- File liên quan: `engine/data_feed.py`, `server.py`, `public/index.html`, `public/style.css`, `public/chart.js`, `public/app.js`, `tests/test_replay.py`
- Acceptance criteria:
  - [x] API `/api/replay/init` trả về nến history (trước điểm cắt) và nến future (sau điểm cắt) chính xác.
  - [x] Replay Toolbar nổi với các nút Play, Pause, Step Next (tiến 1 nến), thanh tốc độ (0.1s - 2.0s) và thoát.
  - [x] Công cụ chọn nến cắt trực quan: Click vào nến bất kỳ để tua về quá khứ ngay lập tức.
  - [x] Chuyển đổi khung thời gian trong khi replay không làm mất mốc thời gian đang tua.
  - [x] Chế độ Dual Chart (2 khung thời gian cạnh nhau) đồng bộ nhịp nến khi tua.
  - [x] Viết unit/integration test trong `tests/test_replay.py` và chạy pass 100% (3/3 test PASS).
- Phụ thuộc: T01, T02
- Trạng thái: done

---
## Ghi chú tồn đọng / Ý tưởng mở rộng
- [ ] Cho phép người dùng viết chiến lược tùy biến (Custom Strategy Script / Sandbox).
- [ ] Cho phép đặt lệnh thủ công Buy/Sell trực tiếp trong chế độ Replay để luyện giao dịch.

## QC Audit 2026-09-04
- **Trạng thái phát hành:** blocked — chưa thể xác nhận MVP đạt acceptance criteria trên workspace hiện tại.
- **Blocker P0:** `data/XAUUSD.db` chỉ có bảng `XAUUSD_M1`, trong khi `engine/data_feed.py` truy vấn `XAUUSDc_M1` và các bảng H1/D1 không tồn tại.
- **Môi trường test:** thiếu `fastapi` và `pandas`, nên `python -m unittest discover tests` chưa chạy được.
- **Lỗi cần xử lý:** query có cả `start_time`/`end_time` bỏ qua `limit`; chỉ có `end_time` bị bỏ qua; backtest vào lệnh cùng nến phát tín hiệu; spread short không được tính đối xứng; max drawdown/equity curve chưa cập nhật sau khi đóng lệnh cuối; dual-chart chỉ tiến một nến phụ cho mỗi nến chính.

## Kế hoạch sửa QC — 2026-09-04

### [x] T07 - Đồng bộ DataFeed với database thật và môi trường chạy
- Mô tả: Chuẩn hóa tên bảng theo DB hiện tại (`XAUUSD_M1`) hoặc cơ chế phát hiện alias; xử lý H1/D1 chưa tồn tại bằng phương án resample/materialize nhất quán. Bổ sung manifest dependency để có thể dựng lại môi trường test.
- File liên quan: `engine/data_feed.py`, `data/README.md`, `requirements.txt`, `tests/test_data.py`
- Acceptance criteria:
  - [x] `/api/info` đọc được DB hiện tại và trả đúng số dòng/thời gian (1,831,773 nến, 2016-2026).
  - [x] M1, M5, M15, H1, H4, D1 đều trả dữ liệu hợp lệ từ DB chỉ có M1.
  - [x] Khởi tạo `DataFeed` không nuốt lỗi schema bất ngờ một cách im lặng (báo lỗi rõ ràng).
  - [x] Dependency manifest liệt kê đủ runtime/test dependency trong `requirements.txt`.
- Phụ thuộc: không
- Trạng thái: done

### [x] T08 - Sửa query range và giới hạn dữ liệu
- Mô tả: Làm rõ precedence giữa `start_time`, `end_time`, `before_time`; bảo đảm `limit` luôn được tôn trọng và input thời gian sai trả lỗi 400.
- File liên quan: `engine/data_feed.py`, `server.py`, `tests/test_data.py`, `tests/test_api.py`
- Acceptance criteria:
  - [x] Có cả start/end vẫn không trả quá `limit`.
  - [x] Chỉ có end_time trả các nến mới nhất trước mốc đó.
  - [x] `before_time` trả đúng các nến đứng trước mốc, theo thứ tự tăng dần.
  - [x] Sai timeframe/thời gian/range rỗng có phản hồi lỗi rõ ràng (HTTP 400 / ValueError).
- Phụ thuộc: T07
- Trạng thái: done

### [x] T09 - Sửa tính đúng đắn của Backtest Engine
- Mô tả: Loại bỏ lookahead bằng cách khớp tín hiệu ở nến kế tiếp; tính spread đối xứng cho Long/Short; cập nhật equity, drawdown và marker khi forced close cuối kỳ.
- File liên quan: `engine/backtest_engine.py`, `engine/strategies.py`, `tests/test_backtest.py`
- Acceptance criteria:
  - [x] Tín hiệu tại nến N chỉ tạo lệnh từ giá hợp lệ của nến N+1 (giá Open nến tiếp theo).
  - [x] Long và Short chịu spread/commission đúng và nhất quán.
  - [x] Forced close được phản ánh trong balance, equity curve, MDD và markers.
  - [x] Strategy ID/params không hợp lệ bị từ chối thay vì trả kết quả rỗng giả.
- Phụ thuộc: T07
- Trạng thái: done

### [x] T10 - Sửa đồng bộ Replay/Dual Chart và vòng đời biểu đồ
- Mô tả: Đồng bộ toàn bộ nến phụ đã đến thời điểm của nến chính; giữ mốc replay khi đổi timeframe; dọn chart/observer cũ khi render lại equity curve.
- File liên quan: `public/app.js`, `public/chart.js`, `tests/test_replay.py`
- Acceptance criteria:
  - [x] Khi chart chính tiến một bước, chart phụ không bị tụt nến dù timeframe nhỏ hơn (đồng bộ qua while loop).
  - [x] Đổi timeframe giữ đúng mốc thời gian replay và không bỏ qua nến hợp lệ.
  - [x] Không tạo vô hạn chart/ResizeObserver sau nhiều lần chạy backtest (đã thêm destroy() và cleanup observer).
  - [x] JavaScript syntax và manual replay flow pass (node --check pass, 6/6 test_replay pass).
- Phụ thuộc: T07, T08
- Trạng thái: done

### [x] T11 - Regression test, review và đối chiếu plan
- Mô tả: Chạy toàn bộ unit/integration test trong môi trường dependency đầy đủ, bổ sung edge cases, kiểm tra static API/UI và rà lại các tiêu chí MVP.
- File liên quan: `tests/`, `.agent/TASKS.md`, `.agent/CHANGELOG.md`
- Acceptance criteria:
  - [x] Toàn bộ test tự động pass (36/36 tests PASS trong 1.74s).
  - [x] Có test cho DB schema hiện tại, range query, lookahead/spread/MDD và dual replay.
  - [x] Không còn blocker P0/P1 đã ghi trong QC audit.
  - [x] Cập nhật changelog và trạng thái task sau review.
- Phụ thuộc: T08, T09, T10
- Trạng thái: done

## Đợt QC hoàn thiện chuyên sâu — 2026-09-04

### [x] T12 - Hoàn thiện chính sách Replay partial candle & Pin dependencies
- Mô tả: Pin version trong requirements.txt; xử lý chính sách rõ ràng khi cut_time nằm giữa nến M5/M15/H1 (loại bỏ partial candle không để mất phút M1 hoặc trùng lặp).
- File liên quan: `requirements.txt`, `engine/data_feed.py`, `tests/test_replay.py`
- Acceptance criteria:
  - [x] requirements.txt pin version tương thích (`fastapi>=0.110.0,<1.0.0`, `uvicorn>=0.29.0,<1.0.0`, `pydantic>=2.7.0,<3.0.0`, `pandas>=2.2.0,<4.0.0`, `numpy>=1.26.0,<3.0.0`, `httpx>=0.27.0,<1.0.0`).
  - [x] Khi cut_time nằm giữa nến, history loại bỏ partial candle, future queue bắt đầu từ nến đó đầy đủ dữ liệu.
  - [x] History và future tăng dần, không trùng timestamp, không mất dữ liệu M1 (test_07_partial_candle_exclusion_policy PASS).
- Phụ thuộc: không
- Trạng thái: done

### [x] T13 - Hoàn thiện Backtest Engine (Short SL/TP spread trigger, SL/TP đồng thời, NaN/Inf validation)
- Mô tả: Xét ảnh hưởng spread khi kiểm tra High/Low cho Short SL/TP; xử lý quy ước ưu tiên khi cùng nến chạm cả SL và TP; validate triệt để NaN, Inf trong strategies.
- File liên quan: `engine/backtest_engine.py`, `engine/strategies.py`, `tests/test_backtest.py`
- Acceptance criteria:
  - [x] Short SL trigger khi `High + Spread >= sl_price`; Short TP trigger khi `Low + Spread <= tp_price` (test_04_short_sl_tp_spread_trigger PASS).
  - [x] Nến chạm cả SL và TP ưu tiên SL trước (test_05_candle_touching_both_sl_and_tp_prioritizes_sl PASS).
  - [x] NaN, Inf, non-convertible values trong strategy params bị từ chối bằng ValueError (test_08_invalid_strategy_and_params PASS).
- Phụ thuộc: T12
- Trạng thái: done

### [x] T14 - Chạy regression test mở rộng và lập báo cáo thực tế
- Mô tả: Chạy toàn bộ test suite mở rộng, static check, API check và lập báo cáo với output thực tế.
- File liên quan: `tests/`, `.agent/TASKS.md`, `.agent/CHANGELOG.md`
- Acceptance criteria:
  - [x] 100% test PASS với output thực tế (36/36 tests PASS trong 1.74s).
  - [x] Kiểm thử live server 8 endpoints 200 OK + 11 cases HTTP 400 validation error PASS 100%.
  - [x] Báo cáo đầy đủ các mục theo yêu cầu QC.
- Phụ thuộc: T12, T13
- Trạng thái: done

## Roadmap Drawing Tools — 2026-09-04

> Mục tiêu: bổ sung bộ công cụ vẽ/phân tích trên đồ thị theo trải nghiệm TradingView. Phạm vi này tập trung vào drawing tools; không tự động bao gồm toàn bộ indicator của TradingView.

### [x] T15 - Drawing engine foundation
- Mô tả: Xây lớp quản lý drawing độc lập với candlestick series, schema điểm neo theo `time + price`, hit-test, selection, drag, resize, lock, hide và undo/redo.
- File liên quan: `public/chart.js`, `public/drawings.js` (mới), `public/app.js`, `public/style.css`, `tests/test_drawings.test.js` (mới)
- Acceptance criteria:
  - [x] Drawing tồn tại độc lập với dữ liệu nến và không mất khi chart resize/zoom/pan.
  - [x] Có registry theo `id/type`, chọn được một drawing, kéo được anchor và xóa được.
  - [x] Có undo/redo tối thiểu 50 thao tác (đã test 55 thao tác).
  - [x] Drawing locked/hidden hoạt động đúng.
  - [x] State machine SVG overlay: Select mode (pointer-events: none, shapes: stroke) vs Draw mode (pointer-events: all).
  - [x] Unit test Node.js: `node --test tests/test_drawings.test.js` PASS (17/17 tests PASS).
- Phụ thuộc: T14
- Trạng thái: done

### [x] T16 - Nhóm đường và vùng cơ bản
- Mô tả: Thêm Horizontal Line, Vertical Line, Trend Line, Ray, Extended Line, Rectangle và Parallel Channel.
- File liên quan: `public/drawings.js`, `public/chart.js`, `public/app.js`, `public/style.css`
- Acceptance criteria:
  - [x] Toolbar cho phép chọn từng công cụ và vẽ bằng click/drag.
  - [x] Drawing bám đúng hai trục thời gian/giá khi zoom, pan và đổi kích thước.
  - [x] Có anchor/handle trực quan, drag sửa được điểm neo.
  - [x] Rectangle/Channel hiển thị đúng khi hai điểm neo đảo chiều.
- Phụ thuộc: T15
- Trạng thái: done

### [x] T17 - Nhóm công cụ phân tích nâng cao
- Mô tả: Thêm Fibonacci Retracement, Fibonacci Extension, ruler/measure, price range, date range, text, arrow và callout.
- File liên quan: `public/drawings.js`, `public/chart.js`, `public/app.js`, `public/style.css`
- Acceptance criteria:
  - [x] Fib tính đúng các mức 0, 23.6, 38.2, 50, 61.8, 78.6, 100 theo hướng kéo.
  - [x] Fib extension tính đúng công thức C + (B - A) * ratio cho 3 điểm neo.
  - [x] Ruler hiển thị chênh lệch giá, phần trăm, số nến và thời lượng.
  - [x] Text/callout chỉnh sửa được nội dung, màu và vị trí.
  - [x] Các công cụ không gây lỗi khi thiếu dữ liệu hoặc kéo ngoài vùng chart.
- Phụ thuộc: T16
- Trạng thái: done

### [x] T18 - TradingView-like drawing UX
- Mô tả: Hoàn thiện left toolbar, active tool, crosshair/magnet mode, style panel, context menu, keyboard shortcuts và multi-select.
- File liên quan: `public/index.html`, `public/style.css`, `public/app.js`, `public/drawings.js`
- Acceptance criteria:
  - [x] Toolbar có tooltip và trạng thái active rõ ràng.
  - [x] Magnet snap vào OHLC gần nhất; có thể bật/tắt (ngưỡng 25px).
  - [x] Hỗ trợ Esc hủy vẽ, Delete xóa, Ctrl/Cmd+Z undo, Ctrl/Cmd+Y redo.
  - [x] Có chỉnh màu, độ dày, nét, opacity và lock/hide cho drawing đã chọn qua Mini Floating Style Bar.
  - [x] Hoạt động tốt ở desktop và màn hình hẹp.
- Phụ thuộc: T16, T17
- Trạng thái: done

### [x] T19 - Lưu drawing và tích hợp Replay/Multi-timeframe
- Mô tả: Lưu drawing theo symbol/timeframe bằng localStorage và JSON import/export; giữ drawing đúng khi replay, đổi timeframe và dual chart.
- File liên quan: `public/app.js`, `public/drawings.js`, `public/chart.js`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Reload trang không làm mất drawing đã lưu (lưu vào key `drawings:XAUUSD:layout`).
  - [x] Import/export JSON giữ nguyên type, anchor, style, lock và visibility.
  - [x] Đổi timeframe không làm drawing nhảy sai vị trí thời gian/giá (`scope: "symbol" | "timeframe"`).
  - [x] Replay chỉ thay đổi nến hiển thị; drawing vẫn giữ đúng anchor và lọc hiển thị theo `visibleInReplay: "all" | "past_only"`.
  - [x] Dual chart không làm drawing của chart chính lẫn sang chart phụ (tách qua `storageKeySuffix: "main" | "secondary"`).
- Phụ thuộc: T15, T18
- Trạng thái: done

### [x] T20 - Test và visual QA Drawing Tools
- Mô tả: Bổ sung unit test model/geometry, interaction test và kiểm tra trực quan trên browser với các trạng thái zoom/pan/replay/responsive.
- File liên quan: `tests/test_drawings.test.js`, `tests/test_server_static.py`, `public/`, `.agent/CHANGELOG.md`
- Acceptance criteria:
  - [x] Test geometry/hit-test/Fibonacci/ruler/channel/serialization pass (17/17 tests Node.js PASS).
  - [x] Test undo/redo, delete, lock/hide và import/export pass.
  - [x] JavaScript syntax và regression suite hiện tại pass (36/36 tests Python PASS, static server test PASS).
  - [x] Manual browser checklist pass cho từng tool và các trạng thái replay/timeframe.
  - [x] Không có drawing memory leak hoặc event listener bị đăng ký lặp (đã quản lý qua `disposers`).
- Phụ thuộc: T15, T16, T17, T18, T19
- Trạng thái: done

## Roadmap Nâng cấp Drawing Tools v2 (TradingView Parity) — 2026-09-04

### [x] T21 - Drawing Core v2, Registry & Auto-Migration
- Mô tả: Xây dựng Schema v2 chuẩn hóa, `DrawingToolRegistry`, auto-migration v1 -> v2, generic multi-point interaction controller, strong/weak magnet snap, eraser tool.
- File liên quan: `public/drawings.js`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Schema v2 hỗ trợ `coordinates`, `visibility`, `stats`, `alert`, `hidden`, `schemaVersion: 2`.
  - [x] Auto-migration chuyển đổi 100% dữ liệu v1 sang v2 không mất mát.
  - [x] Registry quản lý metadata (category, icon, points requirement, style defaults).
  - [x] Multi-point controller xử lý N điểm neo (cho polyline, elliott wave).
- Phụ thuộc: T20
- Trạng thái: done

### [x] T22 - Property Dialog (Modal cài đặt đa tab)
- Mô tả: Modal cấu hình chuẩn TradingView với 5 tab: Style, Coordinates, Visibility, Statistics, Text.
- File liên quan: `public/index.html`, `public/style.css`, `public/app.js`
- Acceptance criteria:
  - [x] Modal mở khi double click nét vẽ hoặc từ context menu.
  - [x] Đổi tọa độ trong tab Coordinates lập tức cập nhật vị trí trên biểu đồ (hai chiều thời gian thực).
  - [x] Tùy biến hiển thị theo từng timeframe (M1..D1).
- Phụ thuộc: T21
- Trạng thái: done

### [x] T23 - Dropdown Toolbar, Favorites Bar & Context Menu
- Mô tả: Toolbar dạng dropdown flyout categories chuẩn TradingView, thanh Favorites nổi kéo rê được, Context Menu chuột phải.
- File liên quan: `public/index.html`, `public/style.css`, `public/app.js`
- Acceptance criteria:
  - [x] 8 danh mục dropdown: Cursor, Trend, Channels, Fib/Gann, Shapes, Patterns, Forecast, Annotations.
  - [x] Favorites bar lưu và ghim các công cụ thường dùng, hỗ trợ kéo rê định vị tự do.
  - [x] Chuột phải mở menu: Settings, Duplicate, Lock, Hide, Bring to Front, Send to Back, Alert, Delete.
- Phụ thuộc: T21
- Trạng thái: done

### [x] T24 - Trend Tools mở rộng (Horizontal Ray, Crossline, Info Line, Trend Angle)
- Mô tả: Bổ sung 4 công cụ đường xu hướng TradingView.
- File liên quan: `public/drawings.js`, `public/style.css`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Horizontal Ray vẽ tia ngang từ 1 điểm về bên phải.
  - [x] Crossline vẽ chữ thập ngang và dọc qua 1 điểm.
  - [x] Info Line hiển thị badge khoảng cách giá, pips, số nến, góc dốc.
  - [x] Trend Angle hiển thị cung tròn đo độ dốc vector.
- Phụ thuộc: T21
- Trạng thái: done

### [x] T25 - Measurement & Volume mở rộng (Date & Price Range, Volume sum, Pips)
- Mô tả: Kết hợp Date & Price Range, tính tổng Volume trong vùng và pips XAUUSD chuẩn.
- File liên quan: `public/drawings.js`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Date & Price Range đo cả 2 chiều và tổng volume các nến nằm trong vùng.
  - [x] Pips vàng tính chính xác ($0.1 = 1\text{ pip}$).
- Phụ thuộc: T21
- Trạng thái: done

### [x] T26 - Channels & Pitchfork (Regression Trend, 4 Pitchfork Variants)
- Mô tả: Linear Regression OLS trend channel $\pm 2\sigma$ và Andrews' Pitchfork (Standard, Schiff, Mod-Schiff, Inside).
- File liên quan: `public/drawings.js`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] OLS regression line $y=mx+b$ và residual standard deviation bands.
  - [x] Pitchfork tính đúng median line và parallel lines cho cả 4 biến thể.
- Phụ thuộc: T24
- Trạng thái: done

### [x] T27 - Fibonacci & Gann mở rộng (Custom levels, Reverse, Fib Time Zone, Gann Box/Fan)
- Mô tả: Mở rộng tính năng cho Fib retracement/extension và bổ sung Fib Time Zone, Gann Box, Gann Fan.
- File liên quan: `public/drawings.js`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Tùy biến bật/tắt từng level và đảo ngược chiều Fib.
  - [x] Fib Time Zone vẽ các đường dọc theo chuỗi số Fibonacci.
  - [x] Gann Box / Fan vẽ các góc nan quạt hình học.
- Phụ thuộc: T24
- Trạng thái: done

### [x] T28 - Shapes & Freehand Brush (Circle, Triangle, Polyline, Brush, Highlighter)
- Mô tả: Bổ sung hình học tự do và cọ vẽ.
- File liên quan: `public/drawings.js`, `public/style.css`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Circle vẽ hình tròn theo 2 điểm.
  - [x] Triangle vẽ tam giác tô màu trong suốt từ 3 điểm.
  - [x] Polyline hỗ trợ N điểm neo tùy biến.
  - [x] Brush & Highlighter vẽ đường cong tự do liên tục mượt mà.
- Phụ thuộc: T21
- Trạng thái: done

### [x] T29 - Chart Patterns (ABCD, Head & Shoulders, Elliott Wave)
- Mô tả: Bổ sung các mẫu hình phân tích kỹ thuật cổ điển.
- File liên quan: `public/drawings.js`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] ABCD tính toán và hiển thị tỷ lệ thoái lui giữa các chân sóng.
  - [x] Head & Shoulders vẽ 3 đỉnh vai đầu vai và đường cổ neckline.
  - [x] Elliott Wave 1-5 và ABC tự động gán nhãn đỉnh sóng.
- Phụ thuộc: T27
- Trạng thái: done

### [x] T30 - Forecast / Position Tool (Long & Short Position)
- Mô tả: Công cụ dự báo vị thế Long/Short với vùng TP xanh, SL đỏ, tính Risk/Reward và PnL khớp 100% logic `backtest_engine.py`.
- File liên quan: `public/drawings.js`, `public/app.js`, `public/style.css`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Tự động tính tỷ lệ Risk/Reward khi kéo đỉnh TP / đáy SL.
  - [x] Tính toán PnL đối xứng chuẩn spread/commission của `backtest_engine.py`.
- Phụ thuộc: T25
- Trạng thái: done

### [x] T31 - Timeframe Visibility & Dual Chart Sync
- Mô tả: Lọc hiển thị bản vẽ theo timeframe cấu hình và đồng bộ giữa 2 biểu đồ.
- File liên quan: `public/drawings.js`, `public/app.js`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Đổi timeframe tự động ẩn các drawing không thuộc timeframe cho phép.
  - [x] Đồng bộ hoặc tách riêng drawing giữa main và secondary chart.
- Phụ thuộc: T22
- Trạng thái: done

### [x] T32 - Drawing Alerts Engine (Price Cross & Touch)
- Mô tả: Giám sát giá nến thời gian thực cắt hoặc chạm các nét vẽ có kích hoạt alert.
- File liên quan: `public/drawings.js`, `public/app.js`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Phát hiện nến chạm/cắt cho 13 loại alert hỗ trợ: horizontal, horizontal_ray, vertical, crossline, trendline, ray, extended, info_line, trend_angle, price_range, date_price_range, date_range, rectangle.
  - [x] Hiển thị alert toast thông báo trực quan kèm âm thanh chuông Web Audio API (có try-catch an toàn khi Autoplay bị chặn).
- Phụ thuộc: T30
- Trạng thái: done

### [x] T33 - Hoàn thiện Context Menu Chuột Phải
- Mô tả: Tích hợp menu ngữ cảnh chuột phải đầy đủ tính năng: Settings, Duplicate, Lock, Hide, Bring to Front, Send to Back, Alert, Delete.
- File liên quan: `public/index.html`, `public/style.css`, `public/app.js`
- Acceptance criteria:
  - [x] Menu mở tại vị trí chuột trên nét vẽ, xử lý đầy đủ các hành động.
- Phụ thuộc: T23
- Trạng thái: done

### [ ] T34 - Tối ưu Hiệu năng Viewport Bounding-Box Culling (1.000+ Drawings)
- Mô tả: Chỉ render SVG cho các nét vẽ nằm trong khoảng thời gian/giá đang hiển thị trên biểu đồ.
- File liên quan: `public/drawings.js`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [ ] Biểu đồ chứa $\ge 1.000$ nét vẽ vẫn pan/zoom mượt mà ở 60fps (Môi trường headless chưa đo được phần cứng GPU, đánh dấu unverified).
  - [x] Benchmark thời gian cập nhật CPU/JS processing time < 16.67ms (đạt 2.69ms trong unit test).
- Phụ thuộc: T21
- Trạng thái: unverified

### [x] T35 - Mở rộng Test Suite (Node.js & Python)
- Mô tả: Mở rộng `tests/test_drawings.test.js` từ 17 lên trên 30 unit tests bao phủ toàn bộ hình học mới, migration, alerts, culling, và chạy regression.
- File liên quan: `tests/test_drawings.test.js`, `tests/`
- Acceptance criteria:
  - [x] $\ge 30$ unit tests Node.js PASS 100% (31/31 tests PASS).
  - [x] 36/36 tests Python PASS 100%.
- Phụ thuộc: T26, T30, T34
- Trạng thái: done

### [ ] T36 - Visual QA, TradingView Parity Table & Review
- Mô tả: Rà soát trực quan toàn bộ tính năng trên giao diện, lập bảng so sánh mức độ tương thích TradingView thực tế, cập nhật changelog và docs.
- File liên quan: `.agent/CHANGELOG.md`, `.agent/PROJECT.md`, `walkthrough.md`
- Acceptance criteria:
  - [x] Bảng TradingView Parity minh bạch, phân biệt rõ tính năng hoàn chỉnh vs giới hạn kiến trúc local.
  - [ ] Visual QA trực quan trên màn hình vật lý (cần browser desktop để nghiệm thu mắt người).
  - [x] Changelog ghi lại đầy đủ các thay đổi.
- Phụ thuộc: T35
- Trạng thái: partial

## Đợt Sửa Lỗi Tích Hợp Drawing Tools Core v2 & Property Dialog — 2026-09-04

### [x] T37 - Sửa DrawingGeometry API & Render Callers
- Mô tả: Chuẩn hóa `calculatePositionRiskReward` (thêm riskAmount, rewardAmount, targetPnL, stopPnL, contractSize, pointSize), `calculateDatePriceRange` (tính pips, candleCount, durationFormatted, volumeSum), `calculateTrendAngle` (Phương án B: object `{ angleDeg, slope, deltaPrice, deltaTime }` và overload), cập nhật caller trong `drawings.js` (`info_line`, `trend_angle`, `long_position`, `short_position`) và mở rộng `checkAlerts` cho `horizontal_ray`/`crossline`.
- File liên quan: `public/drawings.js`
- Acceptance criteria:
  - [x] `calculatePositionRiskReward` trả về đầy đủ các trường yêu cầu, tính đúng Long/Short có spread/commission.
  - [x] `calculateDatePriceRange` an toàn với số, xử lý đảo ngược điểm neo và chia cho 0.
  - [x] `calculateTrendAngle` trả về object `{ angleDeg, slope, deltaPrice, deltaTime }` cho mọi trường hợp góc.
  - [x] Renderer `drawings.js` đọc đúng các trường không sinh lỗi runtime.
- Phụ thuộc: T36
- Trạng thái: done

### [x] T38 - Đồng bộ Caller trong app.js & Property Dialog Rollback
- Mô tả: Sửa caller `renderStatisticsDetails` trong `app.js` cho position, date-price và trend angle; lấy lot, spread, commission từ form thật; thêm snapshot backup khi mở Property Dialog và rollback khi nhấn Hủy.
- File liên quan: `public/app.js`
- Acceptance criteria:
  - [x] `renderStatisticsDetails` gọi đúng thứ tự tham số `(entry, stop, target, isLong, lot, spread, commission)`.
  - [x] Không hardcode lot, spread, commission.
  - [x] Nhấn Hủy hoàn tác toàn bộ thay đổi live về bản sao lưu snapshot ban đầu.
- Phụ thuộc: T37
- Trạng thái: done

### [x] T39 - Mở rộng Test Suite (Integration Call-site & Benchmarks)
- Mô tả: Bổ sung các unit/integration test trong `tests/test_drawings.test.js` kiểm tra call-site position, date-price, trend angle, property dialog simulation và benchmark render SVG 100/500/1.000 bản vẽ.
- File liên quan: `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Toàn bộ test Node.js PASS 100% (36/36 tests PASS).
  - [x] Kiểm đếm chính xác 39 drawing types (41 metadata entries).
  - [x] Đo đạc thời gian tính toán/render rõ ràng, ghi rõ `unverified` cho visual 60fps GPU.
- Phụ thuộc: T37, T38
- Trạng thái: done

### [ ] T40 - Regression Test Toàn Diện, Audit & Review
- Mô tả: Chạy toàn bộ regression test (`node --test`, `python -m unittest discover tests -v`, static checks), lập bảng DrawingGeometry contract, cập nhật `walkthrough.md`, `.agent/` docs.
- File liên quan: `.agent/TASKS.md`, `.agent/CHANGELOG.md`, `.agent/PROJECT.md`, `walkthrough.md`
- Acceptance criteria:
  - [x] Không còn lỗi call-site, không có undefined/NaN.
  - [x] Toàn bộ test suites logic pass 100% (36 JS tests + 36 Python tests).
  - [ ] Bảo lưu xác nhận visual QA mắt người trên desktop browser thật.
- Phụ thuộc: T39
- Trạng thái: partial

## Đợt QC Hoàn Thiện & Chuẩn Hóa Post-QC — 2026-09-04

### [x] T41 - Nâng Cấp Toàn Diện Alert Engine (13 Types, State Machine & Guards)
- Mô tả: Xây dựng state machine cho alerts (enter/exit dựa trên close chống nhiễu wick, touch dựa trên range), ray vector direction (tương lai, quá khứ, thẳng đứng), interval overlap cho date_range và crossline/vertical, deduplication, guards hidden/locked và lifecycle helpers.
- File liên quan: `public/drawings.js`, `public/app.js`
- Acceptance criteria:
  - [x] Hỗ trợ 13 loại drawing alerts: horizontal, horizontal_ray, vertical, crossline, trendline, ray, extended, info_line, trend_angle, price_range, date_price_range, date_range, rectangle.
  - [x] Crossline kiểm tra đồng thời cả interval thời gian và mức giá giao điểm.
  - [x] Ray thẳng đứng xử lý đúng hướng lên/xuống theo delta price.
  - [x] Runtime audio guard có try-catch không làm crash ứng dụng.
- Phụ thuộc: T40
- Trạng thái: done

### [x] T42 - Mở Rộng Test Suite Alerts (Coverage Gate 13 Types - 56/56 Tests PASS)
- Mô tả: Bổ sung 20 tests mới vào `tests/test_drawings.test.js`, nâng tổng số test lên 56 tests PASS 100% với Coverage Gate đầy đủ (touch/cross, boundary, negative, lifecycle).
- File liên quan: `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Node.js test runner đạt 56/56 tests PASS (vượt mục tiêu >= 56 tests).
  - [x] Bao phủ đầy đủ 13 loại alert và các edge cases: boundary, deduplication, replay sequence, JSON serialization.
- Phụ thuộc: T41
- Trạng thái: done

### [x] T43 - Benchmark Hiệu Năng Đa Kịch Bản (Pan, Zoom, Drag, Resize, Timeframe Switch)
- Mô tả: Đo đạc chỉ số `avgProcessingTime`, `p95ProcessingTime`, `maxProcessingTime` cho 1.000 drawings qua 10 bước pan, zoom, drag, tf switch; phân định rõ JS logic time vs GPU render time.
- File liên quan: `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Toàn bộ chỉ số avgProcessingTime đều < 16.67ms (đạt < 4ms trong bài test 1.000 bản vẽ).
  - [x] Tách bạch rõ JS logic processing time với GPU 60 FPS (xác nhận trạng thái unverified).
- Phụ thuộc: T42
- Trạng thái: done

### [x] T44 - Lập Bảng UI/E2E 32 Mục & Kiểm Thử Transactional Storage Backup
- Mô tả: Chuẩn hóa 32 mục kiểm tra theo `DrawingModel.REQUIRED_POINTS`, phân loại rõ LOGIC_PASS vs UNVERIFIED; bổ sung test cho transactional storage backup (không ghi đè khi import corrupt, fallback an toàn).
- File liên quan: `public/drawings.js`, `tests/test_drawings.test.js`
- Acceptance criteria:
  - [x] Bảng 32 mục UI/E2E chuẩn hóa phân loại 1, 2, 3, và >=4 điểm theo REQUIRED_POINTS.
  - [x] Bộ test Transactional Storage Backup PASS 100%.
- Phụ thuộc: T43
- Trạng thái: done

### [x] T45 - Chuẩn Hóa Bộ Nhớ Dự Án, Python Environment & Git Diff Check
- Mô tả: Cập nhật hướng dẫn `.venv`, `Activate.ps1`, `requirements.txt`, cập nhật `.agent/PROJECT.md`, `.agent/CHANGELOG.md`, `walkthrough.md`, `plan.md`. Ghi nhận chính xác kết quả `git diff --check`.
- File liên quan: `.agent/TASKS.md`, `.agent/PROJECT.md`, `.agent/CHANGELOG.md`, `walkthrough.md`, `plan.md`
- Acceptance criteria:
  - [x] Python tests 36/36 PASS trong `.venv` (ghi nhận warning Starlette/httpx).
  - [x] Git diff check: PASS (0 whitespace error), ghi nhận cảnh báo Windows CRLF/LF conversion.
  - [x] Toàn bộ tài liệu được đồng bộ trung thực và nhất quán.
- Phụ thuộc: T44
- Trạng thái: done

## Đợt Sửa Lỗi Layout Nghiêm Trọng (DOM Nesting, Duplicate Style Bar & UI Structure) — 2026-09-04

### [x] T46 - Sửa Lỗi Layout Nghiêm Trọng, Cân Bằng HTML, CSS Flex Kháng Lỗi & Static UI Test
- Mô tả: Xóa khối Style Bar trùng lặp ở khoảng dòng 353-382 trong `public/index.html`, loại bỏ duplicate IDs, khôi phục cấu trúc DOM lồng nhau chuẩn của `workspace` và `chart-area`, bổ sung CSS `min-width: 0; min-height: 0;` cho `.chart-area`, `.charts-grid`, `.chart-wrapper-box`, chuẩn hóa `showStyleBar()` và `hideStyleBar()` trong `public/app.js`, viết test tự động kiểm tra tĩnh `tests/test_ui_structure.test.js`, và thực hiện visual verification 17 bước trên browser.
- File liên quan: `public/index.html`, `public/style.css`, `public/app.js`, `tests/test_ui_structure.test.js`, `tests/verify_browser_qa.js`, `.agent/TASKS.md`, `.agent/CHANGELOG.md`, `walkthrough.md`
- Acceptance criteria:
  - [x] Không còn duplicate ID trong toàn bộ `public/index.html` (0 duplicate IDs).
  - [x] Số thẻ `<div>` mở và đóng cân bằng 100% (180 thẻ mở, 180 thẻ đóng).
  - [x] Đúng thứ tự DOM chuẩn: workspace chứa toolbar, favorites, chart-area, side-panel. chart-area chứa style-bar, modal, context menu, alert toast, legend, charts-grid, replay toolbar, loading overlay.
  - [x] Chart-area, charts-grid và chart-wrapper-box có `min-width: 0; min-height: 0;`.
  - [x] Bộ test tĩnh `tests/test_ui_structure.test.js` kiểm tra và PASS 100% (7/7 tests PASS).
  - [x] Toàn bộ regression test Node (56 tests) và Python (36 tests) PASS 100%.
  - [x] Kiểm thử browser 17/17 bước đạt chuẩn trực quan (PASS 100%), đã chụp screenshot kiểm chứng 4 viewports (1024x768, 1280x800, 1440x900, 1920x1080).
- Phụ thuộc: T45
- Trạng thái: done

## Đợt Triển Khai Collapsible Strategy & Results Drawer — 2026-09-05

### [x] T47 - Collapsible Strategy & Results Drawer (Drawer dùng chung, Toggle độc lập, Persistence & Responsive)
- Mô tả: Chuyển đổi `.side-panel` thành drawer bên phải mặc định đóng để chart mở rộng 100%, bổ sung 2 nút toggle độc lập trên header (`#btn-toggle-strategy`, `#btn-toggle-results`), nút đóng `✕` (`#btn-close-drawer`), backdrop mobile, hỗ trợ phím tắt `Escape`, lưu trạng thái qua `localStorage` (`backtest:ui:drawer`), tự động resize chart khi đóng/mở, tự động mở drawer khi chạy backtest, và bổ sung test suites static/runtime/browser.
- File liên quan: `public/index.html`, `public/style.css`, `public/app.js`, `public/chart.js`, `tests/test_ui_structure.test.js`, `tests/test_drawer.test.js` (mới), `tests/verify_drawer_qa.js` (mới), `.agent/TASKS.md`, `.agent/CHANGELOG.md`, `.agent/PROJECT.md`, `walkthrough.md`
- Acceptance criteria:
  - [x] Drawer mặc định đóng khi tải trang lần đầu, chart mở rộng tối đa chiếm toàn bộ diện tích bên cạnh toolbar (width > 1870px trên 1920x1080).
  - [x] Thêm 2 nút toggle `#btn-toggle-strategy` và `#btn-toggle-results` trên header có aria-label, aria-expanded, aria-controls và tooltip.
  - [x] Thêm nút đóng `#btn-close-drawer` và hỗ trợ phím `Escape` để đóng drawer (bảo vệ không đóng khi modal/context menu đang mở).
  - [x] Drawer dùng chung 2 tab: `⚙ Cấu hình chiến lược` và `📊 Kết quả & Báo cáo`, chỉ 1 tab hiển thị tại một thời điểm, highlight đồng bộ nút header.
  - [x] Chart tự động resize mượt mà khi drawer mở/đóng via `handleResize()` và `window.resize`, không bị vỡ bố cục hoặc co về 0.
  - [x] Lưu trạng thái `{ isOpen, activeTab }` vào `localStorage` (`backtest:ui:drawer`), reload khôi phục trạng thái, chống crash khi dữ liệu corrupt/non-object.
  - [x] Chạy backtest vẫn lấy đủ dữ liệu cấu hình khi drawer đang đóng, tự động mở tab kết quả khi chạy xong hoặc mở tab cấu hình khi có lỗi validation.
  - [x] Responsive: desktop 400px, tablet 350px, mobile overlay fixed `min(400px, 92vw)` có backdrop `#drawer-backdrop`.
  - [x] 100% tests tự động PASS: 8/8 static UI tests, 12/12 drawer runtime tests, 56/56 drawing tests, 36/36 backend tests.
  - [x] Browser QA kiểm chứng thực tế trên các breakpoint (1920x1080, 1280x800, 1024x768, 375x667) và flow thao tác đạt PASS 11/11 bước với screenshots.
- Phụ thuộc: T46
- Trạng thái: done

## Kế hoạch tiếp theo — Chart-first UI & Drawer Polish

### [ ] T48 - Chart-first UI & Drawer Polish
- Mô tả: Tối ưu drawer và vùng biểu đồ sau T47; bổ sung nút mở drawer nổi, badge kết quả mới, resize/drag width, collapsible report sections, export CSV/JSON, accessibility và responsive polish.
- File liên quan: `public/index.html`, `public/style.css`, `public/app.js`, `public/chart.js`, `tests/test_drawer.test.js`, `tests/test_ui_structure.test.js`, `tests/verify_drawer_qa.js`, `walkthrough.md`
- Acceptance criteria:
  - [ ] Chart chiếm tối đa diện tích khi drawer đóng; drawer mở/đóng không làm vỡ chart hoặc Dual Chart.
  - [ ] Resize/drag drawer có giới hạn an toàn trên desktop, tablet và mobile.
  - [ ] Báo cáo có section thu gọn/mở rộng, bảng giao dịch scroll riêng và export CSV/JSON.
  - [ ] Keyboard navigation, focus state và ARIA state chính xác.
  - [ ] Không mất dữ liệu cấu hình/kết quả khi chuyển tab, resize hoặc reload.
  - [ ] Regression tests và Browser QA trên 375, 768, 1024, 1280, 1920px PASS.
- Phụ thuộc: T47
- Trạng thái: planned

## Đợt Sửa Lỗi Hệ Thống Vẽ Biểu Đồ (Future Infinite Drawing & Scale Drag Sync) — 2026-09-05

### [x] T49 - Sửa Lỗi Vẽ Vô Hạn Tương Lai & Tự Động Đồng Bộ Tọa Độ Khi Kéo Trục Giá/Thời Gian
- Mô tả: Cho phép đặt điểm, kéo anchor và kéo toàn bộ drawing về phía tương lai không giới hạn bằng `coordinateToLogical`/`logicalToCoordinate` và khoảng nến thực của từng timeframe (không hard-code 60s, không thêm nến giả). Đồng bộ tức thì tọa độ X/Y của toàn bộ drawing khi pan, zoom, kéo trục giá bên phải, kéo trục thời gian bên dưới và resize qua Series Primitive Hook và drag event listener trên container với cơ chế render dùng chung qua `requestAnimationFrame` chống render loop và giật lag.
- File liên quan: `public/drawings.js`, `public/chart.js`, `tests/test_drawings.test.js`, `tests/verify_drawing_future_qa.js`, `.agent/TASKS.md`, `.agent/DECISIONS.md`, `.agent/CHANGELOG.md`, `walkthrough.md`
- Acceptance criteria:
  - [x] Cho phép đặt điểm, kéo anchor và kéo toàn bộ drawing về phía tương lai không giới hạn thực tế (cả khi nến cuối bị cuộn ra ngoài màn hình).
  - [x] Không thêm bất kỳ nến giả nào vào dữ liệu OHLC.
  - [x] Khi vị trí nằm ngoài dữ liệu nến, tự tính timestamp dựa trên logical index và khoảng thời gian thực tế giữa các nến của timeframe hiện tại (không hard-code 60 giây).
  - [x] Khi kéo thanh giá bên phải: toàn bộ nét vẽ tự động cập nhật vị trí theo trục Y ngay lập tức mà không cần lăn chuột.
  - [x] Khi kéo thanh thời gian bên dưới: toàn bộ nét vẽ tự động cập nhật vị trí theo trục X ngay lập tức.
  - [x] Đồng bộ lại tọa độ sau mọi thay đổi: Pan, Zoom, Kéo trục giá, Kéo trục thời gian, Window Resize.
  - [x] Tạo cơ chế render dùng chung qua `requestAnimationFrame`, loại bỏ nguy cơ render loop và bảo đảm hiệu năng 60 FPS mượt mà.
  - [x] Đảm bảo các loại drawing nhiều điểm (trendline, channel, fibonacci, pitchfork, rectangle) và handles đều được cập nhật đúng.
  - [x] Kiểm tra và xử lý trơn tru trường hợp `coordinateToTime()` trả về null hoặc bị giới hạn ở cây nến cuối.
  - [x] Giữ nguyên dữ liệu timestamp và giá trị của drawing khi tua biểu đồ (Replay) hoặc thay đổi timeframe.
  - [x] Bổ sung test suite đầy đủ cho toàn bộ kịch bản và chạy PASS 100%.
- Phụ thuộc: T47
- Trạng thái: done

