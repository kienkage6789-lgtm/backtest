# Dữ liệu thị trường (Market Data)

Thư mục này dùng để chứa file database SQLite lịch sử nến (OHLCV).

- File yêu cầu: `XAUUSD.db`
- Bảng chính: `XAUUSDc_M1` (chứa nến 1 phút M1 của vàng XAU/USD)
- Các bảng phụ được tự động tạo và lập chỉ mục khi khởi chạy server: `XAUUSD_H1`, `XAUUSD_D1`.

*Lưu ý: File `XAUUSD.db` (~312MB) được cấu hình trong `.gitignore` để không push lên GitHub vì vượt quá giới hạn 100MB của GitHub.*
