# CHANGELOG.md

> Nhật ký các thay đổi thực tế đã làm, theo thời gian.

## 2026-09-03 - Kích hoạt Skill dev-team-workflow & Đồng bộ bộ nhớ dự án
- File đã đổi: `.agent/PROJECT.md`, `.agent/TASKS.md`, `.agent/DECISIONS.md`, `.agent/CHANGELOG.md`
- Đã làm:
  - Tiếp nhận chỉ thị kích hoạt skill `dev-team-workflow` (`.agent/SKILL.md`).
  - Khảo sát thực tế thư mục làm việc: phát hiện file định hướng `plan.md` ("Xây dựng trang web Backtest giống TradingView") và cơ sở dữ liệu `data/XAUUSD.db` (3,297,714 nến M1 vàng từ 2014 đến nay).
  - Dọn dẹp dữ liệu cũ (từ template game bot), đồng bộ hóa toàn bộ bộ nhớ ngoài (`.agent/`) với dự án Web Trading Backtest.
  - Phân tích và lập cấu trúc WBS gồm 5 tasks chính (T01 -> T05) bao gồm Data API, TradingView Lightweight Charts UI, Backtest Engine, Preset Strategies và Report & Visual Markers.
## 2026-09-03 - Hoàn thành Task T01: Database Indexing & Data API Service
- File đã đổi: `data/XAUUSD.db`, `engine/data_feed.py`, `server.py`, `tests/test_data.py`, `tests/test_api.py`
- Đã làm:
  - Tạo index trên cột `time` cho bảng chính `XAUUSDc_M1`.
  - Tiền tính toán (precompute) và lập index cho bảng `XAUUSD_H1` (57,232 nến) và `XAUUSD_D1` (3,890 nến).
  - Triển khai class `DataFeed` hỗ trợ định tuyến thông minh: M1 query trực tiếp (<25ms), M5/M15/M30 resample từ M1, H1/H4 query từ H1 (<10ms), D1 query trực tiếp (<6ms).
  - Xây dựng API FastAPI (`/api/info`, `/api/candles`) hỗ trợ lọc theo khoảng thời gian, limit và scroll backward.
- Đã test bằng:
  - `python -m unittest tests/test_data.py` (4/4 test PASS: kiểm tra tính đúng đắn toán học của nến OHLCV resample, thời gian query đều < 60ms).
  - `python -m unittest tests/test_api.py` (4/4 test PASS: HTTP 200 cho info/candles/strategies/backtest và 400 khi timeframe không hợp lệ).

## 2026-09-03 - Hoàn thành Task T02: Giao diện Chart TradingView (Dark Mode)
- File đã đổi: `public/index.html`, `public/style.css`, `public/chart.js`, `public/vendor/lightweight-charts.standalone.production.js`
- Đã làm: Tải và tích hợp thư viện TradingView Lightweight Charts bản chạy offline 100%. Thiết kế theme dark mode chuẩn TradingView với crosshair sync, candlestick series, volume histogram, dynamic timeframe switcher và tooltip OHLCV.

## 2026-09-03 - Hoàn thành Task T03 & T04: Backtest Engine & Chiến Lược Mẫu
- File đã đổi: `engine/strategies.py`, `engine/backtest_engine.py`, `tests/test_backtest.py`, `server.py`
- Đã làm: Xây dựng engine mô phỏng khớp lệnh tick/bar-by-bar với Stop Loss, Take Profit, Spread, Commission, Long/Short; tích hợp 4 chiến lược kỹ thuật (SMA Crossover, RSI Reversal, MACD Crossover, Donchian Channel Breakout).
- Đã test bằng: `python -m unittest tests/test_backtest.py` (PASS kiểm thử logic SL/TP và kiểm thử dữ liệu thực tế H1).

## 2026-09-03 - Hoàn thành Task T05: Báo Cáo Trực Quan (Equity Curve & Markers) & Start Launcher
- File đã đổi: `public/app.js`, `public/index.html`, `tests/test_server_static.py`, `start.bat`
- Đã làm: Tích hợp bảng thống kê hiệu suất (Net PnL, Win Rate, Profit Factor, Max Drawdown), đồ thị đường cong tăng trưởng vốn (Equity Curve), bảng nhật ký từng lệnh và tự động gán nhãn marker BUY/SELL/EXIT trực tiếp trên nến biểu đồ; tạo script `start.bat` để chạy server 1 chạm.
- Đã test bằng: `python -m unittest discover tests` (Toàn bộ 13/13 unit & integration tests PASS trong 1.26s).

## 2026-09-03 - Hoàn thành Task T06: Tính năng Tua Nến (Bar Replay) & Đa Khung Thời Gian (Multi-Timeframe)
- File đã đổi: `engine/data_feed.py`, `server.py`, `public/chart.js`, `public/style.css`, `public/index.html`, `public/app.js`, `tests/test_replay.py`
- Đã làm:
  - Backend: Bổ sung phương thức `get_replay_candles` trong `DataFeed` hỗ trợ tách lịch sử trước điểm cắt và đệm tương lai sau điểm cắt; xây dựng endpoint `/api/replay/init`.
  - Frontend: Xây dựng thanh điều khiển nổi **Replay Toolbar** (Play/Pause, Step Forward, Speed Selector, Cut Mode, Exit) với hiệu ứng trượt mượt mà.
  - Tích hợp công cụ chọn nến trực tiếp: Khi bật chế độ cắt nến, con trỏ đổi sang crosshair, click vào bất kỳ cây nến nào trên chart sẽ lập tức tua nến về thời điểm đó.
  - Hỗ trợ Đa Khung Thời Gian: Chuyển đổi linh hoạt giữa các khung (M1, M5, M15, H1, D1) trong khi replay mà vẫn giữ nguyên vị trí thời gian lịch sử; hỗ trợ chế độ **Dual Chart** (mở 2 biểu đồ song song với 2 khung thời gian khác nhau, ví dụ H1 và M15, đồng bộ nhịp nến khi tua).
- Đã test bằng:
  - `python -m unittest tests/test_replay.py` (3/3 test PASS: kiểm tra tính tuần tự nến history/future trên M15, H1, D1 và endpoint `/api/replay/init`).
  - `python -m unittest discover tests` (Toàn bộ 16/16 unit & integration test PASS).
  - Kiểm thử trực tiếp HTTP API `/api/replay/init` trên server thật: Status 200 OK.

