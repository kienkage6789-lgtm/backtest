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

## 2026-09-04 - ADR 03: Lấy schema database hiện tại làm nguồn chuẩn
- **Bối cảnh**: QC xác nhận `data/XAUUSD.db` hiện chỉ có bảng `XAUUSD_M1`; tên `XAUUSDc_M1` trong code/tài liệu không khớp và các bảng H1/D1 chưa tồn tại.
- **Quyết định**: Sửa DataFeed theo schema thực tế của DB (`XAUUSD_M1`), đồng thời thiết kế H1/D1 từ nguồn M1 theo một cơ chế nhất quán; không đổi hoặc xóa dữ liệu gốc trong quá trình fix.
- **Lý do**: Tránh phụ thuộc vào tên bảng của dataset khác và bảo đảm app chạy được với artifact đang nằm trong workspace.

## 2026-09-04 - ADR 04: Khóa quy ước khớp lệnh để loại bỏ lookahead
- **Bối cảnh**: Engine hiện phát hiện tín hiệu từ close của nến N rồi vào lệnh ngay trên close N, làm kết quả backtest lạc quan giả.
- **Quyết định**: Tín hiệu tại nến N được khớp tại nến N+1 theo giá mở cửa và mô hình spread đã ghi rõ; mọi thay đổi phải có test synthetic.
- **Lý do**: Đây là quy ước bar-by-bar an toàn và có thể kiểm chứng, phù hợp dữ liệu OHLC không có tick.

## 2026-09-04 - ADR 05: Khớp Short SL/TP theo giá Ask và ưu tiên Stop Loss khi nến quét 2 đầu
- **Bối cảnh**: Lệnh Short đóng lệnh bằng việc mua lại ở giá Ask (Bid + Spread). Do các cột High/Low trong database là giá Bid, việc so sánh trực tiếp High/Low với SL/TP sẽ bỏ qua chi phí spread. Đồng thời, khi nến biến động cực lớn chạm cả SL và TP, cần quy ước rõ ràng.
- **Quyết định**:
  - Với lệnh Short: Stop Loss kích hoạt khi `High + Spread >= sl_price`; Take Profit kích hoạt khi `Low + Spread <= tp_price`.
  - Khi một cây nến chạm đồng thời cả Stop Loss và Take Profit, hệ thống quy ước ưu tiên Stop Loss trước theo nguyên tắc phòng ngừa rủi ro bảo toàn vốn trong giao dịch thực tế (conservative risk modeling).
- **Lý do**: Đảm bảo kết quả backtest phản ánh chính xác chi phí trượt giá/spread của lệnh Short và không bị lạc quan hóa khi thị trường giật mạnh hai chiều.

## 2026-09-04 - ADR 06: Chính sách xử lý Partial Candle trong Bar Replay
- **Bối cảnh**: Khi người dùng chọn điểm cắt `cut_time` ở khung thời gian lớn (M5, M15, H1, D1), mốc thời gian có thể rơi vào giữa chu kỳ của một cây nến (ví dụ `14:07` thuộc nến H1 `14:00-15:00`).
- **Quyết định**:
  - Nếu `cut_time` trùng khớp thời điểm bắt đầu cây nến (hoặc nến đã trọn vẹn): nến được tính vào lịch sử (`history`).
  - Nếu `cut_time` rơi vào giữa cây nến (partial candle): loại bỏ cây nến dở dang khỏi `history` (nến history cuối cùng là nến đóng hoàn chỉnh trước đó), và đưa nguyên cây nến đầy đủ đó vào làm phần tử đầu tiên của `future` queue.
- **Lý do**: Đảm bảo tính toàn vẹn dữ liệu: mọi phút M1 đều được tổng hợp đầy đủ vào nến resample tương ứng, không có phút nào bị biến mất, không có nến dở dang gây sai lệch chỉ báo kỹ thuật, và không bao giờ trùng lặp timestamp giữa history và future.

## 2026-09-04 - ADR 07: Xây dựng Drawing Engine custom trên Lightweight Charts
- **Bối cảnh**: Lightweight Charts không có bộ công cụ vẽ hình học tích hợp sẵn. Cần xây dựng lớp overlay độc lập để hỗ trợ các công cụ phân tích kỹ thuật.
- **Quyết định**: Xây dựng lớp drawing overlay bằng SVG độc lập, lưu anchor theo `time + price`, dùng coordinate conversion của chart (`timeToCoordinate`, `priceToCoordinate`) để render và hit-test; không vẽ bằng pixel cố định.
- **Lý do**: Drawing giữ đúng vị trí khi zoom, pan, resize và đổi khung thời gian, hoàn toàn không phụ thuộc hay làm thay đổi dữ liệu nến.

## 2026-09-04 - ADR 08: Định nghĩa phạm vi công cụ vẽ phổ biến (Parity Scope)
- **Bối cảnh**: Hệ thống TradingView Charting Library có license hỗ trợ hàng trăm công cụ và chỉ báo phức tạp. Cần xác định rõ ranh giới cho bản offline.
- **Quyết định**: Tập trung xây dựng bộ công cụ vẽ và phân tích kỹ thuật cốt lõi: Trend Line, Ray, Extended, Horizontal/Vertical Line, Rectangle, Channel, Fibonacci Retracement/Extension, Ruler/Measure, Text/Arrow, Style Bar, Undo/Redo, LocalStorage, Replay & Dual Chart. Không triển khai cloud sync, Pine Script, Alert hay social sharing.
- **Lý do**: Đảm bảo hoàn thành đúng cam kết với chất lượng cao nhất, chạy mượt mà 100% offline.

## 2026-09-04 - ADR 09: State Machine SVG Overlay & Lưu trữ theo Symbol Layout
- **Bối cảnh**: SVG overlay nếu chặn toàn bộ chuột sẽ làm tê liệt pan/zoom của chart; nếu lưu theo timeframe sẽ làm mất drawing khi đổi timeframe.
- **Quyết định**:
  - State machine: Ở Select mode, SVG container `pointer-events: none`, các shape/anchor `pointer-events: stroke / all`. Ở Draw mode, SVG container `pointer-events: all`, sau khi đặt đủ anchor tự động chuyển về Select mode.
  - Lưu trữ: Lưu theo `drawings:XAUUSD:layout` với `scope: "symbol" | "timeframe"` (mặc định `symbol`) để drawing tồn tại xuyên suốt các khung thời gian.
- **Lý do**: Tương tác mượt mà chuẩn TradingView, giải quyết triệt để mâu thuẫn lưu trữ và tránh chặn tương tác biểu đồ.

## 2026-09-05 - ADR 10: Cơ chế Vẽ Vô Hạn Tương Lai bằng Logical Index và Đồng Bộ Đa Trục qua Container Pointer Tracker & rAF
- **Bối cảnh**:
  1. Lightweight Charts v4 giới hạn `coordinateToTime(x)` trả về `null` trong whitespace. Nếu nến cuối bị cuộn ra khỏi màn hình bên trái (`timeToCoordinate(last.time) === null`), việc phụ thuộc vào `lastX` khiến việc ngoại suy tương lai bị hỏng. Ngoài ra `rightOffset: 10` cố định làm giới hạn không gian tương lai.
  2. Lightweight Charts không có event công khai cho trục giá (`subscribePriceRangeChange`). Sử dụng `attachPrimitive` có nguy cơ gây render loop nếu `updateAllViews` được gọi trong chính frame vẽ của chart.
  3. Tính interval giữa 2 nến cuối dễ bị sai lệch nếu rơi vào gap cuối tuần hoặc dữ liệu nghỉ lễ.
- **Quyết định**:
  1. **Ngoại suy 2 chiều hoàn toàn qua Logical Index & Nới rightOffset động**:
     - Chuyển đổi `x -> logical -> time`: Sử dụng `timeScale.coordinateToLogical(x)`. Khi `logical > candles.length - 1`, tính `diffBars = logical - (candles.length - 1)` và `time = Math.round(last.time + diffBars * interval)`.
     - Chuyển đổi `time -> logical -> x`: Khi `time > last.time`, tính `targetLogical = (candles.length - 1) + (time - last.time) / interval` và `x = timeScale.logicalToCoordinate(targetLogical)`. Hoàn toàn không phụ thuộc vào `timeToCoordinate(last.time)` và hoạt động cả khi nến cuối đã bị cuộn ra ngoài màn hình.
     - Tự động nới `rightOffset` của `timeScale` khi người dùng đặt điểm hoặc kéo vẽ vượt xa mép tương lai hiện tại để đảm bảo tính vô hạn thực tế.
     - Mở rộng viewport bounding box trong `render()` bằng `getVisibleLogicalRange()` để không cull nhầm các nét vẽ tương lai.
  2. **Tính Interval bằng Median đa nến (Multi-Candle Median)**:
     - Lấy delta thời gian của nhóm 20-30 nến gần nhất, loại bỏ delta <= 0 hoặc bất thường, lấy median.
     - Fallback theo `currentTimeframe` (M1: 60s, M5: 300s, M15: 900s, H1: 3600s, D1: 86400s...) nếu không đủ nến. Tuyệt đối không hard-code 60 giây và không thêm nến giả vào OHLC.
  3. **Đồng bộ đa trục an toàn qua Container Pointer Tracker & rAF Batching**:
     - Không dùng `attachPrimitive()` làm cơ chế chính để loại bỏ 100% rủi ro render loop.
     - Lắng nghe `pointerdown`/`pointermove`/`pointerup` trên `this.container` và `window` để bắt trọn vẹn thao tác kéo thanh giá (Price Scale) và thanh thời gian (Time Scale) ở mọi chế độ (kể cả Select Mode).
     - Kết hợp với `subscribeVisibleLogicalRangeChange`, `wheel`, `dblclick`, `ResizeObserver`.
     - Toàn bộ sự kiện được gom lại qua `requestAnimationFrame` có cờ `renderPending`, đảm bảo 1 frame chỉ render tối đa 1 lần, hủy sạch rAF ID khi `destroy()`.
- **Lý do**: Giải quyết triệt để 2 lỗi vẽ, loại bỏ hoàn toàn rủi ro render loop, hoạt động ổn định trên mọi loại thiết bị và màn hình.

## 2026-09-09 - ADR 11: Chốt policy QC cho Order Block/FVG
- `require_ob` giữ mặc định `False`: OB là bộ lọc confluence tùy chọn để bảo toàn hành vi hiện tại; khi bật `True`, setup chỉ hợp lệ nếu có OB còn valid theo policy.
- Strong OB chỉ được nâng quality khi FVG cùng direction, cùng mode và cùng structure leg. Leg được biểu diễn bằng metadata nếu upstream cung cấp; nếu không, detector suy ra leg bằng event cùng hướng gần nhất và loại FVG có structure event ngược hướng chen giữa.
- `OrderBlockTracker` giữ pending event khi `require_fvg=True` nhưng FVG chưa đến; event chỉ được deduplicate sau khi OB thực sự được tạo. Late FVG chỉ được chấp nhận khi `require_fvg_before_event=False`.
- `max_active_blocks` không được dùng để evict OB còn valid khỏi active state, vì làm stale mitigation/invalidation. Tracker giữ toàn bộ active lifecycle; giới hạn này chỉ là metadata/policy hook cho tối ưu hóa tương lai không làm mất trạng thái.

## 2026-09-09 - ADR 12: Chọn Liquidity Pool/Sweep làm milestone SMC tiếp theo
- **Bối cảnh**: Swing, Structure, FVG và Order Block đã có implementation và đã đóng các lỗi QC lookahead/parity chính. Plan tổng quy định Liquidity/Sweep đứng trước Context và Confluence.
- **Quyết định**: T51 triển khai Liquidity Pool/Sweep trước; T52 triển khai Context; T53 mới xây Confluence Engine. Chưa mở rộng sang Breaker Block, Mitigation Block hoặc Propulsion Block.
- **Lý do**: Liquidity sweep là input trực tiếp cho confluence và cần semantics zero-lookahead, batch/incremental parity và lifecycle rõ ràng trước khi ghép nhiều tín hiệu thành `TradeSetup`.
- **Ràng buộc**: Pool chỉ xuất hiện sau pivot confirmation; sweep chỉ xác nhận trên candle đóng; close vượt pool là break/invalidation; mọi timestamp và serialization phải parity giữa batch và tracker.

## 2026-09-09 - ADR 13: Quy định Semantics và Policy cho Liquidity Pool & Liquidity Sweep (T51)
- **Bối cảnh**: Cần xây dựng module Liquidity Pool (Equal Highs, Equal Lows, Swing High, Swing Low) và Liquidity Sweep không repaint, zero-lookahead, đồng thời đảm bảo tính tương đương giữa batch và incremental.
- **Quyết định**:
  1. **Pool Generation**:
     - Chỉ tạo pool từ các SwingPoint đã confirmed (`confirmed_at <= current_bar_index`).
     - Hỗ trợ tolerance policy theo phần trăm giá (`tolerance_pct`), pip/point (`tolerance_pips`), hoặc ATR (`tolerance_atr_mult`). Validate tham số <= 0.
     - `created_at` và `confirmed_at` của pool được gán bằng `max(confirmed_at)` của các swing nguồn. Pool không tồn tại đối với caller trước `confirmed_at`.
     - Cho phép gộp nhiều swing cùng kind/mode vào pool nếu nằm trong tolerance. Mức giá đại diện `price` = avg price, `price_max` = max high/price, `price_min` = min low/price.
  2. **Sweep Detection Semantics**:
     - Chỉ đánh giá trên nến đã đóng hoàn toàn (`current_bar_index`).
     - **Bearish Sweep**: Nến có `high > pool.price_max` (xuyên qua) và `close <= pool.price_max` (đóng cửa quay lại dưới pool). Direction trả về `"bearish"` (tín hiệu short/sell potential).
     - **Bullish Sweep**: Nến có `low < pool.price_min` (xuyên qua) và `close >= pool.price_min` (đóng cửa quay lại trên pool). Direction trả về `"bullish"` (tín hiệu long/buy potential).
     - **Close Break / Invalidation**: Nếu nến đóng cửa vượt hẳn pool (`close > pool.price_max` đối với High pool, hoặc `close < pool.price_min` đối với Low pool), pool bị đánh dấu `valid = False`, `invalidated_at = bar_index`, `invalidation_reason = "close_break"`. Nến này KHÔNG được coi là Liquidity Sweep.
     - **Touch only**: Nến chỉ chạm (`high == pool.price_max` hoặc `low == pool.price_min`) mà không đâm xuyên qua thì KHÔNG phải sweep.
     - **Lifecycle**: Pool khi bị sweep lần đầu tiên sẽ đánh dấu `swept = True`, `swept_at = bar_index`, `sweep_type = "clean"` hoặc `"wick_only"`. Mặc định sau sweep clean/wick, pool chuyển `valid = False` đối với active tracker để tránh duplicate sweep lặp lại.
  3. **Batch/Incremental Parity**:
     - Batch function và Incremental `LiquidityTracker` phải trả về tập object trùng khớp 100% tất cả các trường (`index`, `time`, `direction`, `price`, `indices`, `created_at`, `confirmed_at`, `swept_at`, `sweep_type`, `valid`, metadata và `to_dict()`).

## 2026-09-09 - ADR 14: Quy định Semantics và Policy cho Context (KillZone/SessionFilter và HTF Bias Adapter - T52)
- **Bối cảnh**: Cần xây dựng lớp Context độc lập gồm `KillZone/SessionFilter` và `HTF Bias Adapter` đáp ứng tuyệt đối yêu cầu zero-lookahead, timezone/DST-aware, đóng nến (closed-candle only), batch/incremental parity, và không làm hỏng các module hiện có.
- **Quyết định**:
  1. **HTF Event Effective Time & As-Of Mapping**:
     - HTF mapping sang LTF được thực hiện thuần túy bằng timestamp (`effective_time <= t_ltf`). Tuyệt đối không dùng integer index giữa các timeframe vì số lượng bar và gaps khác nhau.
     - `effective_time` ưu tiên `getattr(event, 'confirmed_time', None)` nếu có; nếu không, lấy `event.time` (thời điểm đóng cây nến tạo break trên HTF).
     - `event.effective_time == t_ltf` được chấp nhận hợp lệ (tính as-of tại mốc đó event đã đóng).
     - `cutoff_time` là timestamp UTC inclusive (`effective_time <= cutoff_time`).
  2. **Conflict & Validation Policy**:
     - `conflict_policy` CHỈ hỗ trợ `"neutral"`. Bất kỳ giá trị nào khác lập tức raise `ValueError`.
     - Nếu nhiều event tại cùng timestamp HTF lớn nhất có cả hướng `bullish` và `bearish`, bias trả về là `"neutral"` với `reason="conflicting_events"`.
     - Event input unsorted phải được sắp xếp deterministic theo `(effective_time, index, event_type)`. Deduplicate theo composite key `(effective_time, index, event_type, direction)`.
     - `event.time` bắt buộc là timezone-aware; naive timestamp sẽ bị reject bằng `ValueError`.
  3. **Future & Late Event Contract trong HTFBiasTracker**:
     - Khi `current_ltf_time is None`: lưu `htf_event` vào `_known_events`, trả về `None`.
     - Event tương lai (`effective_time > current_ltf_time`) được lưu vào hàng đợi nhưng không tính vào bias của nến hiện tại; chỉ có hiệu lực khi `current_ltf_time >= effective_time`.
     - Event đến trễ (`effective_time < last_ltf_time`) được nạp vào bộ nhớ để cập nhật bias từ nến hiện tại trở đi (`meta["late_event"] = True`), không hồi tố thay đổi lịch sử các `BiasState` đã emit.
  4. **Session Boundary, Overnight Attribution & DST**:
     - Boundary: `start <= local_time < end` (start inclusive, end exclusive). Tại `local_time == end` trả về `in_session = False`, `reason = "at_session_end"`.
     - Session qua nửa đêm (overnight, $start > end$, ví dụ `22:00–02:00`): thuộc về ngày bắt đầu phiên. Nến nằm trong đoạn `00:00–02:00` sáng hôm sau được gán weekday của ngày hôm trước (`(weekday - 1) % 7`) để kiểm tra `days`.
     - Timezone & DST: chuyển đổi qua `zoneinfo` / `tz_convert`. Session UTC cố định không chịu ảnh hưởng DST; session theo local exchange (London, New York) tự động dịch chuyển giờ UTC tương ứng khi DST bắt đầu/kết thúc.
     - Naive timestamp: mặc định reject với `reason="naive_timestamp"`, trừ khi có `default_timezone` được truyền để localize an toàn.
  5. **Precedence Kháng Lỗi Cho Closed Status**:
     - Có cả 2 cột `closed` và `is_closed`: raise `ValueError`.
     - Có 1 trong 2: dùng giá trị cột đó. Nếu parameter `candle_closed` mâu thuẫn với cột: raise `ValueError`.
     - Không có cả 2 cột: dùng `candle_closed` nếu có; nếu `candle_closed is None`: raise `ValueError`.
  6. **Multi-session Aggregation**:
     - `evaluate_session_window()`: 1 decision cho 1 window.
     - `evaluate_sessions_batch()`: 1 decision tổng hợp cho mỗi candle.
     - `check_killzone_signal()`: `value = True` nếu $\ge 1$ session match. `session_name` là session match đầu tiên; `meta["matched_sessions"]` chứa toàn bộ session match.
  7. **Immutability**:
     - `SessionDecision.meta` và `BiasState.meta` dùng type annotation `collections.abc.Mapping`, runtime bọc `types.MappingProxyType` và `to_dict()` unpack sang dict sạch.
  8. **Strict Closed-State Boolean Parser & Diagnostic Separation**:
     - Triển khai `_parse_bool_strict`: chỉ chấp nhận bool thật, int 0/1, float 0.0/1.0, string "true"/"false"/"1"/"0" (case-insensitive).
     - Mọi giá trị `NaN`, `None`, số khác 0/1 hoặc chuỗi lạ lập tức raise `ValueError`, ngăn chặn hoàn toàn zero-lookahead leak do `bool("false") == True` hay `bool(NaN) == True`.
     - Nến chưa đóng (`closed=False`) trả về diagnostic `SessionDecision(in_session=False, reason="partial_candle")`.
  9. **Monotonic Timestamp Stream Guard**:
     - `HTFBiasTracker` và `SessionFilter` yêu cầu timestamp của nến sau phải $\ge$ nến trước (`current_ltf_time >= _last_ltf_time`); nếu đi lùi raise `ValueError`.
     - Cung cấp phương thức `reset()` cho phép xóa state và timestamp tracking phục vụ replay rewinding sạch sẽ.
## 2026-09-09 - ADR 15: T53 triển khai deterministic multi-strategy theo từng wave
- **Bối cảnh**: Hệ thống đã có detector Structure/OB/FVG/Liquidity và Context; danh mục đã xác định 10 strategy có khả năng code hóa cao. Nếu implement đồng thời cả 10 hoặc để LLM quyết định trực tiếp sẽ khó chứng minh event ordering, deduplication, parity và nguyên nhân vào lệnh.
- **Quyết định**: Mở rộng T53 thành `Multi-Strategy Confluence & Selection Engine`. Wave 1 chỉ gồm S01 ICT 2022 Reversal, S05 BOS → OB Retest và S09 Silver Bullet. Engine deterministic phải hoàn tất gate/dedup/conflict/selector và backtest QC trước Wave 2/3.
- **Kiến trúc**: Domain package mới nằm trong `smc/engine/`; top-level `engine/` tiếp tục chịu trách nhiệm platform/backtest. Giữ `run_smc_strategy()` hiện tại làm baseline trong Wave 1.
- **Ngoài phạm vi**: LLM supervisor, contextual bandit và online optimization chỉ được xem xét sau khi có kết quả walk-forward của các strategy deterministic.
- **Lý do**: Ba strategy Wave 1 đại diện cho reversal, continuation và time-based logic, đủ để kiểm chứng framework mà không mở rộng scope quá sớm.

## 2026-09-09 - ADR 16: Khóa Semantics và Quy Tắc Thực Thi Wave 1 (S01, S05, S09, Selection Engine) [ACCEPTED]
- **Trạng thái**: ACCEPTED (Người dùng đã chính thức phê duyệt Gate A v5 tại T53.0).
- **Bối cảnh**: Cần chốt quy chuẩn thực thi, event ordering, trigger, touch, entry, SL, target, invalidation, deduplication và conflict resolution cho Wave 1 (S01, S05, S09).
- **Quyết định đề xuất**:
  1. **S05 First Retest Airtight Boundary (Chặn Nến Đóng Xuyên OB)**:
     - Nến $N$ hợp lệ iff sau tracker update: `ob.mitigated_at == N and ob.retest_count == 1 and ob.valid == True and ob.invalidated_at is None`.
     - Nếu nến $N$ chạm OB nhưng đóng nến xuyên thủng OB (`valid == False`, `invalidated_at == N`), setup bị loại ngay lập tức (`invalid_order_block`).
  2. **FVG As-Of Snapshot & Khóa Tràn Tương Lai (Future-Leak Guard)**:
     - Snapshot as-of $N$ cấm tuyệt đối `filled_at > N`. Nến $N$ retest hợp lệ iff chạm zone, Close không xuyên đáy zone, và `fvg.filled_at is None` hoặc `fvg.filled_at == N`.
     - Bất kỳ FVG có `filled_at < N` bị reject `invalid_fvg`; nếu phát hiện `filled_at > N` trong as-of snapshot $\to$ raise AssertionError (future leak).
  3. **Canonical Sequence & Strict Structure-Leg Guard**:
     - S01: $\text{SWEEP\_SEEN} \to \text{MSS\_CONFIRMED} \to \text{FVG\_READY} \to \text{ENTRY\_PENDING}$.
     - FVG displacement hình thành trong sóng phá vỡ ($\text{sweep} \le \text{fvg.index} < \text{mss.index}$, $\text{fvg.confirmed\_at} \le \text{mss.index}$, lag $\le 10$ bars).
     - **Chặn event ngược chiều**: Tuyệt đối không có structure event ngược hướng chen giữa `fvg.index` và `mss.index`. Nếu có $\to$ Reject candidate (`opposite_structure_shift`).
  4. **Ma Trận Tra Cứu 30 Ô Chuẩn Hóa (Strategy × Direction × Regime Matrix)**:
     - Khóa bảng 30 ô: S05 Long 100.0 ở `bullish_trend`, S05 Short 100.0 ở `bearish_trend`, S01/S09 100.0 ở `volatile_reversal`. Các ô ngược trend nhận 0.0 (REJECT).
  5. **Warm-Up Guard Định Lượng & Denominator Guard**:
     - Warm-Up Guard: `close_count >= 20` VÀ `finite_atr14_count >= 100` (đếm phần tử hữu hạn không NaN). Nếu thiếu $\to$ trả về `uncertain` (`insufficient_warmup_bars`).
     - Kaufman ER denominator $= 0 \implies \text{ER} = 0.0$.
  6. **Unified HTF Bias Policy**:
     - Opposed Bias bị **Hard Reject trên toàn bộ chiến lược** (`htf_bias_mismatch`). S05 bắt buộc Aligned (60 điểm Context). S01/S09 cho phép Neutral (30 điểm Context).
  7. **S09 Bar Close Time Trong StrategyContext Cho Grace Period**:
     - `StrategyContext` cung cấp trực tiếp `bar_close_time: pd.Timestamp` (`open_time + timeframe_delta`).
     - Nến retest hợp lệ iff $\text{context.bar\_close\_time} \le T_{\text{window\_end}} + \text{timedelta}(\text{minutes}=15)$.
  8. **Extended Signal Row Alignment ($i-1$) & Dynamic SL/TP Fix**:
     - `BacktestEngine` tại nến $i$ đọc tín hiệu và metadata từ **hàng $i-1$** (`signals[i-1]`, `signal_sl[i-1]`, `signal_tp[i-1]`, `signal_metadata[i-1]`).
     - Trigger SL/TP chuyển sang kiểm tra trực tiếp `position['sl_price'] > 0` và `position['tp_price'] > 0`.
     - Fallback Contract: Thiếu cả 3 cột $\to$ fallback global; Có đủ SL/TP $>0$ $\to$ dynamic; Thiếu 1 cột hoặc chứa NaN/Inf/$\le 0$ khi signal $\neq 0 \to$ Raise `ValueError`.
  9. **Quote Basis Bid/Ask, Geometry Validation & Cash-Basis RR**:
     - `signal_sl`/`signal_tp` là Structural Price (Bid basis từ OHLC).
     - Long: $\text{entry} = \text{Open}_{N+1} + \text{spread}$, $\text{sl} = \text{signal\_sl}$, $\text{tp} = \text{signal\_tp}$. Geometry: $\text{sl} < \text{entry} < \text{tp}$.
     - Short: $\text{entry} = \text{Open}_{N+1}$, $\text{sl} = \text{signal\_sl} + \text{spread}$, $\text{tp} = \text{signal\_tp} + \text{spread}$. Geometry: $\text{tp} < \text{entry} < \text{sl}$.
     - Nếu mở gap xuyên SL/TP hoặc sai geometry $\to$ Hủy lệnh, lưu vào `execution_events` (`geometry_violation_at_fill`).
     - Cash-basis RR sau spread & commission:
       $\text{risk\_cash} = |\text{entry} - \text{sl}| \times \text{lot} \times \text{contract} + 2 \times \text{comm}$
       $\text{reward\_cash} = |\text{tp} - \text{entry}| \times \text{lot} \times \text{contract} - 2 \times \text{comm}$
       Nếu $\text{reward\_cash} \le 0$ hoặc $\text{risk\_cash} \le 0$ hoặc $RR_{\text{effective}} < 1.5 \to$ Hủy lệnh (`insufficient_rr_at_fill`).
  10. **Comprehensive Verification & Independent QC**:
      - Khóa 18 reason codes chuẩn hóa. Buffer giá: `sl_buffer_price = 0.20` USD (20 points). Cooldown 3 bars chỉ tính sau khi mở vị thế thực tế.
      - Schema `execution_events` chuẩn hóa. Benchmark 10.000 nến (<1.5s provisional) trên máy tham chiếu chuẩn. Gate E Independent Read-Only QC.
- **Tài liệu tham chiếu**: `T53_WAVE1_SEMANTICS.md` (v5).

## ADR 17: Stable ID Component Grammar Contract (Hướng A) & Deep Immutability Snapshots (T53.1)
- **Ngày**: 2026-09-09
- **Trạng thái**: ACCEPTED
- **Bối cảnh**:
  - T53.1 yêu cầu sinh ID tất định, phân tách đảo ngược được (reversible), không đụng độ (non-collision) giữa các thành phần composite (`setup_id`, `evidence_id`, `cluster_id`, `decision_id`).
  - Cần bảo đảm tính bất biến sâu (deep immutability) tuyệt đối cho `StrategyContext`, ngăn ngừa leakage khi sửa đổi object nguồn bên ngoài (`SessionDecision`, `BiasState`), đồng thời tránh sửa trực tiếp `smc/models.py` gây vỡ T52.
  - Loại bỏ hoàn toàn ép kiểu ngầm (`int()`, `float()`, `bool()`) trong `from_dict`, fail-fast với `KeyError` khi thiếu required fields và `StrictModelTypeError` / `ValueError` khi sai kiểu.
- **Quyết định**:
  1. **Component Grammar Contract (Hướng A)**:
     - Các base component (`strategy_id`, `direction`, `bar_index`, `leg_id`, `zone_id`, `sub_key`) bắt buộc tuân thủ regex `^[A-Za-z0-9_.-]+$`. Cấm tuyệt đối delimiter `:` và khoảng trắng đầu/cuối.
     - `cluster_id` là trailing composite component, cho phép chứa `:` phân tách các segment con hợp lệ (không chứa segment rỗng).
     - `direction` chỉ nhận `"BUY"` hoặc `"SELL"`. `action` chỉ nhận `"SELECT"` hoặc `"NO_TRADE"`.
     - `make_decision_id` khi `NO_TRADE` định dạng cố định: `f"sel:{bar_index}:NO_TRADE:none"`. Cấm strategy ID thực sự khi `NO_TRADE`.
  2. **Deep Immutability Snapshot DTOs**:
     - Bổ sung `SessionDecisionSnapshot` và `BiasStateSnapshot` (`@dataclass(frozen=True)`).
     - `StrategyContext.__post_init__` tự động ép các object nguồn sang snapshot tương ứng qua `.from_source()`.
     - `_freeze()` đệ quy đóng băng `meta` thành `MappingProxyType`, set thành sorted tuple, list thành tuple. Sửa object nguồn bên ngoài không ảnh hưởng đến context.
  3. **JSON-Safe Serialization & Fail-Fast Type Checking**:
     - `to_dict()` gọi `_unfreeze()` chuẩn hóa NumPy scalars (`np.float32`, `np.int64`, `np.bool_`) về Python primitive, giữ nguyên boolean (tránh cạm bẫy `isinstance(True, int)`), chuyển timestamp sang ISO format, sort set xác định.
     - `_freeze()` fail-fast với `TypeError` khi gặp object mutable không hỗ trợ hoặc non-finite float (`NaN`/`Inf`).
  4. **Strict `from_dict` & Constructor Validation**:
     - Không ép kiểu trước khi gọi constructor; truyền trực tiếp raw values.
     - Thiếu required field bắt buộc raise `KeyError(f"Missing required field '{field}'...")`.
     - Constructor validate nghiêm ngặt: reject `bool` trong numeric/int/float, reject string/int trong bool, reject string trong float, reject `None` trong required string/timestamp.
- **Tài liệu tham chiếu**: `SMC_MULTI_STRATEGY_IMPLEMENTATION_PLAN.md`, `tests/test_smc_engine_models.py`.

## 2026-09-10 - ADR 18: Performance Gate Trung Gian Cho T53.2 [ACCEPTED]
- **Trạng thái**: ACCEPTED — người dùng phê duyệt trực tiếp ngày 2026-09-10.
- **Bối cảnh**: Benchmark hiện tại của `StrategyContextBuilder` khoảng 11.6s/10.000 bars; tracker nền khoảng 4.56s. Mục tiêu provisional `< 1.5s` thấp hơn cả chi phí tracker nền và đòi hỏi tối ưu kiến trúc sâu, không phù hợp làm gate ngắn hạn cho T53.2.
- **Quyết định**:
  1. Đặt gate trung gian bắt buộc cho T53.2 là trung bình 3 lần chạy `< 7.0s / 10.000 bars`, sau 1 warm-up, giữ nguyên dữ liệu/cấu hình benchmark chuẩn.
  2. T53.2 chỉ được chuyển `done` và mở T53.3 khi benchmark nguyên bản đạt gate `< 7.0s` và toàn bộ correctness/no-lookahead/parity tests vẫn PASS.
  3. Giữ `< 1.5s / 10.000 bars` là mục tiêu tối ưu dài hạn, không xóa khỏi lịch sử quyết định nhưng không dùng làm gate chặn T53.2 trong giai đoạn hiện tại.
  4. Cấm đạt PASS bằng cách giảm workload, đổi dữ liệu benchmark, skip/xfail, mock thời gian hoặc nới gate vượt 7.0s mà không có ADR mới.
- **Lý do**: Tạo mục tiêu gần, đo được và vẫn buộc giảm đáng kể thời gian từ khoảng 11.6s; đồng thời không làm thay đổi logic giao dịch, zero-lookahead hoặc API hiện tại.

## 2026-09-10 - ADR 19: Chấp Nhận Baseline Real-Time Hiện Tại và Hoãn Tối Ưu T53.PERF [ACCEPTED]
- **Trạng thái**: ACCEPTED — người dùng xác nhận hệ thống real-time chỉ chạy số lượng mã hạn chế và yêu cầu giữ nguyên hiện trạng.
- **Bối cảnh**: `StrategyContextBuilder` xử lý khoảng 11–12s/10.000 closed bars, tương đương khoảng 1.1–1.2ms/bar. Mức này đủ dư địa cho closed-bar real-time với ít symbol nhưng chưa đạt mục tiêu tối ưu ADR 18 `< 7.0s`.
- **Quyết định**:
  1. Chấp nhận baseline hiện tại để hoàn tất T53.2 và không chặn T53.3.
  2. Không đặt hard ceiling tùy ý trong correctness suite vì benchmark pure Python có độ nhiễu theo tải máy. Chuyển test hiệu năng thành opt-in qua `RUN_SMC_PERFORMANCE_TESTS=1`.
  3. Giữ assertion/probe `< 7.0s` làm chỉ báo technical debt; mục tiêu dài hạn `< 1.5s` vẫn được lưu.
  4. Tạo backlog `T53.PERF`; ưu tiên lại khi mở rộng nhiều symbol/timeframe, chạy tick-level hoặc cần tăng tốc backtest/walk-forward.
  5. Không thay đổi logic giao dịch, lifecycle, zero-lookahead, batch/incremental parity hoặc dữ liệu/workload benchmark.
- **Hệ quả**: Default regression suite báo rõ benchmark là SKIP; khi chạy opt-in, assertion/probe vẫn FAIL/exit nonzero nếu chưa đạt 7s và không được diễn giải là lỗi correctness.

## 2026-09-10 - ADR 20: Strategy Template Protocol & Deterministic Registry Architecture (T53.3) [ACCEPTED]
- **Trạng thái**: ACCEPTED
- **Bối cảnh**: T53.3 cần thiết kế contract cho strategy templates (S01, S05, S09) và deterministic registry. Cần đảm bảo an toàn lifecycle cho strategy stateful, chống evaluate hai lần trên cùng closed bar, chống chia sẻ mutable strategy state giữa các run, và loại bỏ multiple inheritance trong exception hierarchy.
- **Quyết định**:
  1. `StrategyTemplate(Protocol)` với `@runtime_checkable` định nghĩa `strategy_id: str`, `profile: StrategyProfile`, `evaluate(context)` và `reset()`.
  2. Public helper `validate_strategy_id(sid)` chuẩn hóa component grammar `^[A-Za-z0-9_.-]+$` độc lập trong `protocol.py`.
  3. `StrategyRegistry` sở hữu dispatch lifecycle: bọc internal mapping bằng `MappingProxyType`, chỉ cung cấp duy nhất một public execution API `evaluate_enabled(context) -> Mapping[str, tuple[CandidateSetup, ...]]` cache last bar để idempotent retry, và `reset_all()` phục hồi state/poisoned.
  4. Không cung cấp `with_config()`, không lộ mutable strategy instances (chỉ cung cấp `get_profile`), không cho phép dynamic registration.
  5. Đơn giản hóa exception hierarchy thành dependency leaf `smc/engine/errors.py` kế thừa đơn từ `StrategyRegistryError`.
- **Hệ quả**: Đạt exactly-once evaluation per unique bar, bảo toàn immutability và determinism cho toàn bộ multi-strategy selection pipeline.

## 2026-09-10 - ADR 21: Implementation Contract cho T53.5 S05 BOS → OB First Retest [ACCEPTED]
- **Trạng thái**: ACCEPTED
- **Bối cảnh**: ADR 16 đã khóa semantics cấp Wave 1, nhưng S05 còn cần các quyết định đủ cụ thể để code deterministic và tránh stale OB, duplicate BOS/OB ownership, lookahead và lệch parity.
- **Quyết định**:
  1. S05 bắt buộc HTF bias aligned cả lúc ingest BOS và mọi bar narrative còn active; neutral/opposed làm narrative terminal, không hồi sinh.
  2. BOS mặc định phải là close-break có displacement; `require_displacement=True` nhưng cho phép config nghiên cứu tắt.
  3. OB linkage ưu tiên exact `source_event_index == bos.index`; chỉ fallback bằng non-null equal `structure_leg_id` khi không có exact match. Global ownership ưu tiên exact pair rồi BOS mới hơn.
  4. First retest chỉ hợp lệ sau tracker update khi đủ bốn trường: `mitigated_at == N`, `retest_count == 1`, `valid=True`, `invalidated_at is None`; strategy refresh OB snapshot mỗi bar và không dùng clone cũ.
  5. Expiry S05 là inclusive tại `effective_created_at + 25`; opposite BOS hoặc CHoCH trong interval inclusive được xử lý trước emission.
  6. Entry mặc định proximal, tùy chọn CE50; SL tại OB distal ±0.20; target nearest opposing pool, fallback fixed 2R. Không suy diễn `broken_swing_price` thành BOS impulse target trong Wave 1.
  7. Quality mặc định nhận `base`; quality chỉ là filter trong S05, scoring 60/75/90 thuộc T53.8.
  8. T53.5 chỉ phát `CandidateSetup`; fill/cash-RR/cooldown thuộc T53.9, regime/reason-code/dedup liên-strategy thuộc T53.7–T53.8.
- **Tài liệu chi tiết**: `T53_5_S05_BOS_OB_RETEST_IMPLEMENTATION_PLAN.md`.

## 2026-09-10 - ADR 22: Implementation Contract cho T53.6 S09 ICT Silver Bullet [ACCEPTED]
- **Trạng thái**: ACCEPTED (Người dùng đã chính thức phê duyệt kế hoạch triển khai tại T53.6).
- **Bối cảnh**: ADR 16 đã khóa semantics S09 cấp Wave 1; implementation cần khóa thêm WindowKey, event-time basis, DST, grace, one-per-window và cross-strategy cluster identity.
- **Quyết định**:
  1. Ba window canonical Monday–Friday theo `America/New_York`: 03:00–04:00, 10:00–11:00, 14:00–15:00; event start inclusive/end exclusive.
  2. Sweep/MSS/FVG phải được xác nhận trong cùng WindowKey bằng bar-close time. FVG historical dùng bounded `bar_index -> bar_close_time` cache, cấm suy timestamp bằng bar arithmetic.
  3. Retest chỉ sau MSS và hợp lệ khi `bar_close_time <= window_end + 15 minutes`; exact grace pass, sau grace reject.
  4. Aligned/neutral HTF bias pass, opposed/missing fail closed; bias departure terminal trong window.
  5. Tối đa một emitted setup cho mỗi WindowKey; chỉ consume window sau khi build Candidate thành công.
  6. Entry/SL/target/RR theo ADR 16; Candidate chỉ hợp lệ signal bar (`expiry_bar=N`), fill N+1 thuộc T53.9.
  7. S09 dùng cùng cluster convention với S01 cho cùng MSS/FVG để T53.7 deduplicate, setup ID vẫn khác theo strategy ID.
  8. State terminal/window/cluster/bar-close cache phải atomic và bounded theo grace/context capacities.
- **Tài liệu chi tiết**: `T53_6_S09_ICT_SILVER_BULLET_IMPLEMENTATION_PLAN.md`.

## 2026-09-10 - ADR 23: Ranh Giới Regime, Gate, Dedup và Conflict T53.7 [ACCEPTED]
- **Trạng thái**: ACCEPTED — người dùng phê duyệt trực tiếp kế hoạch triển khai ngày 2026-09-10.
- **Bối cảnh**: S01, S05 và S09 đã phát CandidateSetup; cần chuẩn hóa regime, hard gate và opportunity identity trước scoring/selection nhưng không được chọn owner bằng điểm chưa tồn tại.
- **Đề xuất**:
  1. Regime V1 dùng trailing 20 close, 100 ATR14 finite, Kaufman ER, empirical mid-rank ATR percentile, swing BOS và recent sweep; cây priority là trend → volatile reversal → ranging → uncertain.
  2. Eligibility Gate dùng bảng 30 ô và HTF policy ADR 16; hard failure aggregate reason canonical. `uncertain` vẫn eligible với regime score 30, không fail closed chỉ vì warm-up.
  3. Dedup ưu tiên `(direction, evidence_cluster_id)` hoặc exact same non-null structure-leg + zone evidence; chỉ chung sweep/BOS không đủ merge.
  4. S01 stale sweep hard cap 20; S09 dùng WindowKey/grace riêng để không bị loại sai ở cuối Silver Bullet grace; S05 không yêu cầu sweep.
  5. T53.7 chỉ tạo DirectionConflict và giữ cluster members. Primary owner, component/total score, score gap 15 và final NO_TRADE/SELECT thuộc T53.8.
- **Tài liệu chi tiết**: `T53_7_REGIME_GATE_DEDUP_CONFLICT_IMPLEMENTATION_PLAN.md`.

## 2026-09-11 - ADR 24: Deterministic Selector, Ownership và Audit Telemetry T53.8 [ACCEPTED]
- **Trạng thái**: ACCEPTED — Gate A đã phê duyệt, đang triển khai theo plan.
- **Bối cảnh**: T53.7 đã tạo `ConfluenceBatch` gồm eligible clusters và direction conflict nhưng cố ý chưa chấm đủ component, chưa chọn owner/winner và chưa tạo telemetry quyết định.
- **Đề xuất**:
  1. Tổng điểm V1: `round(0.25*regime + 0.35*setup + 0.25*context + 0.15*execution_proxy, 2)`; minimum total 60.0.
  2. `execution_proxy` chỉ dùng `CandidateSetup.planned_rr` tại closed bar N. Actual fill, spread, commission, cash-RR và cooldown vẫn thuộc T53.9.
  3. FVG size fallback dùng `context.atr14` tại signal bar N và ghi rõ basis; không truy hồi full-series ATR hoặc dùng future data.
  4. Cluster score bằng score member cao nhất; member đó là primary owner. Không cộng/tính trung bình theo số strategy, tránh biến overlap thành nhiều phiếu.
  5. Khi có hai hướng, so best BUY/best SELL trước minimum-score filter: gap `<15.0` trả `NO_TRADE/conflicting_direction`, exact `15.0` chọn phía thắng; winner sau đó phải đạt score `>=60.0`.
  6. Tie-break canonical: total, setup, context, execution, regime, planned RR giảm dần; strategy ID, setup ID và cluster ID tăng dần.
  7. Telemetry là immutable per-bar audit và pure aggregate; không dùng rolling PnL/win-rate/LLM để tác động selection trong Wave 1.
- **Tài liệu chi tiết**: `T53_8_SELECTOR_TELEMETRY_IMPLEMENTATION_PLAN.md`.

## 2026-09-11 - ADR 25: Bar-Phased Backtest Integration và Cooldown-After-Fill T53.9 [ACCEPTED]
- **Trạng thái**: ACCEPTED — người dùng đã trực tiếp phê duyệt Gate A sau khi xem contract và kết quả independent QC T53.9.1.
- **Bối cảnh**: T53.8 tạo quyết định tại closed bar N nhưng chưa có execution. Cooldown phụ thuộc successful fill tại Open N+1, nên không thể precompute toàn bộ final signals độc lập với trạng thái execution mà vẫn bảo đảm đúng semantics.
- **Quyết định**:
  1. Coordinator chạy từng bar theo thứ tự Open execution → intrabar SL/TP → Close analysis/selection; decision Close N chỉ có thể fill ở Open N+1.
  2. Wave 1 dùng market-at-next-open; `CandidateSetup.entry_price` chỉ là planned/audit price, không phải limit order.
  3. BUY dùng entry Open+spread và structural SL/TP Bid; SELL dùng entry Open, SL/TP cộng spread và trigger theo Ask. Gap làm sai geometry bị reject.
  4. Cash RR tính sau spread và round-trip commission; exact profile minimum pass, invalid/non-positive hoặc dưới minimum reject.
  5. Tối đa một position. Same-direction signal bị skip; opposite signal chỉ đóng/reverse position cũ sau khi lệnh mới pass fill gate.
  6. Cooldown key `(primary_strategy_id, direction)` bắt đầu từ successful fill, không bắt đầu từ SELECT/reject/skip và không khóa supporting strategy.
  7. HTF structure là explicit as-of timeline; không gán same-timeframe event thành HTF. Proposed API mapping: M1→M15, M5→H1, M15→H1.
  8. Không sửa contract `SelectionDecision.execution_payload={}` của T53.8; adapter tạo immutable execution intent riêng.
  9. Giữ 18 pipeline reason codes; execution skip/cancel dùng namespace riêng. Legacy strategy IDs và output fields hiện hữu phải tương thích ngược.
  10. Timing benchmark theo ADR 19 là opt-in/evidence, không tự đặt hard gate mới. Gate E cần independent read-only QC không còn P0/P1.
- **Tài liệu chi tiết**: `T53_9_BACKTEST_INTEGRATION_IMPLEMENTATION_PLAN.md`.

## 2026-09-12 - ADR 26: Gate E Approval & Wave 1 Backtest Integration Closure (T53.9) [ACCEPTED]
- **Trạng thái**: ACCEPTED — Gate E đạt đầy đủ tiêu chuẩn độc lập; milestone T53.9 chính thức hoàn tất.
## 2026-09-12 - ADR 27: Research Protocol V1 & Dual-Machine Data Quality Gate (T54.0) [ACCEPTED]
- **Trạng thái**: ACCEPTED — Data Quality Gate đạt 100% tiêu chí chấp nhận; Protocol V1 chính thức được khóa.
- **Bối cảnh**: Để đảm bảo tính khoa học, khả năng tái lập và chống overfit trong milestone nghiên cứu T54, cần xác định tập dữ liệu chuẩn, kiểm toán toàn diện tính toàn vẹn nến và thiết lập cơ chế kiểm chứng độc lập 2 máy (Máy 1: Data Runner / Profiler; Máy 2: Independent Read-Only QC).
- **Quyết định**:
  1. **Khóa Canonical Research Dataset [2022-01-01, 2026-08-31]**: Khảo sát `data/XAUUSD.db` phát hiện 1,325 nến đầu (2016–2021) là nến ngày D1 lưu nhầm với timestamp `00:00:00`. Do đó, loại bỏ đoạn 2016–2021 và khóa cứng giai đoạn từ `2022-01-01 00:00:00` đến `2026-08-31 23:59:59` UTC (56 tháng, đúng 1,646,963 nến M1 chuẩn và liên tục).
  2. **Phân chia dữ liệu In-Sample / Validation / Out-of-Sample (60/20/20)**:
     - In-Sample (IS - 58.9% ~ 60%): `2022-01-01` $\to$ `2024-09-30` (33 tháng, 970,516 nến M1, 64,904 nến M15, 16,244 nến H1) — dành cho nghiên cứu baseline và kiểm chứng giả thuyết.
     - Validation (19.6% ~ 20%): `2024-10-01` $\to$ `2025-08-31` (11 tháng, 322,932 nến M1, 21,600 nến M15, 5,406 nến H1) — dành cho phân tích độ nhạy chi phí, regime matrix và selector calibration.
     - Out-of-Sample (OOS - 21.5% ~ 20%): `2025-09-01` $\to$ `2026-08-31` (12 tháng, 353,515 nến M1, 23,626 nến M15, 5,911 nến H1) — bị khóa mù tuyệt đối, chỉ mở tại T54.5/T54.8.
  3. **Mô hình chi phí thực nghiệm**: Khảo sát cột `spread` trong DB thấy 86.1% là 0 do giới hạn dữ liệu sàn cũ; do đó khóa mô hình chi phí tổng hợp: Spread chuẩn = 20.0 points ($0.20/oz), Commission = 5.0 USD/lot round-trip.
  4. **Cơ chế 2 máy hỗ trợ (Dual-Machine Paradigm)**:
     - Máy 1 xuất `dataset_manifest.json`, `data_quality_report.json` và `data_quality_report.md`.
     - Máy 2 mở kết nối riêng `sqlite_uri_readonly` (`mode=ro`), độc lập tính SHA256 checksum M1 và M15, kiểm toán 20 nến ngẫu nhiên và đối chiếu chéo 1-1 với manifest Máy 1.
     - Kết quả: Khớp 100% từng bit checksum (`e53ca000c7bda2f7...` cho M1, `4553fba513f86bc0...` cho M15), 0 nến trùng lặp, 0 lỗi đơn điệu, 0 lỗi hình học OHLC.
  5. **Quy tắc vận hành**: Hai máy dùng chung commit code; không dùng output của máy kia làm input độc nhất; mỗi run có `run_id`; tuyệt đối không tối ưu hóa tham số trong các pha baseline T54.1 - T54.4.
- **Tài liệu chi tiết**: `research/protocol_v1.json`, `research/dataset_manifest.json`, `research/data_quality_report.md`, `research/qc_verification_report.json`.

## 2026-09-12 - ADR 28: Planned SL/TP Wiring & Fill Geometry Guard for Legacy SMC Strategy (T54.1.x) [ACCEPTED]
- **Trạng thái**: ACCEPTED — Đã triển khai, nghiệm thu đầy đủ unit tests, full regression và empirical validation.
- **Bối cảnh**: Chiến lược `smc_confluence` trước đây tính đúng Planned SL/TP dựa trên Order Block / FVG anchor swing levels và `rr_ratio`, nhưng các mức này bị rơi rụng trước khi chuyển tới `ExecutionKernel`, khiến backtest legacy rơi vào fallback cố định 200 SL / 400 TP points ($2.0 / $4.0), làm mất tác dụng của tham số `rr_ratio` và sai lệch bản chất chiến lược.
- **Quyết định**:
  1. **Atomic Binding tại Bar $N$**: `smc/strategy.py` tính toán và ghi nhận đồng thời `planned_entry_price`, `planned_stop_loss`, `planned_take_profit`, `planned_rr` tại bar $N$ cùng với signal, độc lập cho cả limit fill và market order. Kiểm tra hình học setup tại bar $N$ trước khi ghi nhận.
  2. **Zero-Lookahead Execution**: Toàn bộ tín hiệu và planned levels sinh tại bar $N$ chỉ được đọc tại $i-1$ và khớp lệnh tại Open nến $i$ ($N+1$). Tuyệt đối không đọc dữ liệu nến tương lai.
  3. **Relative Geometry Guard tại Actual Fill**: Vì giá khớp thực tế là Open nến $N+1$ (hoặc Open + Spread đối với BUY), giá khớp có thể bị gap. Engine kiểm tra lại hình học tại `actual_entry`:
     - BUY: `planned_sl < actual_entry < planned_tp`
     - SELL: `planned_tp < actual_entry < planned_sl`
     Nếu vi phạm do gap giá, engine áp dụng chính sách **Fail-Closed** an toàn: hủy lệnh, không dịch giá, ghi nhận bộ đếm `rejected_invalid_geometry`.
  4. **Cách Ly Telemetry & Bảo Toàn Parity**:
     - Với lệnh SMC: Bổ sung 6 trường telemetry (`planned_entry_price`, `planned_stop_loss`, `planned_take_profit`, `planned_rr`, `actual_entry_price`, `sl_tp_source`) vào `trade_record`.
     - Với các chiến lược non-SMC legacy (SMA, RSI, MACD, Donchian): Giữ nguyên `metadata={}` rỗng, bảo đảm 100% byte-for-byte schema parity (11 trường trade gốc) và không gây ô nhiễm schema.
## 2026-09-12 - ADR 29: Canonical HTF-to-LTF Timeline Mapping & Zero-Lookahead Event Alignment (T54.1.10 - T54.1.15) [ACCEPTED]
- **Trạng thái**: ACCEPTED — Đã khắc phục triệt để lỗi lệch index M15/H1, bảo đảm zero-lookahead và hoàn tất chạy 10.000 nến IS cho 5 chiến lược.
- **Bối cảnh**: Khi chạy backtest 10.000 nến M15 cho bộ 3 chiến lược Wave 1 (`smc_s01`, `smc_s05`, `smc_s09`) và coordinator `smc_wave1`, phát sinh lỗi `source_event_index > current_bar` hoặc future leak khiến backtest không thể hoàn tất hoặc ra kết quả không tin cậy. Nguyên nhân do HTF events (H1) bị truyền với H1 index vào coordinator đang chạy trên không gian nến M15, kết hợp với lỗi slicing `tail(limit)` trong `DataFeed._query_and_resample` làm lệch thời gian bắt đầu giữa M15 và H1.
- **Quyết định**:
  1. **Chuẩn Hóa Slicing DataFeed khi có `start_time`**:
     - Trong `engine/data_feed.py::_query_and_resample`: Khi có `start_time`, luôn ưu tiên cắt `resampled.head(limit)` thay vì `tail(limit)`. Điều này bảo đảm dữ liệu resample (M15, H1) luôn bắt đầu đồng nhất tại timestamp đầu tiên thỏa mãn điều kiện lọc.
  2. **Định Nghĩa Canonical Mapping H1 → M15**:
     - Thời điểm khả dụng của event cấu trúc H1 là khi nến H1 đóng: `event_available_time = event.time + 1h` (với nến H1 lưu theo open time).
     - `canonical_m15_index` là index của bar M15 đầu tiên có `close_time >= event_available_time`, tìm kiếm hiệu năng cao qua `searchsorted` trên DatetimeIndex của thời điểm đóng nến M15.
     - Ràng buộc nghiêm ngặt: $0 \le \text{canonical\_m15\_index} < N_{\text{bars}}$. Loại bỏ mọi event đóng ngoài khoảng execution bars.
     - Tỷ số index canonical M15 / H1 đạt xấp xỉ $3.99\times \approx 4\times$.
  3. **Zero-Lookahead & Prefix Invariance**:
     - Event chỉ được phép emit tới context builder tại bar M15 có $N \ge \text{canonical\_m15\_index}$.
     - Chạy prefix 5.000 nến và full 10.000 nến cho kết quả tập hợp event 5.000 nến đầu trùng khớp 100% từng event ($52 == 52$).
  4. **Fail-Closed Payload Validation**:
     - `parse_htf_event_payload` từ chối dứt khoát `index < 0`, timestamp thiếu timezone awareness hoặc giá trị non-finite.
     - `BacktestEngine` chấp nhận linh hoạt cả runner payload dict (`payload.get("events", [])`) lẫn danh sách `StructureEvent`.
  5. **Phân Loại Nguyên Nhân Kết Quả Wave 1**:
     - Trên 10.000 nến In-Sample M15, cả 4 mode Wave 1 đều hoàn tất không lỗi kỹ thuật nhưng có 0 lệnh giao dịch.
     - Phân tích funnel 12 tầng chỉ ra: Tầng 1–6 hoạt động đầy đủ (10.000 bars, 113 HTF events, 9.941 bars có bias, 476.347 LTF structures, 325.213 FVGs, 230.500 OBs), nhưng Tầng 7 (`candidate setups`) về 0.
     - Phân loại chuẩn: **`no_candidate`** (thuộc tính logic chiến lược, hạ tầng timeline đạt chuẩn 100%).
- **Tài liệu chi tiết**: `walkthrough.md`, `research/runs/t54_1_baseline_summary_10000.json`, `tests/test_htf_timeline_canonical.py`.

