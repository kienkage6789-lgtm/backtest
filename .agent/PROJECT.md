# PROJECT.md

> File bối cảnh dự án (Bộ nhớ ngoài của Dev Team). Đọc file này đầu tiên khi bắt đầu một phiên làm việc mới.

## Mô tả dự án
Nền tảng web backtest chiến lược giao dịch kỹ thuật mô phỏng theo phong cách TradingView (giao diện dark mode, chart tương tác mượt mà, phân tích nến, chỉ báo và kiểm thử chiến lược giao dịch tự động trên dữ liệu lịch sử).
Dữ liệu lịch sử hiện có: Cặp vàng XAU/USD (M1 - 1 phút) gồm ~3.3 triệu nến trong SQLite database `data/XAUUSD.db`.

## Tech Stack
- **Database**: SQLite3 (`data/XAUUSD.db`, bảng `XAUUSDc_M1`)
- **Backend**: Node.js (Express.js) hoặc Python (FastAPI) để truy vấn nến, nén dữ liệu và tổng hợp khung thời gian (Resample M1 -> M5, M15, H1, H4, D1).
- **Frontend / Charting**: TradingView Lightweight Charts (thư viện chính thức của TradingView, mã nguồn mở, tối ưu cực cao cho nến và chỉ báo).
- **Backtest Engine**: Engine mô phỏng khớp lệnh tick-by-tick / bar-by-bar với slippage, spread và commission.

## Cấu trúc thư mục dự án
```
Web_trading/
├── .agent/                  # External brain memory (Dev Team Workflow)
│   ├── assets/              # Template files
│   ├── references/          # Quy trình 5 bước (Planning, Breakdown, Implementation, Testing, Review)
│   ├── PROJECT.md           # Bối cảnh dự án (file này)
│   ├── TASKS.md             # Danh sách WBS task và trạng thái
│   ├── DECISIONS.md         # Quyết định kiến trúc & kỹ thuật (ADRs)
│   └── CHANGELOG.md         # Nhật ký công việc thực tế
├── data/
│   └── XAUUSD.db            # SQLite DB chứa ~3.3M nến M1 vàng (2014 - nay)
├── plan.md                  # Tài liệu định hướng kiến trúc & lộ trình MVP
└── ...                      # Mã nguồn ứng dụng (sẽ triển khai theo các task)
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

## Yêu cầu hiện tại (Giai đoạn MVP)
1. **Dữ liệu & API**: Xây dựng service truy vấn nến từ `data/XAUUSD.db`, hỗ trợ phân trang (pagination/range query) và resample khung thời gian (M1, M5, M15, H1, D1).
2. **Chart UI**: Giao diện dark theme TradingView, tích hợp Lightweight Charts hiển thị nến mượt mà, hỗ trợ zoom/pan và chuyển timeframe.
3. **Backtest Core**: Engine thực thi chiến lược mẫu (ví dụ: SMA Crossover, RSI, Breakout), tính toán PnL, Winrate, Max Drawdown, Profit Factor.
4. **Báo cáo kết quả**: Hiển thị đường cong vốn (Equity curve) và danh sách chi tiết các lệnh đã thực hiện.

