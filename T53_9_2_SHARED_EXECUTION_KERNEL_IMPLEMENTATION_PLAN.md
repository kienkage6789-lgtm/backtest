# T53.9.2 — Shared Execution Kernel, Dynamic SL/TP & Legacy Compatibility

> Trạng thái: **REVIEW — đã triển khai T53.9.2a–f, 1044 tests pass, chờ independent QC**
>
> Phụ thuộc: **T53.9.0 done, T53.9.1 done, ADR 25 ACCEPTED**
>
> Mục tiêu của bước này: tách máy trạng thái khớp lệnh/PnL khỏi `BacktestEngine.run()` để legacy và Wave 1 có thể dùng chung một kernel, nhưng chưa ghép pipeline S01/S05/S09.

## 1. Kết quả cần đạt

Sau T53.9.2, hệ thống có một execution kernel dùng chung để:

1. Mở LONG/SHORT bằng mức giá đã được caller xác nhận.
2. Đóng vị thế tại Open do reversal hoặc close-only legacy.
3. Kiểm tra SL/TP intrabar bằng chính `sl_price`/`tp_price` của vị thế.
4. Đóng cưỡng bức cuối dữ liệu, tính floating equity và PnL bằng cùng một công thức.
5. Bảo đảm tối đa một vị thế, không pyramiding và reversal nguyên tử.
6. Giữ nguyên chữ ký và toàn bộ output hiện có của `BacktestEngine.run()` cho năm strategy legacy.
7. Cung cấp entry point nội bộ đủ rõ để T53.9.3 đưa lệnh Wave 1 đã qua `validate_fill()` vào kernel.

T53.9.2 **không** chạy ContextBuilder, S01/S05/S09, Eligibility Gate, Selector hoặc cooldown trong backtest thật. Các phần đó thuộc T53.9.3 trở đi.

## 2. Hiện trạng code đã khảo sát

### 2.1. `engine/backtest_engine.py`

`BacktestEngine.run()` hiện đang trộn các trách nhiệm sau trong một vòng lặp:

- lấy signal của nến trước và khớp tại Open nến hiện tại;
- đóng vị thế do signal đảo chiều;
- mở LONG/SHORT và dựng fixed-distance SL/TP;
- kiểm tra SL/TP intrabar;
- tính commission, realized/floating PnL;
- dựng trade record, marker, equity curve, drawdown và metrics;
- forced close cuối backtest.

Lỗi kiến trúc cần xử lý ở bước này: trigger SL/TP đang bị khóa bởi `self.stop_loss_val > 0` và `self.take_profit_val > 0`. Vì vậy một vị thế Wave 1 có structural SL/TP hợp lệ vẫn không thể đóng nếu global fixed SL/TP bằng 0. Trigger phải dựa trên level của **vị thế**, không dựa vào global config.

### 2.2. Contract legacy cần bảo toàn

Năm strategy hiện hữu là:

- `sma_crossover`
- `rsi_reversal`
- `macd_crossover`
- `donchian_breakout`
- `smc_confluence`

Contract public phải giữ nguyên:

- `BacktestEngine.__init__()` và `BacktestEngine.run(df, strategy_id, strategy_params)` không đổi chữ ký.
- Signal Close N chỉ khớp tại Open N+1.
- BUY entry tại `Open + spread`; SELL entry tại `Open`.
- LONG thoát theo Bid; SHORT thoát theo Ask (`Bid + spread`).
- Commission round trip được trừ đúng một lần khi đóng trade.
- Nếu một bar chạm cả SL và TP thì SL thắng.
- Trade field, marker field, chuỗi `exit_reason`, cách làm tròn và nhịp ghi equity curve không đổi.
- `smc_objects` và `funnel_stats` tiếp tục được chuyển từ `df_signals.attrs` sang result.

### 2.3. Khác biệt policy bắt buộc phải tách riêng

Legacy hiện coi SELL signal là tín hiệu thoát LONG. Do đó khi `allow_short=False`, SELL vẫn phải đóng LONG nhưng không mở SHORT. ADR 25 của Wave 1 lại yêu cầu SELL bị `short_disabled` phải **không tác động vị thế đang mở**.

Không được dùng một nhánh mơ hồ cho cả hai. Kernel phải hỗ trợ hai instruction rõ nghĩa:

- `OPEN_OR_REVERSE`: mở khi flat; giữ nguyên khi cùng hướng; khi ngược hướng thì đóng vị thế cũ và mở vị thế mới trong một transition nguyên tử.
- `CLOSE_ONLY`: chỉ đóng vị thế đang mở nếu hướng cần đóng khớp; không mở vị thế mới.

Legacy facade tự chuyển SELL + `allow_short=False` thành `CLOSE_ONLY` đối với LONG. T53.9.3 sẽ không tạo instruction cho Wave 1 SELL bị disabled, nên vị thế cũ được giữ nguyên đúng ADR 25.

## 3. Ranh giới phạm vi

### 3.1. Trong phạm vi

- Tạo kernel cấp platform trong `engine/`, không gắn với strategy cụ thể.
- Tạo strict internal models cho bar, order instruction, position và transition result.
- Chuyển logic open/reversal/intrabar/force-close/PnL hiện tại vào kernel.
- Refactor `BacktestEngine.run()` thành legacy facade gọi kernel.
- Dùng per-position dynamic SL/TP; legacy vẫn dựng fixed-distance levels như trước.
- Thêm unit tests kernel và snapshot/golden regression cho legacy.
- Chuẩn bị method nhận actual entry/SL/TP đã validated để T53.9.3 dùng lại.

### 3.2. Ngoài phạm vi

- Không thêm Wave 1 strategy ID vào API/legacy registry.
- Không tạo `smc/engine/backtest_adapter.py` trong bước này.
- Không chạy selector, context, HTF timeline hoặc cooldown.
- Không phát public `execution_events` cho backtest Wave 1.
- Không đổi schema response API.
- Không thêm limit order, partial fill, slippage, trailing stop, break-even, pyramiding hoặc multi-symbol.
- Không tối ưu tham số hay chạy nghiên cứu hiệu quả chiến lược.
- Không sửa expected legacy chỉ để cho refactor pass.

## 4. Kiến trúc được chọn

### 4.1. Module mới

Tạo `engine/execution_kernel.py` để kernel nằm ở tầng platform, tránh đưa logic vị thế/PnL vào package strategy SMC.

`engine/backtest_engine.py` tiếp tục là facade public và chịu trách nhiệm:

- validate DataFrame theo contract cũ;
- gọi `StrategyRegistry.generate_signals()`;
- chuyển signal legacy thành instruction;
- điều phối vòng lặp bar;
- lấy state/result từ kernel để dựng output tương thích;
- tổng hợp drawdown và metrics theo đúng hành vi hiện tại.

Kernel không import `engine.strategies`, không biết S01/S05/S09 và không gọi selector.

### 4.2. Internal immutable models dự kiến

Tạo bằng `@dataclass(frozen=True)` và strict validation, không ép bool thành số:

#### `ExecutionBar`

- `bar_index: int`
- `timestamp: int`
- `time_value: Any` — giá trị time gốc dùng cho trade output
- `open: float`
- `high: float`
- `low: float`
- `close: float`

Invariant nội bộ:

- index không âm;
- OHLC sau normalization của facade phải là số finite và không phải bool;
- không bổ sung rejection mới cho zero hoặc hình học OHLC bất thường trong legacy path ở bước refactor này, vì code hiện tại đang `to_numeric(...).fillna(0)` và chưa khóa invariant đó.

Lưu ý tương thích: legacy facade phải giữ nguyên normalization hiện hữu trước khi tạo `ExecutionBar`. Validation OHLC fail-closed cho Wave 1 sẽ nằm tại adapter/coordinator T53.9.3–T53.9.5; không được dùng refactor kernel để âm thầm thay đổi contract public của DataFrame cũ.

#### `PositionState`

- `direction: Literal["BUY", "SELL"]`
- `entry_price: float`
- `entry_timestamp: int`
- `entry_time_value: Any`
- `sl_price: float | None`
- `tp_price: float | None`
- `multiplier: float`
- `round_trip_commission: float`
- `source: Literal["legacy", "wave1"]`
- `metadata: immutable mapping`

Invariant:

- BUY: nếu có SL thì `sl < entry`; nếu có TP thì `entry < tp`.
- SELL: nếu có TP thì `tp < entry`; nếu có SL thì `entry < sl`.
- Wave 1 bắt buộc có cả SL và TP; legacy cho phép `None` để biểu diễn global 0 points.
- Toàn bộ giá, multiplier và commission phải finite; giá/multiplier dương, commission không âm.

#### `OpenInstruction`

- `action: Literal["OPEN_OR_REVERSE", "CLOSE_ONLY"]`
- `direction: Literal["BUY", "SELL"]`
- `entry_price`, `sl_price`, `tp_price`
- `source`
- `metadata`

Quy tắc:

- `OPEN_OR_REVERSE` bắt buộc có entry; geometry phải hợp lệ trước khi state mutation.
- `CLOSE_ONLY` không được mang entry/SL/TP và chỉ đóng vị thế ngược với direction signal theo policy legacy.
- T53.9.3 chỉ được tạo `OPEN_OR_REVERSE` sau khi `validate_fill()` trả `is_valid=True`.

#### `KernelTransition`

- `status`: `NO_ACTION`, `OPENED`, `SAME_DIRECTION`, `CLOSED`, `REVERSED`, `STOPPED`, `TARGETED`, `FORCED_CLOSED`.
- `position_before`, `position_after`.
- `closed_trade: dict | None` theo schema trade legacy nội bộ.
- `markers: tuple[dict, ...]`.
- `realized_net_pnl: float`.

Model phải đủ dữ liệu để T53.9.3 ánh xạ sang `ExecutionEvent`, nhưng T53.9.2 chưa phát event public.

### 4.3. `ExecutionKernel`

Kernel quản lý state theo từng run:

- `initial_capital`
- `balance`
- `position`
- `trades`
- `markers`
- counter trade ID
- immutable execution cost config của run

Public nội bộ tối thiểu:

```python
process_open(bar: ExecutionBar, instruction: OpenInstruction | None) -> KernelTransition
process_intrabar(bar: ExecutionBar) -> KernelTransition
mark_to_market(close_bid: float) -> float
force_close(bar: ExecutionBar) -> KernelTransition
reset() -> None
```

Không cung cấp mutation trực tiếp vào `position`, `trades` hoặc `markers`; getter phải trả immutable view hoặc defensive copy.

## 5. State machine và thứ tự xử lý

### 5.1. Mỗi bar legacy

Giữ đúng thứ tự hiện hữu:

1. Dựng `ExecutionBar` từ bar `i`.
2. Đọc signal của bar `i-1`.
3. Chuyển signal thành `OpenInstruction` hoặc `None`.
4. `kernel.process_open(bar, instruction)`.
5. `kernel.process_intrabar(bar)` — vị thế vừa mở có thể chạm SL/TP ngay trong bar entry.
6. `kernel.mark_to_market(close)`.
7. Backtest facade cập nhật peak, drawdown và equity curve đúng nhịp cũ.
8. Sau vòng lặp, `kernel.force_close(last_bar)` nếu còn vị thế.
9. Dựng metrics/result bằng schema và rounding cũ.

### 5.2. Open/reversal transition

`process_open()` phải validate toàn bộ instruction trước khi thay đổi state.

- `instruction is None`: `NO_ACTION`.
- Flat + `CLOSE_ONLY`: `NO_ACTION`.
- Flat + `OPEN_OR_REVERSE`: mở position, `OPENED`.
- Có position cùng hướng + `OPEN_OR_REVERSE`: không pyramiding, `SAME_DIRECTION`.
- Có position ngược hướng + `CLOSE_ONLY`: đóng cũ tại Open, `CLOSED`.
- Có position ngược hướng + `OPEN_OR_REVERSE`: trên working copy, đóng cũ rồi mở mới; commit cả hai cùng lúc, `REVERSED`.
- Instruction sai geometry/type/non-finite: raise trước commit; balance, position, trades, markers giữ nguyên tuyệt đối.

Không được đóng vị thế cũ trước rồi mới phát hiện order mới không hợp lệ.

### 5.3. Dynamic SL/TP intrabar

Trigger dùng level trên `PositionState`, tuyệt đối không dùng `BacktestEngine.stop_loss_val` hoặc `take_profit_val` làm guard.

LONG:

1. Nếu `sl_price is not None` và `low <= sl_price` → đóng tại SL.
2. Ngược lại, nếu `tp_price is not None` và `high >= tp_price` → đóng tại TP.

SHORT:

1. `ask_high = high + spread`; nếu có SL và `ask_high >= sl_price` → đóng tại SL.
2. Ngược lại, `ask_low = low + spread`; nếu có TP và `ask_low <= tp_price` → đóng tại TP.

SL luôn được xét trước TP. Nếu level là `None`, phía đó bị tắt. Giá `0.0` không dùng làm sentinel trong kernel.

### 5.4. Reversal, forced close và mark-to-market

- Đóng LONG tại Open/Close Bid.
- Đóng SHORT tại Open/Close Ask = Bid + spread.
- Intrabar đóng đúng executable level đã lưu trên position.
- `gross_pnl`:
  - BUY: `(exit - entry) * multiplier`
  - SELL: `(entry - exit) * multiplier`
- `net_pnl = gross_pnl - round_trip_commission`.
- Commission chỉ trừ một lần khi trade đóng; không trừ lần hai khi dựng metrics.
- Mark-to-market phải dùng cùng exit-side convention và phản ánh round-trip commission như code cũ.
- `force_close()` gọi lặp lại sau khi flat phải là no-op, không tạo trade/marker trùng.

## 6. Kế hoạch thực hiện theo lát cắt nhỏ

### T53.9.2a — Khóa baseline legacy trước refactor

1. Chạy và lưu kết quả hiện tại của `tests/test_backtest.py`, `tests/test_api.py` và `tests/test_smc_integration.py`.
2. Tạo synthetic fixtures deterministic bao phủ long, short, same-direction, reversal hai chiều, SL/TP, forced close và `allow_short=False`.
3. Tạo baseline cho cả năm strategy legacy bằng candle fixture cố định và params cố định.
4. Snapshot phải so toàn bộ `metrics`, `trades`, `equity_curve`, `markers`; với `smc_confluence` so thêm `smc_objects` và `funnel_stats` sau canonical JSON conversion.
5. Baseline được sinh từ code **trước refactor**, review bằng tay một lần rồi mới sửa runtime.

Acceptance:

- Fixture deterministic, không phụ thuộc thời gian hiện tại.
- Không phụ thuộc thứ tự hash/dict không ổn định.
- Có bằng chứng baseline trước refactor để chống “sửa expected theo code mới”.

### T53.9.2b — Tạo strict kernel models và state

1. Tạo `engine/execution_kernel.py`.
2. Implement `ExecutionBar`, `PositionState`, `OpenInstruction`, `KernelTransition`.
3. Implement validation, defensive copy/deep immutability và atomic state snapshot.
4. Implement `ExecutionKernel.reset()` và isolation giữa hai run.

Acceptance:

- Bool-as-number, NaN/Inf và order/position geometry sai bị từ chối; `ExecutionBar` legacy giữ contract normalization cũ như mục 4.2.
- Input invalid không làm thay đổi state.
- State không bị caller mutate qua reference ngoài.

### T53.9.2c — Open, close, reversal và accounting

1. Implement `process_open()` với `OPEN_OR_REVERSE`/`CLOSE_ONLY`.
2. Trích xuất đúng một helper đóng vị thế dùng chung cho reversal, SL/TP và forced close.
3. Bảo toàn trade/marker schema và rounding legacy ở boundary xuất dữ liệu.
4. Dùng golden reversal vector đã khóa trong `tests/test_smc_engine_execution.py`:
   - BUY cũ entry 2000, reverse tại Bid Open 2010;
   - gross +100, commission 1, net +99;
   - SELL mới mở tại 2010 với dynamic levels đã validate.
5. Probe reversal order invalid: vị thế cũ, balance, trades và markers không đổi.

Acceptance:

- Reversal hợp lệ đóng rồi mở trong cùng Open.
- Reversal invalid không đóng vị thế cũ.
- Không double commission hoặc double trade.
- Same-direction không pyramiding.

### T53.9.2d — Dynamic intrabar exits và equity

1. Implement trigger từ `PositionState.sl_price/tp_price`.
2. Implement LONG Bid và SHORT Ask symmetry.
3. Implement SL-first collision.
4. Implement same-entry-bar SL/TP.
5. Implement mark-to-market và forced close idempotent.

Acceptance:

- Dynamic SL/TP vẫn hoạt động khi global fixed SL/TP bằng 0.
- Global SL/TP khác 0 không được ghi đè dynamic levels của position.
- Optional legacy SL hoặc TP riêng lẻ vẫn hoạt động.
- Equity/balance cuối cùng khớp trade accounting.

### T53.9.2e — Refactor `BacktestEngine` thành legacy facade

1. Giữ nguyên public constructor/run signature.
2. Giữ `StrategyRegistry.generate_signals()` và signal shift N→N+1.
3. Legacy signal adapter:
   - BUY: entry Open+spread; fixed SL/TP quanh actual entry.
   - SELL + short enabled: entry Open; fixed SL/TP quanh entry.
   - SELL + short disabled + đang LONG: `CLOSE_ONLY`.
   - SELL + short disabled + flat: no-op.
4. Chuyển state transition sang kernel.
5. Facade tiếp tục dựng equity sampling, MDD, metrics và optional SMC attrs như cũ.

Acceptance:

- Snapshot của cả năm legacy strategy giống baseline byte-for-byte sau canonical JSON.
- 10 test legacy hiện tại pass mà không nới assertion.
- API legacy không thêm/bớt field.

### T53.9.2f — Chuẩn bị seam cho T53.9.3 và tài liệu

1. Xác nhận kernel nhận được `actual_entry`, `actual_sl`, `actual_tp` từ `FillValidationResult` mà không tính spread lần hai.
2. Metadata của Wave 1 được giữ bất biến xuyên position → closed transition.
3. Không import strategy implementation vào kernel.
4. Cập nhật exports nội bộ nếu thật sự cần; không expose API web mới.
5. Cập nhật `.agent/TASKS.md`, `.agent/CHANGELOG.md`, `walkthrough.md` sau khi implementation/test thực sự hoàn tất.

Acceptance:

- Có integration seam được test bằng một validated BUY và SELL instruction.
- Không chạy coordinator/cooldown sớm trong T53.9.2.

## 7. Ma trận kiểm thử bắt buộc

### 7.1. Kernel unit tests — `tests/test_execution_kernel.py`

1. Flat → open BUY.
2. Flat → open SELL.
3. Same-direction signal không mở thêm.
4. BUY→SELL reversal hợp lệ.
5. SELL→BUY reversal hợp lệ.
6. Invalid opposite instruction giữ nguyên position/balance/audit state.
7. `CLOSE_ONLY` đóng đúng vị thế và không mở mới.
8. `CLOSE_ONLY` khi flat là no-op.
9. LONG dynamic SL.
10. LONG dynamic TP.
11. SHORT SL theo Ask.
12. SHORT TP theo Ask.
13. Cùng chạm SL/TP → SL trước cho cả BUY và SELL.
14. Fill và SL/TP trong cùng entry bar.
15. Dynamic levels hoạt động khi global fixed level bằng 0.
16. Legacy `None` SL/TP không trigger giả.
17. Reversal/SL/TP/forced-close dùng cùng PnL helper.
18. Commission chính xác một round trip.
19. Floating equity LONG/SHORT đối xứng spread.
20. Forced close cập nhật balance/trade/marker đúng một lần.
21. Reset dọn sạch toàn bộ state.
22. NaN/Inf/bool/geometry sai fail-fast và atomic.
23. Metadata immutable, không leak mutation.
24. Deterministic retry/no duplicate transition khi đã flat.

### 7.2. Legacy regression — `tests/test_backtest.py`

Giữ nguyên toàn bộ test cũ và bổ sung:

- reversal hai chiều trong cùng Open;
- repeated same-direction không pyramiding;
- SELL với `allow_short=False` đóng LONG theo semantics legacy nhưng không mở SHORT;
- dynamic-position probe chứng minh trigger không dùng global guard;
- result exact-key schema;
- run hai lần trên cùng engine không leak position/trade/balance.

### 7.3. Golden compatibility — `tests/test_backtest_legacy_compat.py`

So exact canonical payload trước/sau cho năm strategy. Không được cập nhật baseline sau refactor trừ khi independent QC chứng minh baseline cũ sai và người dùng phê duyệt thay đổi contract.

### 7.4. Không-lookahead và boundary

- Signal N không tạo position/trade/marker tại N.
- Open N+1 xử lý reversal trước intrabar N+1.
- Vị thế mới có thể đóng intrabar N+1.
- Signal ở bar cuối không mở lệnh giả.
- Forced close dùng Close bar cuối, không dùng dữ liệu tương lai.

## 8. File dự kiến thay đổi

Production:

- `engine/execution_kernel.py` — mới, kernel và internal models.
- `engine/backtest_engine.py` — refactor thành facade gọi kernel.

Tests/fixtures:

- `tests/test_execution_kernel.py` — mới.
- `tests/test_backtest.py` — bổ sung regression, không làm yếu test cũ.
- `tests/test_backtest_legacy_compat.py` — mới.
- `tests/fixtures/t53_9_2_legacy_baseline.json` — mới nếu snapshot full payload đủ ổn định.

Documentation sau implementation:

- `.agent/TASKS.md`
- `.agent/CHANGELOG.md`
- `walkthrough.md`

Không sửa `server.py`, `engine/strategies.py`, selector, context builder hoặc strategy S01/S05/S09 trong T53.9.2, trừ khi QC chỉ ra dependency thật và scope được cập nhật trước.

## 9. Verification commands

Chạy theo thứ tự:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_execution_kernel -v
.\.venv\Scripts\python.exe -m unittest tests.test_backtest -v
.\.venv\Scripts\python.exe -m unittest tests.test_backtest_legacy_compat -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_engine_execution -v
.\.venv\Scripts\python.exe -m unittest tests.test_smc_integration tests.test_api -v
.\.venv\Scripts\python.exe -m unittest discover tests -q
node --test tests/test_drawings.test.js tests/test_ui_structure.test.js tests/test_drawer.test.js
.\.venv\Scripts\python.exe -m compileall -q engine smc tests
git diff --check
```

Nếu benchmark dao động thì báo số đo quan sát; T53.9.2 không đặt hard performance gate mới. Correctness và legacy parity là gate bắt buộc.

## 10. Independent QC checklist

Reviewer phải tự tạo probe, không chỉ đọc walkthrough:

- [ ] Kernel không import strategy implementation.
- [ ] Trigger dynamic SL/TP không còn phụ thuộc global fixed-distance guard.
- [ ] BUY Bid / SELL Ask và spread không bị tính hai lần.
- [ ] Commission không bị thu hai lần khi reversal hoặc forced close.
- [ ] Invalid reversal không làm mất vị thế cũ.
- [ ] Reversal hợp lệ nguyên tử và không có thời điểm hai position cùng tồn tại.
- [ ] Same-direction không pyramiding.
- [ ] SL-first đúng cả LONG và SHORT.
- [ ] Same-entry-bar exit đúng thứ tự Open → intrabar.
- [ ] Forced close và reset idempotent.
- [ ] Năm strategy legacy có exact output parity.
- [ ] Không có API/schema change ngoài phạm vi.
- [ ] Full regression, compileall, whitespace/conflict scan và diff-check pass.

## 11. Điều kiện hoàn thành T53.9.2

Chỉ đánh dấu `done` khi đồng thời:

1. Kernel dùng chung được triển khai và test độc lập.
2. Per-position dynamic SL/TP hoạt động đúng BUY/SELL.
3. Reversal invalid giữ nguyên position; reversal valid đóng/mở nguyên tử.
4. PnL/spread/commission/equity/forced-close dùng một contract thống nhất.
5. Toàn bộ năm strategy legacy exact parity.
6. Test targeted, full Python, Node regression, compileall và diff-check pass.
7. Independent QC không còn P0/P1; mọi P2 được sửa hoặc ghi debt có owner/task đích.
8. Không triển khai lấn sang coordinator/cooldown/API Wave 1 của T53.9.3–T53.9.4.

Sau khi đạt các điều kiện trên mới chuyển sang **T53.9.3 — Bar-by-bar SMC coordinator, cooldown-after-fill và HTF as-of timeline**.
