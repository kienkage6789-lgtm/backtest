# KẾ HOẠCH TRIỂN KHAI CHI TIẾT — SMC TRADING SYSTEM

## 0. Mục tiêu và nguyên tắc

Mục tiêu là bổ sung hệ thống SMC vào nền tảng backtest hiện có, không xây lại DataFeed, BacktestEngine, Replay hoặc UI.

Nguồn tham chiếu:

1. `plan.md`: kiến trúc nền tảng backtest hiện tại.
2. `SMC_TRADING_SYSTEM_PLAN.md`: định hướng modular SMC.
3. Source LuxAlgo được cung cấp: logic leg, pivot, swing/internal structure, BOS/CHoCH và cách phân loại HH/HL/LH/LL.

Nguyên tắc bắt buộc:

- Chỉ xử lý nến đã đóng.
- Không sử dụng dữ liệu tương lai để tạo tín hiệu tại thời điểm backtest.
- Mỗi module SMC phải độc lập và có output chuẩn hóa.
- Swing chính và internal swing phải là hai cấu hình riêng.
- Core logic phải dùng được cho backtest, replay và live/realtime sau này.
- Mọi module phải có dữ liệu debug để vẽ và kiểm tra bằng mắt.
- Không sao chép nguyên văn source LuxAlgo; chỉ tái hiện ý tưởng và hành vi cần thiết theo thiết kế riêng của dự án.

## 1. Hiện trạng nền tảng cần giữ nguyên

### 1.1. Backend hiện có

- `engine/data_feed.py`: đọc SQLite XAUUSD, query theo thời gian, resample đa khung.
- `engine/strategies.py`: registry chiến lược và sinh tín hiệu.
- `engine/backtest_engine.py`: mô phỏng khớp lệnh từng nến, spread, commission, SL/TP, equity và metrics.
- `server.py`: API candles, replay, strategies và backtest.

### 1.2. Frontend hiện có

- Lightweight Charts.
- Bar Replay và Dual Chart.
- Markers trên chart.
- Drawer cấu hình/kết quả.
- Drawing tools và persistence.

### 1.3. Không được phá vỡ

- Các strategy cũ: SMA, RSI, MACD, Donchian.
- API hiện tại của `/api/candles`, `/api/replay/init`, `/api/backtest`.
- Cơ chế vào lệnh của `BacktestEngine`: signal của nến hiện tại được thực thi ở Open nến kế tiếp.
- Chức năng replay, drawing và báo cáo hiện tại.

## 2. Kiến trúc đích

```text
DataFeed hiện tại
       |
       v
OHLCV Adapter / Data Contract
       |
       +--> Swing Detector
       |       |
       |       +--> Structure Tracker
       |       +--> BOS / CHoCH
       |       +--> HH / HL / LH / LL
       |
       +--> Order Block Detector
       +--> FVG Detector
       +--> Liquidity Pool / Sweep Detector
       +--> Kill Zone / HTF Bias
                       |
                       v
              Confluence Engine
                       |
                       +--> Signal columns cho BacktestEngine
                       +--> TradeSetup metadata
                       +--> Chart overlays / markers
```

SMC sẽ được thêm như một strategy mới, dự kiến ID:

```text
smc_confluence
```

Core SMC không được phụ thuộc vào FastAPI hoặc UI.

## 3. Data contract và adapter

### 3.1. Input chuẩn hóa

Tạo module `smc/data_contract.py` với hàm:

```python
normalize_ohlcv(df) -> pd.DataFrame
```

Input có thể là output hiện tại của `DataFeed`:

```text
time, open, high, low, close, tick_volume
```

Output chuẩn:

```text
index: DatetimeIndex, timezone UTC, name="time"
columns: open, high, low, close, volume
```

Quy tắc:

- Đổi `tick_volume` thành `volume` nếu chưa có `volume`.
- Ép kiểu số cho OHLCV.
- Sắp xếp tăng dần theo thời gian.
- Loại bỏ timestamp trùng.
- Kiểm tra `high >= max(open, close)` và `low <= min(open, close)`.
- Không tự tạo nến thiếu.
- Không forward-fill OHLC.
- Giữ một cột nội bộ `bar_index` để tham chiếu nhanh.

### 3.2. Output metadata

SMC nên trả về object kết quả thay vì chỉ trả một cột signal:

```python
@dataclass
class SMCAnalysis:
    swings: list[SwingPoint]
    structure_events: list[StructureEvent]
    order_blocks: list[OrderBlock]
    fvgs: list[FVG]
    liquidity_pools: list[LiquidityPool]
    signals: list[Signal]
    bar_signals: pd.DataFrame
```

`bar_signals` dùng cho backtest; các list object dùng cho debug, chart và audit.

## 4. Data models

Tạo `smc/models.py`.

### 4.1. SwingPoint

```python
@dataclass
class SwingPoint:
    index: int
    time: pd.Timestamp
    price: float
    kind: Literal["high", "low"]
    strength: int
    confirmed_at: int
    classification: Literal["HH", "HL", "LH", "LL", "UNCLASSIFIED"]
    broken: bool = False
    broken_at: int | None = None
```

`confirmed_at` bắt buộc để tránh nhầm thời điểm swing hình thành với thời điểm swing được biết.

### 4.2. StructureEvent

```python
@dataclass
class StructureEvent:
    index: int
    time: pd.Timestamp
    event_type: Literal["BOS", "CHoCH"]
    direction: Literal["bullish", "bearish"]
    broken_swing_index: int
    broken_swing_price: float
    close_price: float
    displacement: bool = False
```

### 4.3. Các model còn lại

Giữ các model trong `SMC_TRADING_SYSTEM_PLAN.md`, nhưng bổ sung `created_at`, `confirmed_at` hoặc `invalidated_at` khi cần để audit theo thời gian.

## 5. Phase 0 — Baseline và an toàn thay đổi

### Công việc

- Chạy test hiện có trong môi trường có database.
- Lưu lại số lượng test pass làm baseline.
- Kiểm tra `git status`, không đụng các screenshot hoặc thay đổi người dùng.
- Tạo package:

```text
smc/
    __init__.py
    models.py
    data_contract.py
    indicators.py
    structure/
    zones/
    liquidity/
    context/
    engine/
    visualization/
```

- Tạo fixture OHLCV synthetic nhỏ, không phụ thuộc SQLite.
- Thêm test helper cho timestamp UTC và bar index.

### Hoàn thành khi

- Import `smc` không cần FastAPI hoặc database.
- Test synthetic chạy được khi không có `data/XAUUSD.db`.
- Strategy cũ vẫn giữ nguyên hành vi.

## 6. Phase 1 — Swing Detector, ưu tiên cao nhất

Đây là phase nền móng và phải hoàn thành trước BOS/CHoCH, OB hoặc liquidity.

### 6.1. Hai chế độ swing

Phải hỗ trợ:

```python
detect_swings(df, strength=50, mode="swing")
detect_swings(df, strength=5, mode="internal")
```

Tên `strength` trong Python biểu thị độ sâu xác nhận; cần ghi rõ rằng source LuxAlgo dùng `size`/leg chứ không hoàn toàn giống fractal đối xứng.

### 6.2. Logic v1 được chọn

Để backtest ổn định, v1 dùng pivot xác nhận không repaint:

- Swing high tại `i` khi high của `i` lớn hơn vùng kiểm tra bên trái và bên phải.
- Swing low đối xứng.
- Swing chỉ được phát hành tại `confirmed_at = i + right_strength`.
- Không tạo signal trước `confirmed_at`.
- Nếu có bằng giá, dùng quy tắc tie-break cố định và test riêng.

Sau đó thêm compatibility mode mô phỏng leg logic của source LuxAlgo nếu cần đối chiếu chart.

### 6.3. Phân loại cấu trúc

Khi swing high mới xuất hiện:

- Cao hơn swing high trước → `HH`.
- Thấp hơn swing high trước → `LH`.

Khi swing low mới xuất hiện:

- Cao hơn swing low trước → `HL`.
- Thấp hơn swing low trước → `LL`.

Không dùng trend hiện tại để thay thế phân loại giá; trend và classification là hai khái niệm riêng.

### 6.4. Test bắt buộc

- Fractal high/low cơ bản.
- Độ trễ xác nhận đúng.
- Không dùng dữ liệu tương lai trước thời điểm xác nhận.
- HH/HL/LH/LL đúng trên chuỗi synthetic.
- Swing high và swing low không bị ghi đè ngoài ý muốn.
- Internal strength 5 và swing strength 50 hoạt động độc lập.
- Dữ liệu flat, missing, duplicate timestamp và gap thời gian.
- Kết quả deterministic khi chạy lại.

### 6.5. Visualization

Tạo output để UI vẽ:

```json
{
  "time": "...",
  "price": 2500.0,
  "kind": "high",
  "label": "HH",
  "confirmed_at": "..."
}
```

Chỉ hiển thị swing tại vị trí hình thành, nhưng tooltip/debug phải cho biết thời điểm xác nhận.

## 7. Phase 2 — BOS và CHoCH

### 7.1. State machine

```python
trend = None | "bullish" | "bearish"
last_unbroken_high = SwingPoint | None
last_unbroken_low = SwingPoint | None
```

Mỗi nến đóng:

1. Nạp các swing đã được xác nhận tại nến đó.
2. Kiểm tra close có phá swing high/low chưa.
3. Chỉ phá mỗi swing một lần.
4. Nếu phá lên:
   - trend bearish → CHoCH bullish.
   - trend bullish hoặc None → BOS bullish.
5. Nếu phá xuống: logic đối xứng.
6. Đánh dấu swing `broken=True`, `broken_at=current_index`.

### 7.2. Break rule

V1 dùng close vượt hẳn mức swing:

```text
bullish break: close > swing_high.price
bearish break: close < swing_low.price
```

Không dùng wick để xác nhận BOS/CHoCH. Wick chỉ dành cho liquidity sweep.

### 7.3. Displacement

Tính ATR(14) bằng hàm nội bộ trong `smc/indicators.py`.

```text
body = abs(close - open)
displacement = body > ATR(14) * displacement_multiplier
```

### 7.4. Test

- BOS bullish/bearish.
- CHoCH từ trend ngược hướng.
- Không phát lại event trên các nến sau.
- Wick vượt nhưng close không vượt không tạo BOS.
- Break cùng nến với nhiều swing: quy tắc ưu tiên rõ ràng.
- Delay của swing được tính đúng.
- Displacement không nhìn vào ATR tương lai.

## 8. Phase 3 — Order Block

### Công việc

- Khi có BOS/CHoCH bullish, tìm nến bearish gần nhất trước leg breakout.
- Khi có BOS/CHoCH bearish, tìm nến bullish gần nhất.
- Lưu vùng OHLC của nến nguồn.
- Gắn `origin_structure_event` vào metadata.
- Theo dõi mitigation từng nến sau khi OB hình thành.
- Đóng OB khi close phá toàn vùng theo hướng bất lợi.

### Quy tắc phải cấu hình được

- Mitigation theo wick hoặc close.
- Vùng OB toàn nến hoặc vùng tối ưu.
- Số OB tối đa được giữ.
- Cho phép/không cho phép retest nhiều lần.

### Test

- Tìm đúng nến ngược chiều cuối cùng.
- Không chọn nến trước khi structure event được xác nhận.
- Chạm vùng cập nhật `mitigation_pct`.
- Close phá vùng làm `valid=False`.
- OB bullish/bearish đối xứng.

## 9. Phase 4 — FVG

### Công việc

- Detect pattern 3 nến đã đóng.
- Bullish FVG: `low[i+1] > high[i-1]`.
- Bearish FVG: `high[i+1] < low[i-1]`.
- Theo dõi fill theo wick.
- Cấu hình ngưỡng sử dụng: 50%, 100% hoặc custom.

### Test

- FVG tăng/giảm cơ bản.
- Không phát hiện trên nến chưa đóng.
- Fill một phần và fill toàn bộ.
- Không nhầm gap cuối tuần với FVG nếu dữ liệu có khoảng trống thời gian.

## 10. Phase 5 — Liquidity và Sweep

### Công việc

- Gom swing highs gần nhau thành equal highs.
- Gom swing lows gần nhau thành equal lows.
- Tolerance theo ATR hoặc phần trăm giá, không cố định theo giá tuyệt đối.
- Detect sweep:
  - wick vượt pool.
  - close quay lại bên trong.
- Phân loại `clean` hoặc `wick_only` theo số nến retest.

### Test

- Equal highs/lows với tolerance.
- Không gom các swing quá xa.
- Wick sweep không bị nhầm thành BOS.
- Close vượt hẳn pool không bị coi là sweep đảo chiều.
- Pool bị sweep chỉ phát event một lần.

## 11. Phase 6 — Context

### Kill Zone

- Module `context/killzone.py`.
- London: 07:00–10:00 UTC.
- New York: 12:00–15:00 UTC.
- Xử lý timezone rõ ràng.
- Có cấu hình session và ngày giao dịch.

### HTF Bias

- Chạy structure detector trên DataFrame HTF riêng.
- Map bias HTF về LTF bằng timestamp, không dùng thông tin HTF chưa đóng.
- Bias chỉ được cập nhật sau khi nến HTF đóng.
- Test biên timestamp giữa các timeframe.

## 12. Phase 7 — Confluence Engine

Tạo `smc/engine/confluence.py`.

### Mode A: AND cứng

Điều kiện v1 mặc định:

```text
structure_confirmed
price_in_valid_ob
liquidity_swept_recently
```

### Mode B: Weighted scoring

Weights ban đầu:

```text
htf_bias_aligned          1.5
choch_or_bos_confirmed    2.0
price_in_valid_ob         2.0
price_in_fvg              1.0
liquidity_swept_recently  1.5
in_killzone               0.5
displacement_confirmed    1.0
```

### TradeSetup

- Entry theo policy của BacktestEngine.
- SL ngoài OB hoặc swing liên quan.
- TP theo liquidity pool đối diện hoặc RR cố định.
- Hỗ trợ nhiều TP ở metadata; v1 có thể chỉ thực thi TP đầu tiên.
- Tính RR trước khi tạo setup.
- Không tạo setup nếu SL không hợp lệ hoặc RR âm/không đủ.

### Test

- Tất cả điều kiện đúng → có setup.
- Thiếu điều kiện bắt buộc → không có setup.
- Weighted score đúng.
- Long/short đối xứng.
- Không tạo nhiều setup trên cùng một event.
- Cooldown sau entry được áp dụng đúng.

## 13. Phase 8 — Tích hợp StrategyRegistry và BacktestEngine

### StrategyRegistry

Thêm strategy metadata:

```text
id: smc_confluence
```

Params tối thiểu:

```text
structure_mode: swing/internal
swing_strength: 50
internal_strength: 5
decision_mode: and/scoring
score_threshold: 6.0
atr_period: 14
displacement_multiplier: 1.5
ob_lookback: 20
fvg_fill_threshold: 0.5
liquidity_tolerance_atr: 0.1
allow_counter_trend: false
```

### Adapter signal

SMC engine trả về:

```text
signal = 1   # long setup
signal = -1  # short setup
signal = 0   # no entry
```

Tách riêng:

- `signal`: dùng cho BacktestEngine.
- `setup_metadata`: dùng cho debug/UI.

Không sửa behavior của các strategy cũ.

### Backtest integration

- Giữ nguyên nguyên tắc vào lệnh ở nến sau.
- Kiểm tra SL/TP trên OHLC của nến sau entry.
- Không để SMC detector đọc các nến sau thời điểm signal.
- Ghi thêm `signals_fired`, `score`, `structure_event`, `ob_index`, `sweep_index` vào trade metadata.

## 14. Phase 9 — API và UI

### API

Mở rộng `/api/strategies` tự động hiển thị SMC params.

Mở rộng response `/api/backtest` tùy chọn:

```json
{
  "smc": {
    "swings": [],
    "structure_events": [],
    "order_blocks": [],
    "fvgs": [],
    "liquidity_pools": [],
    "setups": []
  }
}
```

Để tránh response quá lớn, cho phép tham số `include_smc_debug=false` mặc định.

### UI overlay

Ưu tiên hiển thị theo thứ tự:

1. Swing labels HH/HL/LH/LL.
2. BOS/CHoCH line + label.
3. Order Block rectangles.
4. FVG rectangles.
5. Liquidity pools và sweep markers.
6. Entry/SL/TP markers.

Mỗi lớp phải bật/tắt độc lập. Dữ liệu overlay không được làm thay đổi dữ liệu nến.

## 15. Phase 10 — Replay và HTF/LTF

- Replay phải chạy detector theo từng nến mới xuất hiện.
- Không chạy detector trên toàn bộ tương lai rồi chỉ ẩn kết quả.
- Khi replay step, chỉ được tạo swing sau đủ nến xác nhận.
- Dual Chart phải hiển thị cùng một event theo timestamp.
- HTF bias chỉ thay đổi khi HTF bar đóng.
- Thêm test replay cho:
  - swing chưa xác nhận.
  - swing vừa xác nhận.
  - BOS xuất hiện sau xác nhận.
  - HTF bias chưa được phép nhìn tương lai.

## 16. Phase 11 — Backtest validation và tối ưu

### Metrics cần bổ sung

- Total trades.
- Win rate.
- Average R.
- Expectancy.
- Profit factor.
- Max drawdown.
- Average holding time.
- Long/short breakdown.
- Performance theo session.
- Performance theo signal combination.
- Edge của từng signal fired.

### Quy trình chống overfit

1. Chia train/test theo thời gian.
2. Tối ưu threshold/weights trên train.
3. Khóa tham số.
4. Chạy test out-of-sample.
5. Walk-forward validation.
6. So sánh với baseline SMA/Donchian.
7. Bao gồm spread, commission và slippage.

Không tối ưu trực tiếp trên toàn bộ dữ liệu lịch sử rồi báo kết quả như kết quả forward.

## 17. Test matrix tổng thể

### Unit test

- `test_data_contract.py`
- `test_swings.py`
- `test_structure.py`
- `test_order_block.py`
- `test_fvg.py`
- `test_liquidity.py`
- `test_context.py`
- `test_confluence.py`

### Integration test

- SMC strategy với BacktestEngine.
- SMC strategy qua `/api/backtest`.
- SMC metadata trong response.
- Strategy cũ không bị ảnh hưởng.

### Replay test

- Không lookahead.
- Delay xác nhận swing.
- BOS/CHoCH xuất hiện đúng bar.
- HTF/LTF đồng bộ.

### UI/browser QA

- Toggle từng lớp overlay.
- Zoom/pan/drawer không làm mất overlay.
- Replay không vẽ tín hiệu tương lai.
- Marker entry/exit trùng trade record.

## 18. Thứ tự triển khai thực tế

### Milestone 1 — Swing foundation

- Data contract.
- Models.
- Swing detector swing/internal.
- Synthetic fixtures.
- Unit tests.
- Debug JSON.

### Milestone 2 — Structure

- BOS/CHoCH state machine.
- HH/HL/LH/LL.
- Displacement.
- Structure visualization payload.

### Milestone 3 — Zones

- Order Block.
- Mitigation.
- FVG.

### Milestone 4 — Liquidity/context

- Equal highs/lows.
- Sweep.
- Kill Zone.
- HTF bias.

### Milestone 5 — Decision

- Confluence AND.
- Weighted scoring.
- TradeSetup.

### Milestone 6 — Existing platform integration

- StrategyRegistry.
- BacktestEngine.
- API.
- Markers/report.

### Milestone 7 — Replay and validation

- Event-by-event replay.
- HTF/LTF validation.
- Train/test.
- Performance breakdown.

## 19. Definition of Done

SMC v1 chỉ được xem là hoàn thành khi:

- Swing detector không repaint trong backtest/replay.
- Mọi swing có `confirmed_at` và không dùng dữ liệu sau thời điểm đó.
- BOS/CHoCH phân biệt đúng theo trend state.
- OB, FVG và sweep có unit test độc lập.
- Có overlay để kiểm tra bằng mắt.
- `smc_confluence` chạy được qua API và UI.
- Backtest có spread/commission và không lookahead.
- Strategy cũ vẫn pass regression test.
- Có train/test hoặc walk-forward report.
- Trade record chứa được lý do setup và các signal đã kích hoạt.

## 20. Việc đầu tiên cần thực hiện

Không bắt đầu bằng Order Block hoặc Confluence.

Việc đầu tiên là tạo:

```text
smc/models.py
smc/data_contract.py
smc/structure/swings.py
tests/test_smc_swings.py
```

Sau đó xây một dataset synthetic nhỏ có chuỗi:

```text
LL → HL → HH → HL → phá HH → BOS bullish
```

và:

```text
HH → LH → LL → LH → phá HL → CHoCH bearish
```

Chỉ khi hai chuỗi này cho kết quả đúng và không lookahead mới chuyển sang các module SMC tiếp theo.
