# T53.9 — Backtest Integration & Independent QC

> Trạng thái: **MILESTONE T53.9 ĐÃ HOÀN TẤT — GATE E PASS (T53.9.0–T53.9.6 done; 15/15 independent probes PASS; 0 P0/P1; P2 benchmark deferred technical debt)**
> Phụ thuộc: **T53.8 đã done**
> Phạm vi: ghép pipeline S01/S05/S09 → Gate → Confluence → Selector vào BacktestEngine, khớp lệnh đúng Open N+1, ghi audit thực thi và kích hoạt cooldown sau fill.
> Không thuộc phạm vi: tối ưu tham số, walk-forward, UI overlay, live broker, limit order, pyramiding, portfolio nhiều symbol.

## 1. Mục tiêu

T53.9 biến kết quả lựa chọn tại closed bar N thành giao dịch có thể kiểm chứng tại Open bar N+1 mà không phá hành vi backtest cũ. Sau task này hệ thống phải chạy được bốn chế độ:

1. `smc_wave1`: chạy đồng thời S01, S05, S09 và chọn tối đa một setup mỗi bar.
2. `smc_s01`: chỉ chạy S01 qua cùng Gate/Selector/Execution path.
3. `smc_s05`: chỉ chạy S05 qua cùng Gate/Selector/Execution path.
4. `smc_s09`: chỉ chạy S09 qua cùng Gate/Selector/Execution path.

`smc_confluence` hiện hữu tiếp tục là baseline legacy; không đổi semantics hoặc kết quả của các strategy cũ.

## 2. Kết quả đầu ra bắt buộc

- Một coordinator bar-by-bar chạy pipeline phân tích ở Close N và execution ở Open N+1.
- Fill gate kiểm tra geometry và effective cash RR sau spread/commission.
- Cooldown theo `(strategy_id, direction)` chỉ bắt đầu sau khi lệnh thực sự fill.
- `execution_events` đầy đủ để giải thích pending, fill, reject, skip, close và end-of-data.
- Trade record chứa owner/supporting strategy, setup/cluster/evidence, regime/session, score và timing.
- API backtest hỗ trợ bốn mode mới nhưng giữ response cũ tương thích ngược.
- Batch, incremental và replay-equivalent path cho cùng kết quả trước cùng cutoff.
- Full regression, quality checks, benchmark quan sát và independent read-only QC không còn P0/P1.

## 3. Gate A — contract phải phê duyệt trước khi code

### 3.1. Thứ tự xử lý canonical trên mỗi bar `i`

Không được precompute toàn bộ signal rồi mới chạy execution, vì trạng thái fill tại bar `i` quyết định cooldown của các lần đánh giá sau. Coordinator phải dùng đúng thứ tự:

1. **Open phase `i`**: lấy duy nhất decision được tạo tại Close `i-1`.
2. Revalidate setup tại giá Open `i`; tính actual entry/SL/TP và cash RR.
3. Nếu pass, xử lý position policy rồi mới fill; chỉ fill thành công mới cập nhật cooldown.
4. **Intrabar phase `i`**: kiểm tra SL/TP bằng OHLC của bar `i`; nếu cùng chạm thì SL trước.
5. **Close phase `i`**: cập nhật HTF timeline đã effective, build immutable `StrategyContext`, classify regime, evaluate strategy, Gate, dedup/confluence và selector.
6. Ghi telemetry/decision ở Close `i`; decision SELECT trở thành pending duy nhất cho Open `i+1`.
7. Mark-to-market equity sau khi trạng thái bar `i` đã hoàn tất.

Bar đầu không có pending decision. Decision ở bar cuối vẫn được audit nhưng không được fill; ghi event `ORDER_CANCELLED/no_next_bar` sau khi kết thúc dữ liệu.

### 3.2. Entry policy Wave 1

- Chỉ dùng **market-at-next-open**.
- `CandidateSetup.entry_price` là `planned_entry`, phục vụ setup geometry, scoring proxy và audit; không phải limit order.
- Không backfill fill về bar tín hiệu, không dùng Close N, không dùng giá tốt hơn trong High/Low N+1.
- Limit/retest pending order, partial fill và slippage model nâng cao được hoãn sang task riêng sau khi có dữ liệu baseline.

### 3.3. Giá thực thi và geometry

Với `spread = spread_points * point_value`:

- BUY: `actual_entry = Open[N+1] + spread`, `actual_sl = signal_sl`, `actual_tp = signal_tp`.
- SELL: `actual_entry = Open[N+1]`, `actual_sl = signal_sl + spread`, `actual_tp = signal_tp + spread`.
- BUY hợp lệ khi `actual_sl < actual_entry < actual_tp`.
- SELL hợp lệ khi `actual_tp < actual_entry < actual_sl`.
- Gap qua SL/TP hoặc geometry sai: không đóng vị thế đang có, không fill lệnh mới, ghi `ORDER_REJECTED/geometry_violation_at_fill`.

SL/TP sau fill giữ quy ước engine hiện tại:

- BUY kiểm tra trên Bid OHLC.
- SELL kiểm tra `High + spread` cho SL và `Low + spread` cho TP.
- Nếu một bar chạm cả SL và TP, ưu tiên SL.
- Không dùng global `stop_loss_points/take_profit_points` để bật/tắt trigger cho position có dynamic levels.

### 3.4. Cash-basis RR tại fill

Với round-trip commission `2 * commission_per_side`:

```text
risk_cash   = abs(actual_entry - actual_sl) * lot_size * contract_size
              + 2 * commission_per_side
reward_cash = abs(actual_tp - actual_entry) * lot_size * contract_size
              - 2 * commission_per_side
effective_rr = reward_cash / risk_cash
```

Reject bằng `insufficient_rr_at_fill` nếu `risk_cash <= 0`, `reward_cash <= 0`, non-finite hoặc `effective_rr < profile.min_rr`. Exact boundary `effective_rr == min_rr` phải pass.

`commission_per_side = commission_per_lot * lot_size`; phải dùng cùng công thức PnL/commission đang được BacktestEngine áp dụng, không thu phí trùng.

### 3.5. Position và reversal policy

- Tối đa một open position, không pyramiding.
- Không có position: valid pending decision được fill bình thường.
- Đã có position cùng hướng: không thêm lệnh; ghi `ORDER_SKIPPED/position_already_open_same_direction`; không kích hoạt cooldown mới.
- Đã có position ngược hướng: phải revalidate lệnh mới trước. Nếu lệnh mới fail, giữ nguyên position cũ. Nếu pass, đóng position cũ tại cùng Open theo spread/commission hiện hữu rồi mở position mới.
- `allow_short=False`: SELL bị skip với `ORDER_SKIPPED/short_disabled`; không tác động position/cooldown.
- Cooldown không thay thế position guard và không được tính từ setup SELECT nhưng không fill.

### 3.6. Cooldown contract

- Key: `(strategy_id, direction)` của primary owner đã fill.
- Với `fill_bar = F`, `cooldown_bars = K`, signal bars `F .. F+K-1` bị chặn; bar `F+K` được phép trở lại.
- Gate thêm `cooldown_active` vào canonical rejection reasons cho candidate của owner tương ứng.
- Supporting strategy không bị cooldown chỉ vì primary owner fill.
- Fill reversal hợp lệ cũng kích hoạt cooldown của owner mới.
- State cooldown phải reset giữa hai run và serialize/snapshot được cho parity test.

### 3.7. HTF input và zero-lookahead

- Core adapter nhận **explicit HTF `StructureEvent` timeline** hoặc một provider có output tương đương; không tự gọi LTF event là HTF.
- Event chỉ được đưa vào `StrategyContextBuilder` khi `effective_time <= LTF bar_close_time`.
- Timeline phải sort deterministic theo `(effective_time, event.index, stable payload key)`, deduplicate và fail fast khi cùng identity nhưng khác payload.
- Future HTF event, late-arriving event và exact close-time boundary phải dùng contract hiện có của `HTFBiasTracker`.
- API helper có thể dựng HTF candles từ `DataFeed`, nhưng phải map timeframe qua bảng cấu hình được khóa; không ngầm đoán. Đề xuất mặc định Wave 1: M1→M15, M5→H1, M15→H1.
- Nếu API không lấy/dựng được HTF timeline, fail closed với lỗi rõ ràng; không silently chạy bias `None` hoặc cùng-timeframe substitute.

### 3.8. Timestamp contract

- Dữ liệu đầu vào phải monotonic, unique, timezone-aware. API chuẩn hóa timestamp DB là UTC trước khi vào core adapter.
- `signal_bar = N`, `signal_time = context.bar_close_time`.
- `fill_bar = N+1`, `fill_time = candle[N+1].time` (Open time).
- Trade metadata lưu thêm `signal_open_time` nếu cần truy nguyên candidate timestamp; không đánh đồng open time và close time.

### 3.9. Reason-code layering

Giữ nguyên 18 canonical pipeline reason codes. Execution lifecycle dùng namespace riêng:

- Rejection: `geometry_violation_at_fill`, `insufficient_rr_at_fill`.
- Skip/cancel: `position_already_open_same_direction`, `short_disabled`, `no_next_bar`.
- Close: `stop_loss`, `take_profit`, `opposite_signal`, `forced_close`.

Không đưa skip/cancel reason vào Eligibility Gate và không thay đổi ý nghĩa telemetry T53.8.

## 4. Thiết kế kiến trúc

### 4.1. Tách ba tầng

```text
SMC analysis (Close N)
  Context → Registry → Regime → Eligibility → Confluence → Selector
                         │
                         ▼
              PendingExecutionIntent
                         │
                    Open N+1
                         ▼
Execution kernel → fill/reject/position/SL-TP/PnL/events
                         │
                         ▼
          cooldown state + trade/audit result
```

1. `smc/engine/backtest_adapter.py`: orchestration SMC, HTF timeline, cooldown và chuyển SelectionDecision thành intent.
2. `smc/engine/execution.py`: strict immutable execution models/config, fill validation và event serialization.
3. `engine/backtest_engine.py`: shared execution kernel và public compatibility facade.

Không để `engine/backtest_engine.py` import strategy implementation cụ thể. Adapter được gọi qua dispatch có chủ đích để tránh vòng phụ thuộc giữa `engine.strategies.StrategyRegistry` legacy và `smc.engine.StrategyRegistry`.

### 4.2. Domain models dự kiến

- `ExecutionConfig`: point value, lot, contract, spread, commission, min RR fallback, allow short; strict finite/non-negative validation, reject bool-as-number.
- `PendingExecutionIntent`: decision/setup snapshot, signal bar/time, planned levels, owner/supporting IDs và score snapshot.
- `ExecutionEvent`: version, event ID, event type, reason, signal/fill bar-time, planned/actual levels, effective RR/cash risk/reward, strategy/setup/cluster IDs, immutable metadata.
- `CooldownBook`: state private, `is_active`, `record_fill`, `snapshot`, `reset`; deterministic and idempotent.

Mọi model có `to_dict/from_dict`, JSON-safe, reject missing/extra key, NaN/Inf, type coercion và mutable nested payload. Không sửa `SelectionDecision.execution_payload`: T53.8 vẫn yêu cầu mapping rỗng; adapter tạo intent bên ngoài decision.

### 4.3. Backtest result V2 tương thích ngược

Giữ nguyên bắt buộc:

- `metrics`, `trades`, `equity_curve`, `markers`.
- Legacy calls `BacktestEngine.run(df, strategy_id, params)` và strategy IDs cũ cho cùng output hiện tại.

Bổ sung khi chạy SMC Wave 1:

- `selection_audit`
- `execution_events`
- `regime_summary`
- `strategy_funnel`
- `run_metadata` gồm mode, schema versions, LTF/HTF timeframe, config fingerprint và counts.

Trade record bổ sung metadata nhưng không đổi field cũ. Public JSON phải không có `MappingProxyType`, tuple lạ, NumPy scalar, Timestamp object, NaN hoặc Inf.

## 5. Phân rã thực hiện

### T53.9.0 — Gate A, ADR và fixtures chuẩn

- Chốt toàn bộ semantics mục 3 trong ADR 25.
- Tạo synthetic fixture nhỏ có signal N, gap Open N+1, spread và commission để làm golden execution vector.
- Khóa expected event/trade JSON trước khi triển khai production.

Acceptance:

- Không còn câu hỏi mở về fill, reversal, cooldown, HTF mapping, timestamp, final-bar hoặc event reasons.
- Golden vectors tự tính tay cho BUY, SELL, geometry reject và RR reject.

### T53.9.1 — Execution models và pure fill gate

- Thêm `smc/engine/execution.py` và exports.
- Implement strict models, stable event ID, cash-RR calculator và pure `validate_fill()`.
- Không đụng strategy evaluation ở bước này.

Acceptance:

- Exact JSON round-trip và tamper probes.
- BUY/SELL spread transforms đúng.
- Boundary RR, zero/negative/non-finite, post-rounding geometry và commission pass.
- Pure function không mutate decision/setup/config.

### T53.9.2 — Shared execution kernel và legacy compatibility

Plan triển khai chi tiết: `T53_9_2_SHARED_EXECUTION_KERNEL_IMPLEMENTATION_PLAN.md`.

- Tách state transition position/PnL/SL-TP từ `BacktestEngine` thành kernel dùng chung hoặc helper private rõ contract.
- Sửa trigger dynamic SL/TP dựa trên level của position, không dựa vào global config.
- Giữ public legacy `run()` và kết quả strategy cũ.
- Thêm entry point nội bộ để adapter gửi validated intent vào Open phase.

Acceptance:

- Existing `tests/test_backtest.py` pass không sửa expected để che regression.
- Golden legacy snapshots trước/sau giống nhau cho SMA/RSI/MACD/Donchian/SMC legacy.
- SL-first, short Ask, commission, reversal và forced close đúng.

### T53.9.3 — Cooldown và bar-by-bar SMC coordinator

- Implement `CooldownBook` và đưa read-only cooldown view vào Eligibility Gate bằng optional argument mặc định rỗng để không phá T53.7/T53.8.
- Implement coordinator theo đúng Open → intrabar → Close order.
- Hỗ trợ single strategy mode và all-strategy mode qua cùng code path.
- Feed explicit HTF events as-of bar close.
- Reset toàn bộ ContextBuilder, Registry, Gate state, strategy state, Selector/telemetry, cooldown và execution kernel giữa runs.

Acceptance:

- Reject tại fill không kích cooldown.
- Fill kích cooldown đúng boundary; supporting owner không bị khóa nhầm.
- Duplicate/retry cùng bar không double fill/event/cooldown.
- Không có future event/candidate/decision ảnh hưởng prefix cũ.

### T53.9.4 — Data/API integration và mode exposure

- Thêm bốn strategy IDs mới vào danh mục API nhưng giữ `smc_confluence` legacy.
- Dispatcher chuyển riêng Wave 1 IDs sang adapter; legacy IDs vẫn dùng path hiện tại.
- Dựng/nhận HTF timeline với mapping cấu hình được công khai trong run metadata.
- Response chỉ thêm optional V2 fields cho Wave 1; frontend hiện hữu không bắt buộc thay đổi.
- Validate Wave 1 timeframe theo profile; lỗi 400 rõ ràng cho timeframe/input unsupported.

Acceptance:

- `/api/strategies` không có ID trùng và mô tả đúng market-at-next-open.
- `/api/backtest` legacy response tương thích; Wave 1 response JSON-safe.
- H1 default cũ không silently chạy strategy chỉ hỗ trợ M1/M5/M15.

### T53.9.5 — Integration, parity và performance evidence

- Test end-to-end cho S01, S05, S09 riêng và `smc_wave1`.
- Test conflict, dedup, max one selected/bar, max one position, same/opposite position policy.
- Test future-append invariance trên mọi output trước cutoff.
- Test batch/incremental/replay-equivalent parity của contexts, decisions, events, trades và metrics.
- Test malformed dynamic fields fail closed: thiếu cột, partial fields, NaN/Inf, invalid type và invalid timestamp.
- Benchmark tách component timing và end-to-end 10.000 bars.

Performance policy:

- Benchmark timing là opt-in theo ADR 19; không biến nhiễu máy thành lỗi correctness.
- Báo median/warm-up, cấu hình, Python version, CPU nếu lấy được và counts của contexts/candidates/decisions/events/trades.
- Ghi baseline thực đo; không tự đặt hard threshold mới. T53.PERF vẫn là technical debt riêng.

### T53.9.6 — Documentation, self-review và independent QC

- Cập nhật `walkthrough.md`, `.agent/TASKS.md`, `.agent/CHANGELOG.md`, ADR status và tài liệu schema.
- Chạy targeted tests, full Python, Node regression, compileall, whitespace/conflict-marker scan và `git diff --check`.
- Independent QC bằng GPT-5.6 Sol mức medium nếu môi trường có; read-only, không sửa code.
- QC phải tự tạo probes chứ không chỉ đọc test/report của implementer.

Acceptance:

- Không còn P0/P1.
- P2 phải sửa hoặc được ghi debt có lý do, owner và task đích; không tự tuyên bố PASS khi còn correctness/no-lookahead issue.
- Chỉ chuyển T53.9 `done` sau Gate E.

## 6. Ma trận test tối thiểu

| Nhóm | Probe bắt buộc |
|---|---|
| Timing | Signal N không fill N; chỉ fill Open N+1; final signal không có N+1 |
| BUY | Spread vào entry; SL/TP Bid; geometry và exact RR boundary |
| SELL | Entry Bid; SL/TP cộng spread; Ask trigger; geometry và exact RR boundary |
| Gap | Open N+1 xuyên SL hoặc TP phải reject, không backfill intrabar |
| Collision | Cùng bar chạm SL+TP phải đóng SL |
| Position | same-direction skip; opposite valid reversal; opposite invalid giữ position cũ |
| Cooldown | chỉ sau fill; K-bar boundary; reset; owner/supporting isolation |
| HTF | future hidden; exact boundary visible; late event deterministic; conflicting duplicate fail |
| Selector | tối đa một pending; conflict gap 15; score 60 boundary; no-trade không tạo intent |
| Parity | batch/incremental/replay JSON exact trên prefix |
| Compatibility | 5 legacy strategies và API fields cũ không regression |
| Serialization | missing/extra keys, bool-as-number, NumPy scalars, NaN/Inf, nested mutation |
| Accounting | spread, commission, close/reversal/forced close, metrics và equity |

## 7. File dự kiến thay đổi

Production:

- `smc/engine/execution.py` — mới.
- `smc/engine/backtest_adapter.py` — mới.
- `smc/engine/eligibility.py` — optional cooldown view.
- `smc/engine/__init__.py` — exports.
- `engine/backtest_engine.py` — shared kernel/dynamic execution compatibility.
- `engine/strategies.py` — mode catalog/dispatch tối thiểu.
- `server.py` — Wave 1 request/response và HTF input adapter.

Tests:

- `tests/test_smc_engine_execution.py` — mới.
- `tests/test_smc_engine_backtest_adapter.py` — mới.
- `tests/test_smc_engine_t53_9_integration.py` — mới.
- `tests/test_backtest.py`, `tests/test_api.py` — regression/API extensions.
- `scratch/run_t53_9_independent_probes.py` — probe QC, không import helper test của implementer khi có thể.

Docs/state:

- `.agent/DECISIONS.md`, `.agent/TASKS.md`, `.agent/CHANGELOG.md`, `walkthrough.md`.

## 8. Lệnh xác minh cuối task

```powershell
python -m unittest tests.test_smc_engine_execution -v
python -m unittest tests.test_smc_engine_backtest_adapter -v
python -m unittest tests.test_smc_engine_t53_9_integration -v
python -m unittest tests.test_backtest tests.test_api -v
python -m unittest discover tests -v
node --test tests/test_drawings.test.js tests/test_ui_structure.test.js tests/test_drawer.test.js
python -m compileall engine smc tests
git diff --check
```

Benchmark opt-in chạy riêng theo biến môi trường đã khóa trong ADR 19; kết quả không được trộn với correctness pass/fail.

## 9. Definition of Done / Gate E

T53.9 chỉ hoàn thành khi đồng thời đạt:

- Fill N+1, spread, dynamic SL/TP, commission, RR và reversal đúng contract.
- Không có lookahead từ candle, HTF event, setup, selector, fill hoặc final bar.
- Cooldown chỉ phát sinh sau successful fill và parity qua mọi path.
- S01/S05/S09 riêng, Wave 1 selector và toàn bộ legacy strategies đều chạy qua API/backtest đúng phạm vi.
- Execution audit giải thích được mọi SELECT từ signal tới fill/reject/skip/cancel/close.
- Full test/compile/diff checks pass; benchmark evidence được ghi trung thực.
- Independent QC kết luận không còn P0/P1.

## 10. Sau T53.9

Khi Gate E pass, bắt đầu backtest nghiên cứu theo thứ tự: dữ liệu holdout → baseline từng S01/S05/S09 → Wave 1 selector → cost sensitivity → walk-forward → robustness/overfit checks. Chưa tối ưu tham số bằng dữ liệu toàn kỳ trước khi khóa protocol đánh giá.
