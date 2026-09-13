# Research Framework — Milestone T54

Hệ thống nghiên cứu định lượng và backtest chiến lược SMC (Smart Money Concepts) trên thị trường XAU/USD với cơ chế **2 máy hỗ trợ** (Dual-Machine Paradigm) nhằm đảm bảo tính toàn vẹn dữ liệu, khả năng tái lập 100% và phòng chống overfit / curve-fitting.

---

## 1. Cấu Trúc Thư Mục

```text
research/
├── README.md                           # Tài liệu tổng quan & quy ước nghiên cứu
├── protocol_v1.json                    # Giao thức nghiên cứu bị khóa (Protocol V1)
├── dataset_manifest.json               # Manifest metadata & SHA256 checksums chuẩn
├── data_quality_report.json            # Dữ liệu phân tích chất lượng (Máy 1)
├── data_quality_report.md              # Báo cáo chất lượng dữ liệu chi tiết
├── qc_verification_report.json         # Báo cáo đối chiếu độc lập (Máy 2)
├── scripts/                            # Bộ công cụ chạy nghiên cứu
│   ├── __init__.py
│   ├── data_runner_m1.py               # Máy 1: Data profiler & manifest generator
│   └── independent_qc_m2.py            # Máy 2: Read-only independent auditor
├── runs/                               # Kết quả backtest các pha T54.1 -> T54.6
│   └── .gitkeep
└── final/                              # Báo cáo tổng hợp T54.8
    └── .gitkeep
```

---

## 2. Nguyên Tắc 2 Máy Hỗ Trợ

| Hạng mục | Máy 1 (Runner / Profiler) | Máy 2 (Independent QC) |
|---|---|---|
| **Data Source** | Read-write connection tới `data/XAUUSD.db` | Read-only connection (`?mode=ro`) |
| **Vai trò** | Profiling, gap classification, resampling, sinh manifest | Độc lập query, tính checksum, kiểm toán mẫu ngẫu nhiên |
| **Quy tắc** | Sinh `dataset_manifest.json` & `data_quality_report.*` | Đối chiếu chéo 1-1, không dùng báo cáo Máy 1 làm bằng chứng duy nhất |
| **Quyền sửa code** | Cùng commit hash | Cùng commit hash |

---

## 3. Dataset Chuẩn (Canonical Research Dataset)

- **Symbol**: `XAUUSD`
- **Khoảng thời gian**: `2022-01-01 00:00:00 UTC` đến `2026-08-31 23:59:59 UTC` (56 tháng ~ 4 năm 8 tháng).
- **Lý do loại bỏ 2016–2021**: Dữ liệu trước `2021-03-02` trong database thực chất là nến ngày (D1) bị lưu nhầm vào bảng M1 với timestamp `00:00:00`. Giai đoạn `2022-01-01` trở đi là chuỗi nến M1 chuẩn và liên tục.
- **Phân chia dữ liệu (Data Split)**:
  - **In-Sample (IS - 60%)**: `2022-01-01` $\to$ `2024-09-30` (33 tháng) — Dùng cho baseline và nghiên cứu cơ bản.
  - **Validation (20%)**: `2024-10-01` $\to$ `2025-08-31` (11 tháng) — Dùng cho phân tích độ nhạy chi phí, regime matrix.
  - **Out-of-Sample (OOS - 20%)**: `2025-09-01` $\to$ `2026-08-31` (12 tháng) — Bị khóa mù, chỉ mở một lần duy nhất tại T54.5/T54.8.
