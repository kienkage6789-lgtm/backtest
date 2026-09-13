# T53 WAVE 1 SEMANTICS SPECIFICATION (REVISED v5)
## Multi-Strategy SMC Confluence & Selection Engine (Wave 1: S01, S05, S09)

> **Trạng thái**: PROPOSED (Đã chuẩn hóa toàn diện 5 boundary P1 và 3 chi tiết P2 theo đánh giá QC v5)  
> **Tác giả**: AI Lead Architect & Dev Team  
> **Dự án**: Web Trading Backtest Platform — SMC Engine  
> **Tài liệu tham chiếu**: `T53_MULTI_STRATEGY_EXECUTION_PROMPT.md`, `SMC_MULTI_STRATEGY_IMPLEMENTATION_PLAN.md`, `SMC_STRATEGY_CATALOG_V1.md`

---

## 1. Khảo sát Data Contracts & API Hiện Tại (Codebase Survey)

Bảng tổng hợp hợp đồng dữ liệu và các detector thực tế trong codebase (`smc/`):

| Model / Entity | File & Class | Các trường chính | Timestamp / Bar Index | Mutable State nguy cơ Lookahead | Stable ID Scheme |
|---|---|---|---|---|---|
| **SwingPoint** | `smc/models.py`<br>`smc/structure/swings.py` | `index`, `time`, `price`, `kind`, `strength`, `confirmed_at`, `mode`, `classification`, `broken`, `broken_at` | `index`: nến đỉnh/đáy.<br>`confirmed_at`: `index + right_strength`.<br>`broken_at`: nến bị close phá vỡ. | `broken` và `broken_at` bị mutate trong batch khi có nến tương lai break. Tại `as_of` bar $B$, swing chỉ broken nếu `broken_at <= B`. | `sw:{mode}:{kind}:{index}` |
| **StructureEvent** | `smc/models.py`<br>`smc/structure/bos_choch.py` | `index`, `time`, `event_type` (BOS/CHoCH), `direction`, `broken_swing_index`, `close_price`, `displacement`, `structure_leg_id` | `index`: nến đóng cửa tạo break.<br>`confirmed_at`: chính là `index`. | Immutable sau khi sinh. `structure_leg_id = f"{mode}:{direction}:{broken_swing_index}"`. | `struct:{mode}:{event_type}:{direction}:{index}` |
| **FairValueGap** | `smc/models.py`<br>`smc/zones/fvg.py` | `index` (nến giữa), `time`, `direction`, `top`, `bottom`, `confirmed_at` (nến phải $i+1$), `filled`, `filled_at`, `structure_leg_id` | `index`: nến giữa tạo gap.<br>`confirmed_at`: nến $i+1$ đóng cửa.<br>`filled_at`: nến giá lấp kín gap.<br>*(Lưu ý: FVG không có trường `displacement`)*. | `filled` và `filled_at` trong batch phản ánh toàn bộ tương lai. Tại `as_of` bar $B$, FVG chỉ filled nếu `filled and filled_at <= B`. Cấm rò rỉ `filled_at > B`. | `fvg:{mode}:{direction}:{index}` |
| **OrderBlock** | `smc/models.py`<br>`smc/zones/order_block.py` | `index` (source candle), `direction`, `high`, `low`, `source_event_index`, `created_at`, `source_fvg_index`, `mitigated`, `mitigated_at`, `valid`, `invalidated_at`, `retest_count` | `index`: nến nguồn.<br>`source_event_index`: nến BOS/CHoCH.<br>`created_at`: $\max(\text{event.index}, \text{fvg.confirmed\_at})$. | `mitigated`, `mitigated_at`, `valid`, `invalidated_at`, `retest_count` trong batch bị cập nhật bởi nến tương lai. Phải tính lifecycle as-of. | `ob:{mode}:{direction}:{source_event_index}:{index}` |
| **LiquidityPool** | `smc/models.py`<br>`smc/liquidity/detector.py` | `kind`, `price`, `price_max`, `price_min`, `indices`, `created_at`, `confirmed_at`, `swept`, `swept_at`, `valid`, `invalidated_at` | `created_at` / `confirmed_at`: $\max(\text{confirmed\_at})$ của các swings nguồn. | `swept`, `swept_at`, `valid`, `invalidated_at` bị cập nhật tương lai trong batch. Tại `as_of` bar $B$, pool chỉ swept/invalid nếu mốc $\le B$. | `pool:{mode}:{kind}:{'_'.join(indices)}` |
| **LiquiditySweep** | `smc/models.py`<br>`smc/liquidity/detector.py` | `index`, `time`, `direction`, `pool_kind`, `pool_price`, `pool_indices`, `price_wick`, `close_price`, `confirmed_at`, `sweep_type` | `index` = `created_at` = `confirmed_at` = `swept_at`: nến quét râu và đóng cửa bên trong. | Immutable sau khi sinh. Đã được bảo vệ anti-repaint và no-lookahead. | `sweep:{mode}:{direction}:{index}:{'_'.join(pool_indices)}` |
| **SessionDecision** | `smc/models.py`<br>`smc/context/session.py` | `in_session`, `session_name`, `timestamp`, `reason`, `meta` | `timestamp`: mốc UTC nến đã đóng.<br>Đánh giá theo múi giờ chuẩn IANA + DST. | Frozen dataclass, MappingProxyType metadata, hoàn toàn immutable. | `session:{session_name}:{timestamp.isoformat()}` |
| **BiasState** | `smc/models.py`<br>`smc/context/htf_bias.py` | `bias` (bullish/bearish/neutral), `timestamp`, `source_event_index`, `source_event_time`, `as_of`, `reason`, `meta` | `as_of`: cutoff timestamp của nến LTF.<br>Chỉ nhận HTF event có `confirmed_time <= as_of`. | Frozen dataclass, MappingProxyType metadata, hoàn toàn immutable. | `bias:{as_of.isoformat()}:{source_event_index}` |

---

## 2. Decision Tables: 18 Semantics Chung (Đã Khóa Chặt 5 Boundary P1)

| # | Mục Semantics | Phương án khuyến nghị (Recommended) | Phương án thay thế | Lý do chọn | Ảnh hưởng no-lookahead & thực thi | Giá trị mặc định đề xuất |
|---|---|---|---|---|---|---|
| **1** | **Closed bar & `as_of`** | Đánh giá strictly tại nến $N$ đã đóng (`closed=True`). Toàn bộ detector state, bias, pool, FVG, OB chỉ được dùng dữ liệu $[0..N]$. | Đánh giá intra-bar (trên tick/unclosed bar). | Tránh repaint 100%, nhất quán với toàn bộ kiến trúc SMC và BacktestEngine hiện tại. | Zero-lookahead tuyệt đối. Không bị ảnh hưởng bởi biến động giữa nến. | `candle_closed = True` |
| **2** | **Extended Execution Contract & Row Alignment ($i-1$)** | - Tín hiệu sinh tại hàng $N$, khớp tại Open hàng $N+1$.<br>- **Khóa hàng truy cập**: `BacktestEngine` tại nến $i$ bắt buộc đọc tín hiệu và metadata từ **hàng $i-1$**:<br>  `prev_sig = signals[i-1]`<br>  `prev_sl = signal_sl[i-1]`<br>  `prev_tp = signal_tp[i-1]`<br>  `prev_meta = signal_metadata[i-1]`.<br>- **Trigger SL/TP trong Engine**: Trigger kiểm tra trực tiếp `position['sl_price'] > 0` và `position['tp_price'] > 0` (không phụ thuộc `self.stop_loss_val > 0`).<br>- **Fallback Contract (P2.1)**: (1) Nếu cả 3 cột mở rộng không tồn tại $\to$ dùng global config cho legacy strategy; (2) Nếu có SL/TP hữu hạn $> 0 \to$ dùng dynamic; (3) Nếu thiếu 1 cột hoặc chứa NaN/Inf/$\le 0$ khi `signal != 0` $\to$ **Raise ValueError** (tuyệt đối không âm thầm fallback). | Đọc SL/TP ở hàng $i$ hoặc âm thầm fallback khi cột dynamic hỏng. | Khắc phục triệt để lỗi off-by-one (P1.1); bảo đảm SL/TP của chính setup đó được áp dụng đúng tại nến fill $N+1$. | Ngăn chặn leak SL/TP từ tương lai $N+1$ hoặc lùi về $N-1$. | Contract: `signal_bar = N`, `fill_bar = N + 1`, truy cập `signals[i-1]` |
| **3** | **Quote Basis Bid/Ask & Geometry Validation** | - **Input Contract**: `signal_sl` và `signal_tp` truyền từ Adapter là **Structural Price (Bid basis từ OHLC)**.<br>- **Chuyển đổi Executable Levels**:<br>  * Long: $\text{entry} = \text{Open}_{N+1} + \text{spread}$, $\text{sl} = \text{signal\_sl}$, $\text{tp} = \text{signal\_tp}$.<br>  * Short: $\text{entry} = \text{Open}_{N+1}$, $\text{sl} = \text{signal\_sl} + \text{spread}$ (mua lại giá Ask), $\text{tp} = \text{signal\_tp} + \text{spread}$ (mua lại giá Ask).<br>- **Geometry Check trước khi tạo position**:<br>  * Long: bắt buộc $\text{sl} < \text{actual\_entry} < \text{tp}$.<br>  * Short: bắt buộc $\text{tp} < \text{actual\_entry} < \text{sl}$.<br>  * Nếu $N+1$ mở gap xuyên SL/TP hoặc sai geometry: **Hủy lệnh ngay tại Open $N+1$**, lưu vào `execution_events` với reason `geometry_violation_at_fill`. | Bỏ qua spread khi khớp Short SL hoặc cho phép khớp kể cả khi gap qua SL. | Khắc phục triệt để P1.2: bảo vệ vốn khi có gap giá đầu phiên, phản ánh đúng chi phí spread lệnh Short. | Bảo đảm tính khả thi toán học và kinh tế của từng lệnh. | Strict Geometry & Spread-Aware SL/TP |
| **4** | **Same-bar event ordering & Strict Structure-Leg Guard** | - Canonical Sequence: $\text{SWEEP\_SEEN} \to \text{MSS\_CONFIRMED} \to \text{FVG\_READY} \to \text{ENTRY\_PENDING}$.<br>- FVG displacement hình thành trong sóng phá vỡ: $\text{sweep.index} \le \text{fvg.index} < \text{mss.index}$ và $\text{fvg.confirmed\_at} \le \text{mss.index}$.<br>- **Strict Structure-Leg Guard**: Giới hạn $\text{mss.index} - \text{fvg.index} \le 10$ nến VÀ **tuyệt đối không có structure event ngược hướng chen giữa** (`fvg.index` $\le e.\text{index} \le \text{mss.index}$). Nếu có $\to$ Reject candidate (`opposite_structure_shift`).<br>- Retest FVG diễn ra sau khi MSS đã xác nhận ($N_{\text{retest}} > \text{mss.index}$). | Dùng fallback $\pm 3$ bar hoặc cho phép structure ngược chen giữa. | Khắc phục P1.4: thống nhất canonical sequence và ngăn chặn việc ghép nhầm FVG của con sóng khác qua `_leg_for_fvg()`. | Zero-lookahead. Đảm bảo FVG và MSS 100% thuộc cùng 1 structure leg thực tế. | Max FVG-MSS lag: 10 bars, Zero Intervening Opposite Events |
| **5** | **Post-Update Retest & Lookahead-Safe Fill Contract** | **Khắc phục triệt để P1.1 & P1.2**:<br>- **S05 (OB First Retest)**: Nến $N$ hợp lệ iff sau tracker update:<br>  `ob.mitigated_at == N` VÀ `ob.retest_count == 1` VÀ **`ob.valid == True`** VÀ **`ob.invalidated_at is None`**.<br>  *(Nếu nến $N$ đóng xuyên qua OB làm `valid == False` $\to$ Bị loại ngay lập tức!)*.<br>- **S01/S09 (FVG Retest & Fill)**: Trong snapshot as-of $N$, cấm rò rỉ `filled_at > N`. Nến $N$ hợp lệ iff: (1) Nến $N$ chạm zone (`wick touch`); (2) Nến $N$ **không đóng cửa xuyên thủng đáy zone** (Close $\ge$ Bottom cho Buy, Close $\le$ Top cho Sell); (3) FVG chưa bị lấp trước nến $N$: **`fvg.filled_at is None` hoặc `fvg.filled_at == N`**. Bất kỳ FVG có `filled_at < N` bị loại (`invalid_fvg`); có `filled_at > N` bị coi là future leak! | Chỉ kiểm tra `retest_count == 1` mà bỏ qua `valid`, hoặc chấp nhận `filled_at > N`. | Ngăn chặn vào lệnh trên OB đã vỡ; loại bỏ hoàn toàn nguy cơ lookahead leak của FVG. | Zero-lookahead. An toàn tuyệt đối trước các nến break xuyên zone. | Contract: `valid == True` & `filled_at in (None, N)` |
| **6** | **Entry level** | **Proximal boundary**: Mép gần nhất của zone (Bullish: Top FVG/OB; Bearish: Bottom FVG/OB). | Midpoint / 50% Consequent Encroachment (CE), hoặc Full Zone Penetration. | An toàn nhất cho khớp lệnh; nến retest thường chỉ chạm mép ngoài rồi bật đi, chờ 50% CE dễ hụt lệnh. Có config mở rộng. | Entry = Proximal. Cho phép cấu hình `entry_level = "proximal"` hoặc `"ce_50"`. | `entry_level = "proximal"` |
| **7** | **SL anchor & Buffer Contract** | **Structural Extreme**: S01 dùng Sweep extreme wick; S05 dùng OB distal price (Bullish: OB low, Bearish: OB high); S09 dùng Sweep extreme wick; cộng thêm `sl_buffer_price = 0.20` USD (tương đương 20 points trong BacktestEngine). | Dùng pips mơ hồ không rõ tỷ lệ point. | Chuẩn hóa đơn vị tiền tệ/giá tuyệt đối (0.20 USD = 20 points XAU/USD). | Đảm bảo tính nhất quán tuyệt đối giữa SL tính toán và engine khớp lệnh. | `sl_buffer_price = 0.20` USD (20 points) |
| **8** | **Target & RR Policy (Wave 1 Scope)** | **Opposing Liquidity Pool** (từ `LiquidityTracker`) là target chính; nếu không có pool hợp lệ hoặc khoảng cách cho $RR < \text{min\_rr}$, dùng **Fixed RR fallback** (mặc định 2.0R). Bỏ target SessionRange (Asian/London High/Low) khỏi Wave 1 do chưa có module SessionRange. | Xây thêm module SessionRange mới ngay trong Wave 1. | Giữ vững nguyên tắc phân tầng và phạm vi Wave 1; tận dụng các detector LiquidityPool đã pass test và có độ ổn định cao. | Loại bỏ rủi ro target âm hoặc target quá gần không đủ bù spread/commission. | Primary: `opposing_pool`, Fallback: `fixed_rr = 2.0`, `min_rr = 1.5` |
| **9** | **Cash-Based RR Formula & Revalidation Contract** | **Công thức tính Cash-Basis chuẩn xác (Khắc phục P1.2)**:<br>$$\text{risk\_cash} = |\text{actual\_entry} - \text{actual\_sl}| \times \text{lot\_size} \times \text{contract\_size} + 2 \times \text{commission\_per\_side}$$<br>$$\text{reward\_cash} = |\text{actual\_tp} - \text{actual\_entry}| \times \text{lot\_size} \times \text{contract\_size} - 2 \times \text{commission\_per\_side}$$<br>$$\text{Effective } RR = \frac{\text{reward\_cash}}{\text{risk\_cash}}$$<br>- **Quy tắc**: Nếu $\text{reward\_cash} \le 0$ hoặc $\text{risk\_cash} \le 0$ hoặc $\text{Effective } RR < 1.5 \to$ **Hủy lệnh ngay tại Open $N+1$**, lưu vào `execution_events` với reason `insufficient_rr_at_fill`. Spread đã nằm trong executable entry/SL/TP nên không trừ lần 2. | Tính theo khoảng cách giá pips hoặc hạ ngầm về 1.0 tại fill time. | Tính toán đúng đơn vị tiền tệ (USD), phản ánh chính xác 100% PnL thực tế của BacktestEngine. | Loại bỏ hoàn toàn các lệnh không đủ bù chi phí hoa hồng và spread. | `min_rr = 1.5` (Cash-basis) |
| **10** | **Expiry boundary** | **Inclusive**: Setup có hiệu lực đến hết bar $\text{created\_at} + \text{expiry\_bars}$. Tại bar $\text{created\_at} + \text{expiry\_bars} + 1$, setup bị coi là expired. | Exclusive (hết hạn tại đúng bar $\text{created\_at} + \text{expiry\_bars}$). | Rõ ràng, dễ test bằng boundary $[ \text{limit}-1, \text{limit}, \text{limit}+1 ]$. | Tránh lệch 1 nến giữa các hệ thống kiểm thử. | `max_setup_age_bars = 15` (S01, S09), `25` (S05) |
| **11** | **Opposite structure shift invalidation** | Hủy pending setup ngay tại nến đóng cửa xuất hiện **Opposite CHoCH hoặc Opposite BOS** (ví dụ pending Buy gặp Bearish CHoCH). | Giữ pending setup cho đến khi hết expiry bất kể cấu trúc. | Đảo chiều cấu trúc chứng tỏ dòng tiền lớn đã thay đổi hướng đi; cố chấp giữ setup là giao dịch ngược dòng tiền. | Ngăn chặn các lệnh vào sai xu hướng khi thị trường đã flip. | `invalidate_on_opposite_shift = True` |
| **12** | **Cooldown policy** | Cooldown theo `(strategy_id, direction)` trong $K$ bars **chỉ bắt đầu tính sau khi một vị thế thực tế được mở tại Open $N+1$**. Nếu Fill Gate hủy lệnh (do RR hoặc geometry), cooldown KHÔNG kích hoạt. | Kích hoạt cooldown ngay khi sinh signal tại bar $N$. | Nếu lệnh không được khớp thì chiến lược không bị phạt dừng giao dịch vô lý. | Tối ưu hóa khả năng nắm bắt cơ hội hợp lệ tiếp theo. | `cooldown_bars = 3` (tính từ bar mở vị thế) |
| **13** | **Giới hạn setup & lệnh** | Mỗi bar chỉ chọn **tối đa 1 executable setup**. Hệ thống chỉ duy trì **tối đa 1 vị thế mở** tại một thời điểm (single-position constraint của BacktestEngine). | Cho phép mở nhiều vị thế song song (hedging / multi-position). | `BacktestEngine` hiện tại chỉ hỗ trợ `position = None` hoặc 1 vị thế. Mở nhiều vị thế đòi hỏi sửa kiến trúc core engine. | Đảm bảo tính tương thích và an toàn cho platform. | `max_selected_per_bar = 1`, `max_open_positions = 1` |
| **14** | **Market Regime Classifier V1 (Deterministic Specification)** | Khắc phục P1.4: Định nghĩa warm-up bằng số lượng giá trị hợp lệ: `close_count >= 20` VÀ `finite_atr14_count >= 100`. Nếu thiếu $\to$ `uncertain` (`insufficient_warmup_bars`). Denominator ER $= 0 \to \text{ER} = 0.0$. | Kiểm tra $N < 100$ raw bars. | Tránh lỗi mập mờ warm-up; bảo đảm đủ 100 giá trị ATR14 hữu hạn không NaN để tính percentile. | Kháng crash và nhất quán toán học. | Trailing Lookback: 20 bars, finite ATR14 buffer: 100 values |
| **15** | **HTF Bias Policy Thống Nhất (Unified Policy)** | - **Opposed Bias**: Bắt buộc **Hard Reject trên toàn bộ chiến lược** (S01, S05, S09) với reason `htf_bias_mismatch`. Tuyệt đối không chiến lược nào được phép vào lệnh ngược HTF Bias.<br>- **Aligned Bias**: Cả 3 chiến lược đều nhận tối đa 60.0 điểm Context.<br>- **Neutral Bias**: S05 Hard Reject; S01 & S09 cho phép nhưng chỉ nhận 30.0 điểm Context (penalize -30 điểm so với Aligned). | Cho phép S09 đánh ngược Bias chỉ bị trừ 15 điểm. | Đồng bộ hóa 100% kỷ luật giao dịch: Không bao giờ chống lại dòng tiền khung lớn. S01 và S09 chỉ được linh hoạt khi HTF đi ngang (Neutral). | Thống nhất giữa bảng tổng quan và chi tiết từng chiến lược, không còn mâu thuẫn. | `reject_opposed_bias = True` (toàn hệ thống) |
| **16** | **Candidate xung đột đối hướng cùng bar** | Nếu có cả Buy và Sell cùng đạt chuẩn tại 1 bar: tính $\Delta = |\text{Score}_{\text{bull}} - \text{Score}_{\text{bear}}|$. Nếu $\Delta \ge \text{min\_score\_gap}$ (15.0 điểm), chọn bên thắng. Nếu $\Delta < 15.0 \to$ **`NO_TRADE`** (reason: `conflicting_direction`). | Luôn trả `NO_TRADE` khi có xung đột, hoặc chọn ngẫu nhiên. | Thị trường đang phân vân lưỡng lự cực mạnh, trừ khi một bên có ưu thế vượt trội (confluence áp đảo), không vào lệnh là giải pháp an toàn nhất. | Tránh các cú whipsaw 2 đầu rủi ro cao. | `minimum_score_gap = 15.0` |
| **17** | **Evidence Dedup & Strategy Ownership** | Gom các candidate dùng chung evidence (cùng Liquidity Sweep, cùng Structure Leg, hoặc cùng FVG) vào một **Evidence Cluster**. Chọn strategy có score cao nhất làm `primary_owner`, các strategy còn lại ghi nhận vào `supporting_strategy_ids`. | Cho phép bắn cả 2 lệnh, hoặc hủy cả 2. | S01 và S09 thường cùng phát hiện một cơ hội khi Silver Bullet window mở. Không được phép nhân đôi rủi ro trên cùng 1 khối lệnh. | Đảm bảo 1 sự kiện thị trường = 1 rủi ro đơn vị, có telemetry truy vết đa chiến lược. | Cluster key: `f"{direction}:{leg_id}:{fvg_id or ob_id}"` |
| **18** | **Precision & Rounding** | Giá làm tròn 3 chữ số thập phân (`round(p, 3)` cho XAU/USD). Score làm tròn 2 chữ số (`round(s, 2)`). Serialized IDs không dùng float không làm tròn. | Giữ nguyên float 64-bit không làm tròn. | Float không làm tròn gây sai khác vi mô giữa các hệ điều hành/CPU trong test parity và serialization JSON. | Đảm bảo tính deterministic 100% trong so sánh, hashing và assertion test. | Price: `3 decimals`, Score: `2 decimals` |

---

## 3. Semantics Chi Tiết Từng Chiến Lược (Wave 1)

### 3.1. Chiến Lược S01: ICT 2022 Model (Reversal)

#### Chuỗi sự kiện Canonical & State Machine
```text
[1. SWEEP_SEEN] ──────────> Phát hiện Liquidity Sweep tại sweep.index
       │
       ▼ (trong vòng <= 20 nến kể từ sweep)
[2. MSS_CONFIRMED] ───────> Break of Structure / CHoCH xác nhận tại mss.index
       │
       ▼ (tìm displacement FVG cùng leg: fvg.confirmed_at <= mss.index, không có opposite break chen giữa)
[3. FVG_READY] ───────────> FVG hợp lệ sẵn sàng làm entry zone
       │
       ▼ (chờ giá hồi quy <= 15 nến kể từ khi FVG sẵn sàng)
[4. ENTRY_PENDING] ───────> Nến retest chạm FVG (wick touch, close tôn trọng đáy) ──> Emit Signal tại Bar N
       │
       ▼
[5. EXECUTION] ───────────> Revalidate Cash RR >= 1.5 & Geometry tại Open N+1 ──> Khớp Lệnh
```

#### Quy tắc kỹ thuật S01
1. **Liquidity Pools hợp lệ**: `equal_highs`, `equal_lows`, `swing_high`, `swing_low`.
2. **Sweep Validation**: Theo chuẩn `LiquidityTracker` (râu vượt qua pool nhưng nến đóng cửa quay lại bên trong pool).
3. **Displacement FVG Linkage & Strict Leg Guard**:
   - `fvg.direction == mss.direction`
   - $\text{sweep.index} \le \text{fvg.index} < \text{mss.index}$ và $\text{fvg.confirmed\_at} \le \text{mss.index}$.
   - $\text{mss.index} - \text{fvg.index} \le 10$ nến.
   - **Tuyệt đối không có structure event ngược hướng chen giữa** (`fvg.index` $\le e.\text{index} \le \text{mss.index}$). Nếu có $\to$ Reject `opposite_structure_shift`.
4. **SL Anchor**: Sweep Extreme Wick $\pm$ `sl_buffer_price` (0.20 USD):
   - Bullish (Buy): $\min(\text{sweep.price\_wick}, \text{broken\_swing.price}) - 0.20$
   - Bearish (Sell): $\max(\text{sweep.price\_wick}, \text{broken\_swing.price}) + 0.20$
5. **Target & RR**: Opposing Liquidity Pool gần nhất, fallback Fixed RR 2.0R. Bắt buộc Cash-basis $RR \ge 1.5$ tại cả Candidate và Fill time sau spread + phí.
6. **HTF Bias Policy**: Aligned nhận 60.0 điểm, Neutral nhận 30.0 điểm, Opposed bị **Hard Reject** (`htf_bias_mismatch`).
7. **Long/Short Symmetry**: Đối xứng 100% về mọi phép so sánh và tính toán.

---

### 3.2. Chiến Lược S05: BOS $\to$ Order Block Retest (Trend Continuation)

#### Chuỗi sự kiện Canonical & State Machine
```text
[1. BIAS_ALIGNED] ────────> HTF Trend Bias Aligned (BẮT BUỘC: Bullish hoặc Bearish)
       │
       ▼
[2. BOS_CONFIRMED] ───────> BOS cùng hướng Trend Bias tại bos.index
       │
       ▼
[3. OB_READY] ────────────> Order Block sẵn sàng: ob.created_at, ob.structure_leg_id == bos.structure_leg_id
       │
       ▼ (trong vòng <= 25 nến, OB chưa bị close break, chưa retest trước đó)
[4. RETEST_PENDING] ──────> Nến retest chạm OB lần đầu (retest_count == 1, mitigated_at == N, valid == True)
       │
       ▼
[5. EXECUTION] ───────────> Revalidate Cash RR >= 1.5 & Geometry tại Open N+1 ──> Khớp Lệnh
```

#### Quy tắc kỹ thuật S05
1. **HTF Bias**: Bắt buộc Aligned 100% (`bullish_trend` cho Buy, `bearish_trend` cho Sell). Nếu Neutral hoặc Opposed $\to$ **Hard Reject** (`htf_bias_mismatch`).
2. **Same-Leg Linkage**: `ob.structure_leg_id == bos.structure_leg_id` hoặc `ob.source_event_index == bos.index`.
3. **Retest Policy (Post-Update Airtight Check)**:
   Nến $N$ hợp lệ iff:
   $$\text{ob.mitigated\_at} == N \quad \text{AND} \quad \text{ob.retest\_count} == 1 \quad \text{AND} \quad \text{ob.valid} == \text{True} \quad \text{AND} \quad \text{ob.invalidated\_at is None}$$
   *(Nếu nến $N$ chạm OB nhưng đóng cửa phá vỡ OB làm `valid == False` $\to$ Bị loại ngay lập tức!)*
4. **Invalidation**:
   - Close break qua OB (`close < ob.low` với Bullish, `close > ob.high` với Bearish).
   - Xuất hiện Opposite CHoCH trước khi giá chạm OB.
   - Quá thời hạn `max_ob_age` (25 nến).
5. **SL Anchor**: Mép ngoài của Order Block (OB Distal Boundary) $\pm 0.20$ USD:
   - Bullish: `ob.low - 0.20`
   - Bearish: `ob.high + 0.20`
6. **Target & RR**: Đỉnh/đáy tạo bởi cú BOS đó hoặc Opposing Pool. Cash-basis Min RR $\ge 1.5$.

---

### 3.3. Chiến Lược S09: ICT Silver Bullet

#### Chuỗi sự kiện Canonical & State Machine
```text
[1. WINDOW_OPEN] ─────────> Silver Bullet Window mở (Múi giờ America/New_York tự động DST)
       │
       ▼ (xảy ra BÊN TRONG Window)
[2. SWEEP_SEEN] ──────────> Liquidity Sweep xác nhận bên trong Window (đã xóa Range Break)
       │
       ▼ (xảy ra BÊN TRONG Window)
[3. MSS_CONFIRMED] ───────> MSS xác nhận + FVG displacement sẵn sàng
       │
       ▼ (Bên trong Window HOẶC nến đóng cửa trước mốc Grace Expiry 15 phút tuyệt đối)
[4. ENTRY_PENDING] ───────> Retest FVG Touch ──> Signal tại Bar N
       │
       ▼
[5. EXECUTION] ───────────> Revalidate Cash RR >= 1.5 & Geometry tại Open N+1 ──> Khớp Lệnh
```

#### Quy tắc kỹ thuật S09
1. **Múi giờ & Khung giờ chuẩn**: Sử dụng múi giờ `America/New_York` qua module `smc/context/session.py`:
   - **London Open**: 03:00 – 04:00 AM NY Time
   - **NY AM Session**: 10:00 – 11:00 AM NY Time
   - **NY PM Session**: 02:00 – 03:00 PM NY Time
2. **Vị trí Events**: Sweep và MSS bắt buộc xác nhận bên trong Window. FVG phải hình thành bên trong Window. **Bỏ hoàn toàn khái niệm Range Break (chỉ nhận Liquidity Sweep)**.
3. **Grace Period So Với Thời Điểm Đóng Nến (Khắc phục P1.5)**:
   - Nguồn dữ liệu: `StrategyContext` cung cấp trực tiếp `bar_close_time: pd.Timestamp` (tính từ `open_time + timeframe_delta` đã validate).
   - Định nghĩa mốc hết hạn: $T_{\text{grace\_expiry}} = T_{\text{window\_end}} + \text{timedelta}(\text{minutes}=15)$.
   - Nến retest hợp lệ iff **thời điểm đóng nến** $\text{context.bar\_close\_time} \le T_{\text{grace\_expiry}}$.
   - Ngăn chặn hoàn toàn việc nến khung lớn (M15, H1) mở trước mốc hết hạn nhưng đóng sau mốc hết hạn lọt vào hệ thống.
4. **Giới hạn lệnh**: Tối đa **1 setup mỗi Window**.
5. **HTF Bias Policy**: Opposed $\to$ **Hard Reject** (`htf_bias_mismatch`). Aligned $\to$ 60.0 điểm Context. Neutral $\to$ 30.0 điểm Context.
6. **Target Policy (Wave 1)**: Opposing Liquidity Pool từ `LiquidityTracker` hoặc Fixed RR fallback 2.0R.

---

## 4. Market Regime Classifier V1 (Đặc Tả Thuật Toán Định Lượng Tuyệt Đối)

### 4.1. Thông số và Dữ liệu Đầu Vào
- Trailing Window: $W = 20$ bars ($[N-19, \dots, N]$).
- ATR Buffer: Trailing 100 giá trị hữu hạn của $\text{ATR}_{14}$.

### 4.2. Warm-Up & Denominator Guards (Khắc phục P1.4)
1. **Warm-Up Guard**:
   $$\text{close\_count} \ge 20 \quad \text{VÀ} \quad \text{finite\_atr14\_count} \ge 100$$
   Nếu không thỏa mãn $\to$ Trả về regime `uncertain` với reason `insufficient_warmup_bars`.
2. **Denominator Guard**: Trong công thức Kaufman Efficiency Ratio (ER):
   $$\text{denom} = \sum_{k=N-18}^{N} |\text{close}[k] - \text{close}[k-1]|$$
   Nếu $\text{denom} == 0.0$ (thị trường phẳng hoàn toàn, không có dao động giá) $\to$ Gán $\text{ER} = 0.0$.

### 4.3. Cây Phân Loại Deterministic (5 Trạng Thái Rời Rạc)
1. **`bullish_trend`**:
   $\text{HTF\_Bias} == \text{"bullish"}$ VÀ $\text{BOS\_Count}_{\text{bull}} \ge 1$ VÀ $\text{BOS\_Count}_{\text{bear}} == 0$ VÀ $\text{ER} \ge 0.30$.
2. **`bearish_trend`**:
   $\text{HTF\_Bias} == \text{"bearish"}$ VÀ $\text{BOS\_Count}_{\text{bear}} \ge 1$ VÀ $\text{BOS\_Count}_{\text{bull}} == 0$ VÀ $\text{ER} \ge 0.30$.
3. **`volatile_reversal`**:
   $\text{ATR\_Percentile} \ge 60.0$ VÀ $\text{Recent\_Sweep} == \text{True}$.
4. **`ranging`**:
   $\text{ER} < 0.30$ VÀ $\text{BOS\_Count}_{\text{bull}} == 0$ VÀ $\text{BOS\_Count}_{\text{bear}} == 0$ VÀ $\text{ATR\_Percentile} < 60.0$.
5. **`uncertain`**:
   Tất cả các trường hợp còn lại (có BOS 2 chiều xung đột, hoặc HTF Neutral không có sweep).

---

## 5. Ma Trận Chấm Điểm Toàn Diện (Strategy × Direction × Regime Matrix)

Khắc phục triệt để P1.3 bằng bảng tra cứu đầy đủ 30 ô, không để implementation tự suy diễn:

| Strategy | Direction | `bullish_trend` | `bearish_trend` | `volatile_reversal` | `ranging` | `uncertain` |
|---|---|---|---|---|---|---|
| **S05 (BOS $\to$ OB)** | **Long** | **100.0** (ALLOW) | **0.0** (REJECT) | **40.0** (ALLOW) | **60.0** (ALLOW) | **30.0** (ALLOW) |
| **S05 (BOS $\to$ OB)** | **Short** | **0.0** (REJECT) | **100.0** (ALLOW) | **40.0** (ALLOW) | **60.0** (ALLOW) | **30.0** (ALLOW) |
| **S01 (ICT 2022)** | **Long** | **75.0** (ALLOW) | **0.0** (REJECT) | **100.0** (ALLOW) | **60.0** (ALLOW) | **30.0** (ALLOW) |
| **S01 (ICT 2022)** | **Short** | **0.0** (REJECT) | **75.0** (ALLOW) | **100.0** (ALLOW) | **60.0** (ALLOW) | **30.0** (ALLOW) |
| **S09 (Silver Bullet)** | **Long** | **80.0** (ALLOW) | **0.0** (REJECT) | **100.0** (ALLOW) | **60.0** (ALLOW) | **30.0** (ALLOW) |
| **S09 (Silver Bullet)** | **Short** | **0.0** (REJECT) | **80.0** (ALLOW) | **100.0** (ALLOW) | **60.0** (ALLOW) | **30.0** (ALLOW) |

*Ghi chú*: Mọi trường hợp có điểm **0.0 (REJECT)** sẽ bị Eligibility Gate chặn đứng với reason code `wrong_regime` hoặc `htf_bias_mismatch`, hoàn toàn thống nhất với Unified HTF Bias Policy.

---

## 6. Selector, Scoring Engine & Telemetry Policy (Deterministic 100%)

### 6.1. Bảng Điểm và Công Thức Chấm Điểm Tuyệt Đối
Công thức tổng quát:
$$\text{Total Score} = \text{round}(0.25 \cdot S_{\text{regime}} + 0.35 \cdot S_{\text{setup}} + 0.25 \cdot S_{\text{context}} + 0.15 \cdot S_{\text{exec}}, 2)$$

#### 1. Setup Quality ($S_{\text{setup}}$, Trọng số 0.35)
- **Base Score**:
  - Với S05 (OrderBlock):
    * `quality == "premium_candidate"` $\to$ **90.0**
    * `quality == "strong"` $\to$ **75.0**
    * `quality == "base"` $\to$ **60.0**
  - Với S01/S09 (FairValueGap):
    * Nếu `linked_structure_event is not None and linked_structure_event.displacement`: **85.0**
    * Else nếu $\text{gap\_size} = (\text{top} - \text{bottom}) \ge 1.0 \times \text{ATR}_{14}[\text{index}]$: **80.0**
    * Else: **65.0**.
- **Bonuses (Áp dụng tối đa 1 lần mỗi tiêu chí)**:
  - Nếu Sweep gắn liền có `sweep_type == "clean"` (wick ratio $\ge 0.4$): $+10.0$
  - Nếu Pool bị quét là `equal_highs` hoặc `equal_lows`: $+10.0$
  - Nếu FVG có hợp lưu trực tiếp với OrderBlock (`require_ob == True`): $+10.0$
- Công thức:
  $$S_{\text{setup}} = \min(100.0, \max(0.0, \text{Base} + \sum \text{Bonuses}))$$

#### 2. Context Quality ($S_{\text{context}}$, Trọng số 0.25)
- **Bias Score**: Aligned = **60.0**, Neutral = **30.0**, Opposed = **0.0** (bị Hard Gate loại).
- **Session Score**: Nằm trong Kill Zone / Active Window = **40.0**, Nằm ngoài = **10.0**.
- Công thức:
  $$S_{\text{context}} = \min(100.0, \max(0.0, \text{Bias\_Score} + \text{Session\_Score}))$$

#### 3. Execution Quality ($S_{\text{exec}}$, Trọng số 0.15)
Nội suy tuyến tính thuần túy theo tỷ lệ Cash-basis $RR$ đo được:
- Nếu $RR \ge 3.0$: **100.0**
- Nếu $2.0 \le RR < 3.0$: $60.0 + \frac{RR - 2.0}{1.0} \times 40.0$
- Nếu $1.5 \le RR < 2.0$: $30.0 + \frac{RR - 1.5}{0.5} \times 30.0$
- Nếu $RR < 1.5$: **0.0** (bị Hard Gate loại)
- Clamp: $\min(100.0, \max(0.0, S_{\text{exec}}))$.

---

### 6.2. Schema Chuẩn Hóa Của `execution_events` (P2.2)
Khi lệnh bị Fill Gate hủy tại Open $N+1$, sự kiện được lưu vào mảng `execution_events`:
```python
{
    "event_type": "ORDER_REJECTED",
    "signal_bar": int,             # Bar index N sinh tín hiệu
    "signal_time": str,            # ISO timestamp bar N
    "fill_bar": int,               # Bar index N+1 cố gắng khớp lệnh
    "fill_time": str,              # ISO timestamp bar N+1
    "strategy_id": str,
    "setup_id": str,
    "direction": "BUY" | "SELL",
    "reason": str,                 # 'geometry_violation_at_fill' | 'insufficient_rr_at_fill'
    "planned_entry": float,
    "planned_sl": float,
    "planned_tp": float,
    "actual_entry": float,
    "actual_sl": float,
    "actual_tp": float,
    "effective_rr": float,
    "risk_cash": float,
    "reward_cash": float,
    "setup_metadata": dict
}
```

---

### 6.3. Danh Mục 18 Reason Codes Chuẩn Hóa
1. `wrong_regime`: Sai chế độ thị trường.
2. `htf_bias_mismatch`: Đi ngược xu hướng HTF Bias hoặc S05 gặp Neutral Bias.
3. `missing_required_evidence`: Thiếu Sweep, FVG, hoặc BOS/CHoCH.
4. `invalid_event_order`: Sự kiện sai trình tự thời gian.
5. `outside_session`: Nằm ngoài khung giờ cho phép.
6. `expired_setup`: Quá thời hạn nến chờ hồi quy.
7. `invalid_order_block`: OB đã bị close break hoặc đã retest trước đó.
8. `invalid_fvg`: FVG đã bị lấp kín trước nến hiện tại (`filled_at < N`).
9. `fvg_invalidated_by_close`: Nến retest đóng cửa xuyên thủng đáy zone FVG.
10. `stale_liquidity_sweep`: Quá 20 nến từ nhịp quét thanh khoản.
11. `opposite_structure_shift`: Có structure event ngược hướng chen giữa FVG và MSS hoặc xuất hiện trước khi khớp lệnh.
12. `insufficient_rr`: Cash-basis RR tại Candidate Gate $< 1.5$.
13. `insufficient_rr_at_fill`: Cash-basis RR tại Fill Gate Open $N+1$ sau spread/commission $< 1.5$.
14. `geometry_violation_at_fill`: Nến $N+1$ mở gap xuyên qua SL/TP hoặc làm sai lệch hình học giá.
15. `cooldown_active`: Đang trong thời gian cooldown 3 nến sau lệnh đã khớp.
16. `conflicting_direction`: Xung đột 2 chiều điểm chênh lệch $< 15.0$.
17. `insufficient_score`: Tổng điểm Confluence $< 60.0$.
18. `insufficient_warmup_bars`: Chưa đủ số nến trailing buffer ($N < 100$) để tính toán regime an toàn.

---

## 7. Kế Hoạch Kiểm Thử Toàn Diện (Comprehensive Verification & QA Protocol)

### 7.1. Lệnh Kiểm Thử & Kiểm Tra Cú Pháp
```powershell
.\.venv\Scripts\python.exe -m unittest discover tests -v
.\.venv\Scripts\python.exe -m compileall -q server.py engine smc tests
node --test tests/test_drawings.test.js
node --test tests/test_ui_structure.test.js
node --test tests/test_drawer.test.js
git diff --check
```

### 7.2. Benchmark Hiệu Năng Chi Tiết
- **Policy vận hành theo ADR 19**: baseline hiện tại được chấp nhận cho closed-bar real-time với số lượng mã hạn chế; performance benchmark không nằm trong correctness suite bắt buộc.
- **Technical debt**: mục tiêu `< 7.0s cho 10.000 nến` và mục tiêu dài hạn `< 1.5s` tiếp tục được đo bằng test opt-in/probe riêng nhưng không chặn T53.3.
- **Môi trường tham chiếu chuẩn hóa (P2.3)**:
  - CPU: AMD / Intel x86_64 Multi-Core (Class Intel Core i7 / AMD Ryzen 7 hoặc tương đương).
  - OS: Windows 10/11 64-bit.
  - Python: 3.10+ trong `.venv` (virtualenv cô lập).
  - Execution: 1 run warm-up và tính trung bình 3 lần đo liên tiếp.
  - Status: Mục tiêu thử nghiệm (provisional). Nếu chạy trên môi trường CPU khác, so sánh tỷ lệ hồi quy so với baseline `run_smc_strategy()`.
- **Phạm vi đo riêng**:
  1. `StrategyContext` builder (zero-lookahead snapshot).
  2. 3 Strategy templates (`S01`, `S05`, `S09`).
  3. Regime classifier & Eligibility Gate.
  4. Evidence Deduplicator & Selector.
  5. End-to-end 10.000 bars backtest pipeline.

### 7.3. Independent Read-Only QC Protocol (Gate E)
Sau khi hoàn tất Gate E, kích hoạt một sub-agent độc lập ở chế độ read-only để:
- Tự viết probe kiểm chứng no-lookahead, same-bar boundary, future-append invariance.
- Kiểm tra duplicate evidence, row $i-1$ signal alignment, và N+1 fill contract.
- Xác nhận không còn lỗi P0/P1 trước khi nghiệm thu T53.
