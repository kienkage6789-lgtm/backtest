# PROJECT.md

> File bối cảnh dự án (Bộ nhớ ngoài của Dev Team). Đọc file này đầu tiên khi bắt đầu một phiên làm việc mới.

## Mô tả dự án
Nền tảng web backtest chiến lược giao dịch kỹ thuật mô phỏng theo phong cách TradingView (giao diện dark mode, chart tương tác mượt mà, Bar Replay tua nến quá khứ, Dual Chart đa khung thời gian song song, hệ thống Drawing Tools 39 loại công cụ vẽ / 41 metadata entries, Drawing Alerts Engine 13 types và kiểm thử chiến lược giao dịch tự động trên dữ liệu lịch sử).
Dữ liệu lịch sử hiện có: Cặp vàng XAU/USD (M1 - 1 phút) gồm 1,831,773 nến (2016 - 2026) trong SQLite database `data/XAUUSD.db` (bảng `XAUUSD_M1`, index `idx_XAUUSD_M1_time`).

## Tech Stack & Môi Trường
- **Database**: SQLite3 (`data/XAUUSD.db`, bảng chính `XAUUSD_M1`, hỗ trợ alias `XAUUSDc_M1`, có index `time`).
- **Backend**: Python 3.10+ (FastAPI + Uvicorn + Pydantic + Pandas + NumPy) truy vấn nến siêu tốc (<15ms cho 5,000 nến), nén dữ liệu và resample thông minh M1 -> M5, M15, M30, H1, H4, D1.
- **Python Virtual Environment (.venv)**: Môi trường cô lập chuẩn hóa tại `.venv`, phụ thuộc khai báo trong `requirements.txt`.
- **Frontend / Charting**: TradingView Lightweight Charts v4.1.3 (vendor offline 100%) kết hợp lớp tương tác SVG Overlay cho Drawing Tools.
- **Drawing Engine Core v2**: Tự phát triển không license (`public/drawings.js`), hỗ trợ 39 drawing types (41 metadata entries), 8 danh mục, SVG interaction state machine, Magnet Snap (25px), Undo/Redo 50 bước, LocalStorage key `drawings:XAUUSD:layout`, transactional backup key `drawings:XAUUSD:layout:backup`, JSON import/export và cô lập Dual Chart (`drawings:XAUUSD:layout:secondary`).
- **Drawing Alerts Engine**: State machine kiểm tra giá/thời gian thời gian thực cho 13 types, phân định `inside` dựa trên `close` chống nhiễu wick, `touch` theo biên độ nến, interval overlap cho date range, crossline dual condition và audio guard.
- **Backtest Engine**: Engine mô phỏng khớp lệnh bar-by-bar không lookahead, tính phí spread đối xứng cho Long/Short, tính commission 2 chiều, kích hoạt SL/TP chuẩn xác và forced close cuối kỳ.

## Thiết lập Môi trường Chạy & Kiểm thử
```powershell
# 1. Khởi tạo và kích hoạt virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# (Nếu PowerShell bị giới hạn execution policy: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass hoặc gọi .\.venv\Scripts\python.exe trực tiếp)

# 2. Cài đặt dependencies chính thức
python -m pip install -r requirements.txt

# 3. Chạy toàn bộ kiểm thử
python -m unittest discover tests -v
node --test tests/test_drawings.test.js
```

## Cấu trúc thư mục dự án
```
Web_trading/
├── .agent/                  # Bộ nhớ ngoài của Dev Team
│   ├── references/          # Quy trình 5 bước (Planning, Breakdown, Implementation, Testing, Review)
│   ├── PROJECT.md           # Bối cảnh dự án (file này)
│   ├── TASKS.md             # Danh sách WBS tasks và trạng thái thực tế
│   ├── DECISIONS.md         # Quyết định kiến trúc & kỹ thuật (ADR 01 -> ADR 09)
│   └── CHANGELOG.md         # Nhật ký công việc và kết quả kiểm thử thực tế
├── data/
│   ├── XAUUSD.db            # SQLite DB chứa 1,831,773 nến M1 vàng (2016 - 2026)
│   └── README.md            # Tài liệu schema và index
├── engine/
│   ├── data_feed.py         # DataFeed đọc DB, query range, resample đa khung, Bar Replay
│   ├── strategies.py        # 4 chiến lược mẫu (SMA, RSI, MACD, Donchian)
│   └── backtest_engine.py   # Core mô phỏng khớp lệnh SL/TP, spread, commission, MDD
├── public/
│   ├── vendor/              # Lightweight Charts standalone production JS (offline)
│   ├── index.html           # Layout TV Dark Theme, Left Toolbar, Style Bar, Replay Controls
│   ├── style.css            # Stylesheet hoàn chỉnh (Dark Theme, Drawing Toolbar, Overlay)
│   ├── drawings.js          # Drawing Engine lõi: DrawingGeometry, DrawingModel, DrawingManager, Alert Engine
│   ├── chart.js             # Wrapper TradingChart, tích hợp DrawingManager & ResizeObserver
│   └── app.js               # Logic điều khiển, API calls, ReplayManager, Hotkeys, Dual Chart, Property Dialog
├── tests/
│   ├── test_data.py         # 10 unit tests cho DataFeed
│   ├── test_api.py          # 6 tests cho FastAPI endpoints
│   ├── test_backtest.py     # 10 tests cho Backtest Engine
│   ├── test_replay.py       # 7 tests cho Bar Replay & Dual Chart Sync
│   ├── test_server_static.py# 3 tests cho serving file tĩnh
│   ├── test_drawings.test.js# 56 unit & integration tests Node.js cho Drawing & Alert Engine
│   ├── test_ui_structure.test.js # 8 tests kiểm tra tĩnh cấu trúc DOM và HTML balance
│   ├── test_drawer.test.js  # 10 unit tests runtime cho Collapsible Drawer
│   ├── verify_browser_qa.js # Script Browser QA 17 bước
│   └── verify_drawer_qa.js  # Script Browser QA 11 bước cho Drawer
├── .gitattributes           # Chuẩn hóa LF line-ending cho js, py, md, css, html
├── requirements.txt         # Manifest dependencies đã được pin version
├── server.py                # FastAPI HTTP Server & Static files mounting
└── start.bat                # Script khởi chạy 1-click cho người dùng Windows
```

## Quy ước Dev Team
- **Quy trình chuẩn**: Tuân thủ nghiêm ngặt 5 bước trong `.agent/SKILL.md`:
  1. Lên ý tưởng / Spec (`references/01-planning.md`)
  2. Chia việc (`references/02-task-breakdown.md`)
  3. Code (`references/03-implementation.md`)
  4. Test (`references/04-testing.md`)
  5. Review (`references/05-code-review.md`)
- **Nguyên tắc chống bịa code**: Đọc file thật trước khi sửa, kiểm tra thư viện/database thật trước khi gọi.
- **Nguyên tắc chống quên ngữ cảnh**: Cập nhật `TASKS.md`, `DECISIONS.md`, `CHANGELOG.md` liên tục trên đĩa.
- **Test-first / Verification**: Không báo hoàn thành nếu chưa chạy lệnh test và kiểm tra kết quả thực tế.

## Trạng thái tính năng hiện tại (T01 -> T47)
1. **Dữ liệu & API**: Đọc DB thật 1.83M nến, resample M1 -> D1 siêu tốc, range query tôn trọng limit.
2. **Chart UI & Responsive Layout**: Giao diện dark theme TradingView, Lightweight Charts offline, zoom/pan mượt mà, cấu trúc DOM phân cấp chuẩn xác không lỗi lặp, co giãn responsive bền bỉ ở mọi độ phân giải (1024x768, 1280x800, 1440x900, 1920x1080).
3. **Collapsible Strategy & Results Drawer (T47)**: Panel bên phải chuyển thành drawer 2 tab thu gọn, mặc định đóng để chart mở rộng 100% diện tích làm việc (>1870px), 2 nút toggle độc lập trên header (`#btn-toggle-strategy`, `#btn-toggle-results`), nút đóng ✕, phím tắt Escape, backdrop mobile, auto-resize chart không vỡ bố cục, persistence an toàn qua `localStorage` (`backtest:ui:drawer`), tự động bung kết quả khi hoàn thành backtest.
4. **Backtest Core**: Khớp lệnh không lookahead, spread đối xứng Long/Short, SL/TP chính xác, forced close cập nhật đầy đủ.
5. **Báo cáo kết quả**: Equity Curve, thống kê Net PnL, Winrate, MDD, nhật ký lệnh và chart markers.
6. **Bar Replay & Multi-Timeframe**: Tua nến quá khứ, click cắt nến, Dual Chart đồng bộ qua while loop.
7. **Drawing Tools (39 công cụ / 41 entries)**: 8 categories, Favorites bar, Property Dialog 5 tab có snapshot rollback, Undo/Redo 50 bước, LocalStorage Schema v2 và transactional backup.
8. **Drawing Alerts Engine**: Giám sát giá/thời gian thời gian thực cho 13 types, phân biệt touch/cross/enter/exit, guards hidden/locked và deduplication nến.
9. **Hiệu năng & Kiểm thử**: 74 tests JS PASS (56 Drawing/Alerts + 8 UI Structure + 10 Drawer Runtime) + 36 tests Python PASS trong .venv, CPU/JS processing time < 4ms, Browser Visual QA 11/11 bước cho Drawer PASS 100% kèm ảnh chụp kiểm chứng thực tế.

