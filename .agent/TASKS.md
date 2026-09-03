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

