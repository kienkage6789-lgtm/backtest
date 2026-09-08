# Walkthrough: T49 – Sửa Lỗi Vẽ Vô Hạn Tương Lai & Tự Động Đồng Bộ Tọa Độ Khi Kéo Trục Giá/Thời Gian

Đã triển khai hoàn tất task **T49: Future Infinite Drawing & Scale Drag Sync** cho hệ thống vẽ kỹ thuật Web Backtest. Khắc phục triệt để lỗi không vẽ được vào khoảng trắng tương lai và lỗi bản vẽ không tự động cập nhật vị trí khi kéo trục giá / trục thời gian mà phải chờ lăn chuột.

---

## 1. Tóm Tắt Khắc Phục Lỗi Hệ Thống Vẽ (Task T49)

### A. Lỗi 1: Không thể vẽ vô hạn về phía tương lai
- **Nguyên nhân gốc**: Code cũ phụ thuộc vào `timeToCoordinate(lastCandle.time)`. Khi người dùng cuộn biểu đồ sang phải để làm việc trong vùng tương lai, cây nến cuối bị cuộn trôi ra ngoài màn hình bên trái khiến `timeToCoordinate` trả về `null`, làm gãy toàn bộ phép tính chuyển đổi tọa độ; ngoài ra `rightOffset` bị giới hạn cứng và interval nến tính thô sơ 2 nến cuối bị sai lệch khi gặp gap cuối tuần.
- **Giải pháp**:
  - Chuyển đổi 2 chiều bằng `timeScale.coordinateToLogical(x)` và `timeScale.logicalToCoordinate(targetLogical)`. Hệ tọa độ logical index là trục số liên tục nguyên vẹn, hoạt động hoàn hảo ngay cả khi cây nến cuối nằm ngoài màn hình.
  - Tính interval nến động bằng trung vị (`median`) của tối đa 30 nến gần nhất, tự động lọc qua gap cuối tuần/ngày lễ và fallback chuẩn theo timeframe hiện tại (không hard-code 60s).
  - Tự động nới rộng `rightOffset` động qua `ensureFutureOffset(targetLogical)`.
  - Viewport Bounding-Box Culling sử dụng `getVisibleLogicalRange()`, đảm bảo nét vẽ nằm hoàn toàn trong tương lai vẫn hiển thị trọn vẹn.
  - **Tuyệt đối không thêm bất kỳ nến giả (dummy candle) nào vào mảng OHLC**.

### B. Lỗi 2: Bản vẽ không tự cập nhật khi kéo trục giá / trục thời gian
- **Nguyên nhân gốc**: Lightweight Charts không phát ra event `visibleLogicalRangeChange` khi chỉ kéo giãn trục giá (Price Scale Drag) hoặc chỉ kéo trục thời gian (Time Scale Drag). Người dùng phải lăn chuột (`wheel`) mới kích hoạt listener. Thêm vào đó, việc dùng `attachPrimitive()` có nguy cơ tạo render loop vô hạn trong chu kỳ render của Lightweight Charts.
- **Giải pháp**:
  - Bắt trọn tương tác pointer trên container (`pointerdown` trên container, `pointermove` và `pointerup` trên window) kết hợp `wheel`, `dblclick` và `resize`. Hoạt động trơn tru trong mọi chế độ (Select mode / Drawing mode).
  - Thống nhất pipeline hiển thị qua `window.requestAnimationFrame` với cờ `renderPending` và lưu `this.rafId`. Loại bỏ hoàn toàn tình trạng render loop và bảo đảm hiệu năng 60 FPS mượt mà.
  - Dọn dẹp sạch sẽ listener và hủy rAF khi `destroy()`.

---

# Walkthrough: T47 – Collapsible Strategy & Results Drawer

---

## 1. Các Thay Đổi Trọng Tâm Đã Thực Hiện

### A. Giao diện & Điều khiển (HTML & UX)
- **2 Nút Toggle Độc Lập trên Header**: Bổ sung `#btn-toggle-strategy` (`⚙ Cấu hình`) và `#btn-toggle-results` (`📊 Báo cáo`) tại `.header-right` với trạng thái active trực quan và các thuộc tính WAI-ARIA (`aria-expanded`, `aria-controls="side-panel"`).
- **Drawer Thống Nhất 2 Tab**: Chuyển đổi `#side-panel` thành drawer bên phải với mặc định có class `collapsed`. Tích hợp 2 tab:
  1. `⚙ Cấu hình chiến lược` (`#tab-strategy`)
  2. `📊 Kết quả & Báo cáo` (`#tab-results`)
- **Nút Đóng Nhanh & Phím Tắt**: Thêm nút `✕` (`#btn-close-drawer`) ngay trên thanh header tab của drawer và hỗ trợ phím tắt `Escape` (bảo vệ chống đóng nhầm khi Property Dialog Overlay hoặc Context Menu đang mở).
- **Mobile Backdrop Isolation**: Bổ sung `#drawer-backdrop` cho chế độ màn hình nhỏ (<768px), đồng thời cấu hình CSS `display: none !important` trên desktop/tablet để tuyệt đối không che phủ hay chặn tương tác.

### B. Hiệu Ứng Trượt & Responsive Stacking Context (CSS)
- **Smooth Transition**: Sử dụng `transition: width 0.25s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.2s ease` tạo cảm giác trượt mượt mà.
- **Trạng Thái Thu Gọn Kháng Lỗi**: `.side-panel.collapsed` có `width: 0 !important; border-left: none !important; opacity: 0; visibility: hidden; pointer-events: none;`, giải phóng 100% diện tích cho biểu đồ (chiều rộng chart đạt > 1870px trên màn hình 1920x1080).
- **Stacking Context Defense-in-Depth**:
  - **Desktop / Tablet ($\ge 768\text{px}$)**: `.side-panel` có `position: relative; z-index: 1001;`. `.drawer-backdrop` được ẩn hoàn toàn (`display: none !important; pointer-events: none !important;`), đảm bảo không có bất kỳ phần tử backdrop nào che phủ hay chặn click/scroll vào tab, form inputs hay nút điều khiển.
  - **Mobile ($< 768\text{px}$)**: `.side-panel` có `position: fixed; z-index: 1050;` (cao hơn hoàn toàn so với backdrop `z-index: 990`). Khi mở drawer, backdrop hiển thị làm mờ nền xung quanh và click vào nền sẽ đóng drawer.

### C. Logic Điều Khiển, Persistence & Auto-Resize (JavaScript)
- **Tự Động Resize Canvas**: Bổ sung phương thức `handleResize()` cho `TradingChart` và cơ chế resize 2 tầng (`handleResize()` tức thì + sau 280ms transition) giúp Lightweight Charts canvas co giãn hoàn hảo, không giật lag hay vỡ bố cục.
- **Chỉ Bật Backdrop Trên Mobile**: Logic `openDrawer()` và sự kiện `window.onresize` sử dụng điều kiện `matchMedia('(max-width: 767px)').matches` để chỉ hiển thị backdrop trên thiết bị di động, tự động thu hồi backdrop khi xoay màn hình sang desktop.
- **Lưu Trữ Bền Vững (LocalStorage)**: Lưu cấu hình qua key `backtest:ui:drawer` dạng `{ isOpen: boolean, activeTab: string }`. Khôi phục chính xác tab khi reload trang, đồng thời có fallback chống crash khi dữ liệu corrupt hoặc môi trường private browsing.
- **Bảo Toàn Form Data**: Toàn bộ input tham số chiến lược (`#strategy-select`, `#initial-capital`, `#lot-size`, `#stop-loss`, `#take-profit`, v.v.) được giữ nguyên vẹn trong DOM kể cả khi đóng drawer.
- **Tích Hợp Luồng Backtest**: Khi backtest chạy xong, hệ thống tự động mở drawer và chuyển sang tab `tab-results` để hiển thị ngay báo cáo hiệu suất và đồ thị vốn; nếu validation lỗi, tự động mở `tab-strategy`.

---

## 2. Minh Chứng Trực Quan Qua Ảnh Chụp Headless Chrome (Visual Evidence)

Tất cả các ảnh chụp màn hình được tạo tự động bởi Headless Chrome DevTools Protocol (`verify_drawer_qa.js`) trên môi trường thật:

````carousel
![01. Trạng thái mặc định khi mở trang (Drawer đóng, Chart chiếm 100% diện tích)](C:/Users/Admin/.gemini/antigravity/brain/b650687b-9f2f-46ee-817c-8a7abf455532/drawer_01_default_closed.png)
<!-- slide -->
![02. Mở Cấu hình chiến lược bằng nút Header (#btn-toggle-strategy, Backdrop ẩn trên Desktop)](C:/Users/Admin/.gemini/antigravity/brain/b650687b-9f2f-46ee-817c-8a7abf455532/drawer_02_strategy_opened.png)
<!-- slide -->
![03. Chuyển sang tab Báo cáo bằng nút Header (#btn-toggle-results)](C:/Users/Admin/.gemini/antigravity/brain/b650687b-9f2f-46ee-817c-8a7abf455532/drawer_03_results_opened.png)
<!-- slide -->
![04. Chuyển tab trực tiếp bên trong Drawer qua .panel-tab](C:/Users/Admin/.gemini/antigravity/brain/b650687b-9f2f-46ee-817c-8a7abf455532/drawer_04_tab_switched.png)
<!-- slide -->
![05. Đóng Drawer bằng nút ✕ (#btn-close-drawer)](C:/Users/Admin/.gemini/antigravity/brain/b650687b-9f2f-46ee-817c-8a7abf455532/drawer_05_closed_via_btn.png)
<!-- slide -->
![06. Đóng Drawer bằng phím tắt Escape](C:/Users/Admin/.gemini/antigravity/brain/b650687b-9f2f-46ee-817c-8a7abf455532/drawer_06_closed_via_escape.png)
<!-- slide -->
![07. Chạy Backtest và tự động mở tab Báo cáo với Equity Curve và Metrics](C:/Users/Admin/.gemini/antigravity/brain/b650687b-9f2f-46ee-817c-8a7abf455532/drawer_07_backtest_auto_opened.png)
<!-- slide -->
![08. Hiển thị Responsive Laptop (1280x800)](C:/Users/Admin/.gemini/antigravity/brain/b650687b-9f2f-46ee-817c-8a7abf455532/drawer_08_laptop_1280.png)
<!-- slide -->
![09. Hiển thị Responsive Tablet (1024x768)](C:/Users/Admin/.gemini/antigravity/brain/b650687b-9f2f-46ee-817c-8a7abf455532/drawer_09_tablet_1024.png)
<!-- slide -->
![10. Hiển thị Mobile Overlay & Backdrop (375x667)](C:/Users/Admin/.gemini/antigravity/brain/b650687b-9f2f-46ee-817c-8a7abf455532/drawer_10_mobile_375.png)
````

---

## 3. Kết Quả Kiểm Thử Toàn Diện (Verification Matrix)

| Hạng mục kiểm thử | Công cụ / Lệnh | Kết quả | Chi tiết xác nhận |
|:---|:---|:---:|:---|
| **Cú pháp JS** | `node --check public/app.js` | **PASS** | 0 lỗi cú pháp |
| **Cấu trúc DOM tĩnh** | `node --test tests/test_ui_structure.test.js` | **8/8 PASS** | 0 duplicate ID; 181/181 thẻ `<div>` cân bằng tuyệt đối; đầy đủ drawer IDs; đúng phân cấp DOM |
| **Runtime Drawer Tests** | `node --test tests/test_drawer.test.js` | **12/12 PASS** | Default closed; Toggle buttons; Tab switches; Close ✕; Desktop backdrop isolation; Mobile backdrop activation; Auto-hide on resize; Escape hotkey isolation; Corrupt storage fallback; Form input retention; Backtest auto-open |
| **Drawing & Alerts Engine** | `node --test tests/test_drawings.test.js` | **56/56 PASS** | Toàn bộ 39 công cụ vẽ, 13 loại cảnh báo, undo/redo, v2 migration không bị ảnh hưởng |
| **Backend & Python API** | `python -m unittest discover tests -v` | **36/36 PASS** | Data feed 1.83M nến, resample, backtest engine, SL/TP spread, bar replay và static files |
| **Browser QA Thực Tế (CDP)** | `node tests/verify_drawer_qa.js` | **11/11 PASS** | Headless Chrome xác minh 11 bước thao tác trên giao diện thật, xác nhận backdrop ẩn trên desktop và active trên mobile, lưu ảnh 4 viewports |

---

## 4. Kết Luận
Task **T47** đã được hoàn thiện đạt chuẩn chất lượng cao nhất:
1. Giải phóng không gian tối đa cho biểu đồ khi mở trang.
2. Hai nút điều khiển độc lập trên header cho phép bật riêng Cấu hình hoặc Báo cáo.
3. Chuyển đổi tab mượt mà trong cùng một drawer.
4. Cơ chế Stacking Context 2 lớp bảo vệ triệt để, không bao giờ để backdrop che phủ hoặc chặn thao tác trên desktop/tablet.
5. Bảo toàn 100% dữ liệu form, thông số chiến lược, nét vẽ và logic khớp lệnh backtest.
