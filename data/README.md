# Dữ liệu thị trường (Market Data)

Thư mục này dùng để chứa file database SQLite lịch sử nến (OHLCV).

- File yêu cầu: `XAUUSD.db`
- Bảng chính (gốc): `XAUUSD_M1` (chứa nến 1 phút M1 của vàng XAU/USD, ~1.83 triệu nến từ 2016 đến 2026).
- Hỗ trợ alias: `XAUUSDc_M1` (nếu có).
- Cơ chế đa khung thời gian (Multi-timeframe):
  - Khung M1: Query trực tiếp từ bảng gốc `XAUUSD_M1`.
  - Khung M5, M15, M30: Resample động từ `XAUUSD_M1`.
  - Khung H1, H4, D1: Resample động từ `XAUUSD_M1`, hoặc query trực tiếp nếu các bảng `XAUUSD_H1`, `XAUUSD_D1` đã được materialize.
- Index: Tự động đảm bảo index trên cột `time` để tối ưu tốc độ truy vấn khoảng thời gian.

*Lưu ý: File `XAUUSD.db` được cấu hình trong `.gitignore` để không push lên GitHub vì vượt quá giới hạn file size của GitHub.*
