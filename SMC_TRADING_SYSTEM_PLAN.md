# PLAN HỆ THỐNG GIAO DỊCH SMC (Smart Money Concepts)
### Kiến trúc: Modular Confluence System — Python

---

## 0. TRIẾT LÝ THIẾT KẾ

- **Mỗi khái niệm SMC = 1 module độc lập**, nhận vào dữ liệu OHLCV, trả về **tín hiệu chuẩn hoá** (không tự quyết định vào lệnh).
- **Confluence Engine** là nơi duy nhất tổng hợp tín hiệu và quyết định entry.
- **Tách biệt hoàn toàn 3 lớp**: Data → Signal Generation → Decision/Execution. Không lớp nào gọi ngược lớp trước.
- Mọi module phải **verify được bằng mắt** (vẽ lên chart) trước khi đưa vào confluence — tránh bug âm thầm.
- Ưu tiên **vector hoá bằng pandas/numpy** cho backtest nhanh, nhưng giữ logic core dưới dạng hàm thuần (pure function) để tái dùng cho live/replay.

---

## 1. CẤU TRÚC DỮ LIỆU (DATA MODELS)

### 1.1. OHLCV chuẩn hoá
```python
# Dùng pandas DataFrame làm nguồn chân lý (source of truth)
# Bắt buộc các cột sau, index là datetime (UTC)
columns = ["open", "high", "low", "close", "volume"]
# Index: pd.DatetimeIndex, tên "time", timezone-aware (UTC)
```
Quy ước:
- Timeframe entry (LTF) và timeframe bối cảnh (HTF) là 2 DataFrame riêng, đồng bộ qua `resample` hoặc load riêng.
- Mỗi nến có `index` nguyên (int position) dùng nội bộ để tham chiếu nhanh (`df.reset_index()` khi cần).

### 1.2. Swing Point
```python
@dataclass
class SwingPoint:
    index: int              # vị trí nến trong DataFrame
    time: pd.Timestamp
    price: float
    kind: Literal["high", "low"]
    strength: int            # số nến xác nhận 2 bên (vd: fractal 5 nến -> strength=2)
    broken: bool = False      # đã bị phá vỡ (BOS/CHoCH) chưa
    broken_at: int | None = None
```

### 1.3. Structure Event (BOS / CHoCH)
```python
@dataclass
class StructureEvent:
    index: int
    time: pd.Timestamp
    event_type: Literal["BOS", "CHoCH"]
    direction: Literal["bullish", "bearish"]
    broken_swing: SwingPoint      # swing point bị phá
    close_price: float             # giá đóng cửa xác nhận phá vỡ
    displacement: bool = False     # có kèm nến thân dài (displacement) không
```

### 1.4. Order Block
```python
@dataclass
class OrderBlock:
    index: int                     # vị trí nến gốc tạo OB
    time: pd.Timestamp
    direction: Literal["bullish", "bearish"]
    high: float
    low: float
    open: float
    close: float
    origin_type: Literal["swing_ob", "breaker", "internal"]
    mitigated: bool = False
    mitigated_at: int | None = None
    mitigation_pct: float = 0.0    # % vùng OB đã bị giá lấp (0-1)
    valid: bool = True             # còn hiệu lực để trade hay không
```

### 1.5. Fair Value Gap (FVG)
```python
@dataclass
class FVG:
    index: int                     # vị trí nến giữa (nến tạo gap)
    time: pd.Timestamp
    direction: Literal["bullish", "bearish"]
    top: float
    bottom: float
    filled: bool = False
    filled_pct: float = 0.0
    filled_at: int | None = None
```

### 1.6. Liquidity Pool
```python
@dataclass
class LiquidityPool:
    kind: Literal["equal_highs", "equal_lows", "swing_high", "swing_low"]
    price: float
    indices: list[int]             # các nến tạo thành vùng thanh khoản (equal H/L có >=2 điểm)
    swept: bool = False
    swept_at: int | None = None
    sweep_type: Literal["clean", "wick_only"] | None = None
```

### 1.7. Signal chuẩn hoá (đầu ra mọi module)
```python
@dataclass
class Signal:
    name: str                      # vd: "bos_bullish", "price_in_bullish_ob"
    value: bool
    weight: float = 1.0
    confidence: float = 1.0        # 0-1, optional cho scoring mềm
    meta: dict = field(default_factory=dict)   # dữ liệu phụ trợ (giá, index nguồn...)
```

### 1.8. Trade Setup (kết quả cuối, output confluence engine)
```python
@dataclass
class TradeSetup:
    time: pd.Timestamp
    direction: Literal["long", "short"]
    entry: float
    stop_loss: float
    take_profit: list[float]       # hỗ trợ nhiều TP
    score: float
    signals_fired: list[str]       # tên các signal đã kích hoạt
    risk_reward: float
```

---

## 2. ĐỊNH NGHĨA KHÁI NIỆM & QUY TẮC PHÁT HIỆN

### 2.1. Swing High / Swing Low
**Định nghĩa**: Điểm giá cao/thấp cục bộ, được xác nhận bởi N nến hai bên (fractal).

**Quy tắc phát hiện** (fractal strength = k):
- Swing High tại nến `i` nếu `high[i] > high[i-k..i-1]` VÀ `high[i] > high[i+1..i+k]`.
- Swing Low tương tự với `low`.
- k đề xuất: 2 cho LTF nhạy (M5-M15), 3-5 cho HTF ổn định (H1-D1).
- **Lưu ý**: swing chỉ xác nhận được sau `k` nến kế tiếp đóng cửa → có độ trễ, cần dùng cho backtest (không repaint issue) nhưng phải tính trễ khi mô phỏng live.

### 2.2. Market Structure — BOS vs CHoCH
**BOS (Break of Structure)**: giá đóng cửa vượt qua swing point *cùng hướng* với xu hướng hiện tại → xác nhận xu hướng tiếp diễn.
- Uptrend đang có higher-highs/higher-lows → giá phá higher-high gần nhất bằng nến đóng cửa = BOS bullish.

**CHoCH (Change of Character)**: giá đóng cửa vượt qua swing point *ngược hướng* với xu hướng hiện tại → tín hiệu đảo chiều cấu trúc.
- Trong uptrend, giá phá xuống dưới swing low gần nhất (higher-low) = CHoCH bearish.

**Thuật toán tracking trạng thái xu hướng:**
```
state = {"trend": None, "last_high": None, "last_low": None}
Duyệt qua từng swing point mới xác nhận theo thứ tự thời gian:
  - Nếu giá đóng cửa > last_high đã biết:
        nếu trend == "down" -> CHoCH bullish, đổi trend = "up"
        nếu trend == "up" hoặc None -> BOS bullish, trend = "up"
        cập nhật last_high
  - Nếu giá đóng cửa < last_low đã biết: logic đối xứng cho bearish
```
- **Displacement filter**: đánh dấu `displacement=True` nếu thân nến phá vỡ có `body_size > ATR(14) * 1.5` (nến động lực mạnh, tăng độ tin cậy).

### 2.3. Order Block (OB)
**Định nghĩa**: Nến (hoặc cụm nến) cuối cùng theo hướng ngược lại *ngay trước* một đợt di chuyển giá mạnh (thường đi kèm BOS/CHoCH).

**Quy tắc phát hiện Bullish OB:**
1. Xác định điểm có BOS/CHoCH bullish tại nến `j`.
2. Lùi lại từ `j-1` về trước, tìm nến **giảm** (close < open) gần nhất trước chuỗi tăng dẫn đến breakout.
3. Vùng OB = `[low, high]` của nến đó (một số biến thể dùng `[open, low]` cho vùng "tối ưu").
4. Bearish OB đối xứng: nến **tăng** cuối cùng trước breakout xuống.

**Mitigation (vô hiệu hoá OB):**
- OB được coi là "mitigated" khi giá quay lại chạm vùng `[low, high]` của OB sau khi hình thành.
- `mitigation_pct` = phần trăm vùng đã bị giá xuyên qua (0 = chưa chạm, 1 = xuyên hết).
- Quy tắc invalid phổ biến: nếu giá đóng cửa vượt qua toàn bộ OB theo hướng ngược lại → `valid = False` (OB "chết").

**Breaker Block** (biến thể nâng cao, optional Phase 2): OB bị phá vỡ hoàn toàn rồi đóng vai trò hỗ trợ/kháng cự đảo vai trò.

### 2.4. Fair Value Gap (FVG) / Imbalance
**Định nghĩa**: Khoảng trống giá giữa nến 1 và nến 3 trong chuỗi 3 nến liên tiếp, nơi nến 2 di chuyển mạnh không để lại giao dịch 2 chiều.

**Quy tắc phát hiện (3-candle pattern):**
- Bullish FVG tại nến giữa `i`: `low[i+1] > high[i-1]` → gap = `[high[i-1], low[i+1]]`.
- Bearish FVG tại nến giữa `i`: `high[i+1] < low[i-1]` → gap = `[high[i+1], low[i-1]]`.

**Fill tracking:**
- `filled_pct` cập nhật mỗi nến sau đó dựa trên phần giá đã đi vào vùng gap.
- Ngưỡng thường dùng: FVG được coi "đã dùng" khi `filled_pct >= 0.5` (50%) — có thể để tham số cấu hình.

### 2.5. Liquidity Pool & Liquidity Sweep
**Định nghĩa**: Vùng giá tập trung nhiều stop-loss/pending order — thường là các đỉnh/đáy bằng nhau (equal highs/lows) hoặc swing point rõ rệt.

**Equal Highs/Lows detection:**
- Hai (hoặc nhiều) swing high có chênh lệch giá `<= tolerance` (vd: tolerance = 0.1% giá, hoặc N*pip tuỳ instrument) → gộp thành 1 `LiquidityPool`.

**Liquidity Sweep (quét thanh khoản):**
- Giá xuyên qua mức liquidity pool (high/low) bằng **wick** nhưng **đóng cửa quay lại bên trong** vùng cấu trúc trước đó.
- Điều kiện: `high[i] > pool.price` (hoặc `low[i] < pool.price`) VÀ `close[i]` nằm lại phía trong (không đóng cửa vượt qua).
- `sweep_type = "clean"` nếu chỉ 1 nến quét rồi đảo chiều ngay; `"wick_only"` nếu nhiều nến test nhẹ.
- Đây thường là **trigger** quan trọng nhất — sweep + CHoCH ngay sau đó là combo entry kinh điển trong ICT.

### 2.6. Kill Zone / Session Filter
**Định nghĩa**: Khung giờ thanh khoản cao, nơi smart money hoạt động mạnh (theo giờ UTC hoặc theo giờ sàn).
- London Kill Zone: ~07:00–10:00 UTC
- New York Kill Zone: ~12:00–15:00 UTC
- (Asian range dùng làm vùng tích luỹ tham chiếu, không phải kill zone entry)
- Module chỉ trả `Signal(name="in_killzone", value=bool)` dựa trên `time.hour` của nến hiện tại.

### 2.7. HTF Bias (bối cảnh đa khung thời gian)
**Định nghĩa**: Xu hướng ở timeframe cao hơn dùng làm bộ lọc hướng cho entry ở timeframe thấp.
- Lấy trạng thái `trend` mới nhất từ module BOS/CHoCH chạy trên HTF DataFrame (vd H4/D1).
- Entry LTF chỉ được chấp nhận nếu cùng hướng với HTF bias (trừ khi chiến lược chủ đích counter-trend).

---

## 3. CONFLUENCE ENGINE — LOGIC TỔNG HỢP

### 3.1. Danh sách tín hiệu chuẩn (ví dụ bộ 6 điều kiện)
| Tên signal | Nguồn | Weight đề xuất |
|---|---|---|
| `htf_bias_aligned` | context/htf_bias.py | 1.5 |
| `choch_or_bos_confirmed` | structure/bos_choch.py | 2.0 |
| `price_in_valid_ob` | zones/order_block.py | 2.0 |
| `price_in_fvg` | zones/fvg.py | 1.0 |
| `liquidity_swept_recently` | triggers/sweep.py | 1.5 |
| `in_killzone` | context/killzone.py | 0.5 |
| `displacement_confirmed` | structure/bos_choch.py | 1.0 |

Tổng weight tối đa = 9.5 (tuỳ chỉnh).

### 3.2. Hai chế độ quyết định
**Mode A — AND cứng (conservative):**
```python
REQUIRED = ["choch_or_bos_confirmed", "price_in_valid_ob", "liquidity_swept_recently"]
def decide(signals: dict[str, Signal]) -> bool:
    return all(signals[name].value for name in REQUIRED)
```

**Mode B — Weighted scoring (linh hoạt, tối ưu được qua backtest):**
```python
def decide(signals: dict[str, Signal], threshold: float) -> tuple[bool, float]:
    score = sum(s.value * s.weight for s in signals.values())
    return score >= threshold, score
```
→ Threshold là tham số quét được trong backtest (grid search) để tìm điểm cân bằng win-rate/số lệnh.

### 3.3. Quy trình xử lý mỗi nến mới (event loop)
```
Với mỗi nến mới đóng cửa:
  1. Cập nhật swing points (structure/swings.py)
  2. Cập nhật BOS/CHoCH state
  3. Cập nhật danh sách OB (thêm mới nếu có BOS/CHoCH, update mitigation cho OB cũ)
  4. Cập nhật FVG list (thêm mới, update filled_pct)
  5. Cập nhật liquidity pools (thêm equal H/L mới, check sweep)
  6. Nếu chưa có vị thế mở:
       a. Tính HTF bias (nếu đến thời điểm cập nhật HTF)
       b. Kiểm tra giá hiện tại có nằm trong OB/FVG hợp lệ không
       c. Gom tất cả Signal hiện tại -> confluence.decide()
       d. Nếu đủ điều kiện -> tạo TradeSetup (entry, SL, TP) -> gửi cho execution/backtest engine
  7. Nếu đang có vị thế mở -> quản lý lệnh (trailing, TP1/TP2, breakeven...)
```

### 3.4. Quy tắc đặt SL/TP mặc định
- **SL**: ngay ngoài vùng OB/swing point làm entry (thường + buffer nhỏ = spread hoặc ATR*0.1).
- **TP**: mục tiêu là liquidity pool đối diện gần nhất, hoặc RR cố định (1:2, 1:3) làm baseline để so sánh.
- **Risk per trade**: % vốn cố định (vd 1%), tính position size từ khoảng cách SL.

---

## 4. LỘ TRÌNH TRIỂN KHAI (PHASES)

### Phase 0 — Khung sườn (1 buổi)
- Tạo cấu trúc thư mục, `Signal`, `dataclasses` cơ bản.
- Data loader đọc CSV/MT5 export, chuẩn hoá DataFrame.
- Hàm vẽ chart cơ bản (mplfinance/plotly) để verify trực quan mọi module sau này.

### Phase 1 — Structure (nền móng)
- `structure/swings.py`: fractal detection.
- Verify bằng mắt: vẽ marker lên chart, so khớp tay trên TradingView.
- `structure/bos_choch.py`: state machine BOS/CHoCH.
- Verify: vẽ đường structure line + label BOS/CHoCH.

### Phase 2 — Zones
- `zones/order_block.py` + mitigation tracking.
- `zones/fvg.py` + fill tracking.
- Verify từng cái bằng overlay lên chart, đối chiếu indicator LuxAlgo/ICT có sẵn trên TradingView (chỉ để tham khảo, không sao chép code).

### Phase 3 — Liquidity & Triggers
- `zones/liquidity.py`: equal highs/lows.
- `triggers/sweep.py`: sweep detection.
- `structure/displacement.py` (hoặc gộp vào bos_choch): filter ATR.

### Phase 4 — Context
- `context/killzone.py`, `context/htf_bias.py`.

### Phase 5 — Confluence Engine
- `engine/confluence.py`: implement cả 2 mode (AND cứng + scoring).
- Unit test với dữ liệu giả lập (synthetic data) để đảm bảo logic đúng trước khi chạy dữ liệu thật.

### Phase 6 — Backtest Engine
- `backtest/engine.py`: vòng lặp nến-by-nến (không lookahead bias!), quản lý vị thế, equity curve.
- `backtest/metrics.py`: win rate, avg RR, max drawdown, expectancy, profit factor.
- Chạy grid search cho threshold/weight (Mode B) trên tập dữ liệu train, kiểm định trên tập test riêng (walk-forward, tránh overfit).

### Phase 7 — Tối ưu & Mở rộng (optional)
- Breaker blocks, mitigation block, propulsion block (khái niệm nâng cao).
- Multi-symbol/multi-timeframe scanner.
- Kết nối live (MT5 Python API / cTrader Open API) để chạy signal real-time.

---

## 5. CẠM BẪY CẦN TRÁNH (LESSONS LEARNED TỪ CỘNG ĐỒNG SMC-CODE)

1. **Lookahead bias**: swing point chỉ "biết được" sau k nến xác nhận — nếu backtest dùng future data để vẽ swing thì kết quả ảo, không dùng được live. Luôn tính độ trễ xác nhận vào entry logic.
2. **Repaint**: OB/FVG có thể bị "vẽ lại" nếu logic dùng dữ liệu chưa đóng nến (nến đang chạy). Chỉ xử lý nến đã đóng cửa (`closed candle only`).
3. **Quá nhiều điều kiện AND cứng** → gần như không có lệnh nào khớp đủ (over-filtering). Nên bắt đầu với 3 điều kiện cốt lõi (Structure + Zone + Trigger), thêm dần.
4. **Không tách dữ liệu train/test** khi tối ưu threshold → overfit vào quá khứ, không hoạt động forward. Bắt buộc walk-forward validation.
5. **Bỏ qua transaction cost** (spread, slippage, commission) trong backtest → kết quả lệch xa thực tế, đặc biệt với chiến lược nhiều lệnh nhỏ.
6. **Equal highs/lows tolerance cố định theo giá tuyệt đối** → sai với instrument có giá trị khác nhau (XAUUSD vs EURUSD). Nên dùng tolerance theo ATR hoặc % giá.

---

## 6. THƯ VIỆN PYTHON ĐỀ XUẤT

- `pandas`, `numpy` — xử lý dữ liệu cốt lõi
- `mplfinance` hoặc `plotly` — vẽ chart verify trực quan
- `ta` hoặc tự viết ATR/indicator phụ trợ (tránh phụ thuộc nặng nếu không cần)
- `MetaTrader5` (nếu lấy dữ liệu/live từ MT5) hoặc `ccxt` (nếu crypto)
- `pytest` — unit test cho từng module (đặc biệt structure & zones, dễ sai logic biên)
- `optuna` hoặc grid search tự viết — tối ưu threshold ở Phase 6

---

## 7. FILE ĐẦU RA MẪU (định dạng lưu tín hiệu, dùng cho debug & audit)

```json
{
  "time": "2026-09-08T08:15:00Z",
  "direction": "long",
  "entry": 2512.35,
  "stop_loss": 2508.10,
  "take_profit": [2520.00, 2528.50],
  "score": 6.5,
  "signals_fired": [
    "htf_bias_aligned",
    "choch_or_bos_confirmed",
    "price_in_valid_ob",
    "liquidity_swept_recently"
  ],
  "risk_reward": 2.1
}
```
Lưu mọi TradeSetup ra JSONL/CSV để sau này review lại — đây cũng là dữ liệu để phân tích signal nào thực sự có edge (feature importance) khi anh muốn nâng cấp lên ML sau này.
