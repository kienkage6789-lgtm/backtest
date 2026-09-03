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
