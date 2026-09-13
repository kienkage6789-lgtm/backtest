import os

def update_changelog():
    path = ".agent/CHANGELOG.md"
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    marker = "## 2026-09-12 - T53.9.2: Sửa Lỗi QC P1"
    pos = text.find(marker)
    if pos == -1:
        raise ValueError("Marker not found in CHANGELOG.md")

    entry = """## 2026-09-12 - T53.9.3: Bar-by-bar SMC Coordinator, Cooldown-after-fill và HTF as-of timeline (review — awaiting independent QC)
- **Triển khai SMCBacktestCoordinator & HTFTimeline trong `smc/engine/backtest_adapter.py`**:
  - Chu trình canonical bar 4 pha độc lập, tuần tự và xác định:
    1. Open phase: lấy pending intent duy nhất từ Close i-1, revalidate hình học, tính actual entry/SL/TP với spread, tính cash RR (tính cả commission round-trip), áp dụng position policy (flat fill, same-direction skip, opposite-direction atomic reversal, allow_short guard).
    2. Intrabar phase: chuyển giao sang ExecutionKernel, bid OHLC cho BUY, ask OHLC cho SELL, bảo thủ ưu tiên SL-first khi va chạm SL/TP cùng nến, phát sinh event POSITION_CLOSED.
    3. Close phase: nạp HTF events qua HTFTimeline (`effective_time <= bar_close_time`), build immutable StrategyContext, phân loại MarketRegime, đánh giá strategies, EligibilityGate (tích hợp CooldownBook kiểm tra `cooldown_active`), Confluence deduplication, StrategySelector, sinh SelectionDecision (giữ `execution_payload` rỗng `{}` theo T53.8), lên lịch PendingExecutionIntent cho bar i+1 hoặc ORDER_CANCELLED/no_next_bar nếu là bar cuối.
    4. Mark-to-market: tính floating PnL/equity tại close bar hiện tại, không gây lookahead.
  - Hỗ trợ đầy đủ 4 chế độ: `smc_wave1` (S01+S05+S09+Selector), `smc_s01`, `smc_s05`, `smc_s09`.
- **Triển khai CooldownBook**:
  - Key `(strategy_id, direction)` độc lập; chặn đúng khoảng `[F, F+K-1]`, giải phóng tại `F+K`.
  - Chỉ successful fill (`ORDER_FILLED`) mới kích hoạt cooldown; reject và same-direction skip không tạo cooldown.
  - Supporting strategy không bị khóa khi primary strategy fill.
  - Snapshot immutable, JSON-safe và khôi phục deterministic qua `from_snapshot()`.
- **Triển khai PendingExecutionIntent**:
  - Frozen, deep-immutable, stable ID `intent:{signal_bar}:{decision_id}:{strategy_id}:{direction}`, không mutate `SelectionDecision`.
  - Lưu đầy đủ metadata audit: planned levels, expiry, regime, session, selector score, cluster ID, evidence IDs.
- **Triển khai ExecutionEvent schema 1.0.0**:
  - Phát sinh đầy đủ 6 loại event: `ORDER_SELECTED`, `ORDER_REJECTED`, `ORDER_SKIPPED`, `ORDER_FILLED`, `ORDER_CANCELLED`, `POSITION_CLOSED`.
  - Reasons namespace chuẩn hóa: `geometry_violation_at_fill`, `insufficient_rr_at_fill`, `position_already_open_same_direction`, `short_disabled`, `cooldown_active`, `no_next_bar`, `stop_loss`, `take_profit`, `opposite_signal`, `forced_close`.
  - JSON round-trip 100%, không chứa NaN/Inf/custom objects.
- **Parity & Invariance Tuyệt Đối**:
  - Bit-for-bit parity giữa batch execution và incremental `step()` execution.
  - Replay-prefix invariance: dữ liệu nến tương lai thêm vào không làm thay đổi các decisions/events của prefix lịch sử.
  - Idempotent: gọi duplicate step cùng bar với cùng input trả cached result, không duplicate fill/cooldown/trade.
- **Kiểm Thử & Đảm Bảo Chất Lượng**:
  - `tests/test_smc_engine_backtest_adapter.py`: **24/24 tests PASS** (Groups A-G).
  - `tests/test_smc_engine_execution.py`: **51/51 tests PASS**.
  - `tests/test_smc_engine_selector.py`: **125/125 tests PASS**.
  - `tests/test_smc_engine_telemetry.py`: **60/60 tests PASS**.
  - `tests/test_smc_engine_t53_8_integration.py`: **15/15 tests PASS** (1 skipped opt-in benchmark).
  - `tests/test_backtest_legacy_compat.py`: **6/6 tests PASS**.
  - Toàn bộ suite discovery: **1105/1105 tests PASS** (skipped=2).
  - Node UI tests: **87/87 tests PASS**.
  - Independent QC probe (`scratch/probe_qc_t53_9_3.py`): **10/10 probes PASS** (exit code 0).
  - Static checks: `compileall` sạch, `git diff --check` sạch.
- **Ranh Giới**: Không triển khai T53.9.4 và T53.9.5. Giữ trạng thái `T53.9.3: review — awaiting independent QC`.

"""
    updated = text[:pos] + entry + text[pos:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    print("SUCCESS")

if __name__ == "__main__":
    update_changelog()
