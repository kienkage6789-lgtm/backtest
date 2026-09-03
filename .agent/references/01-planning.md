# Bước 1 — Lên ý tưởng / Spec (vai trò: Product Manager)

Mục tiêu: biến yêu cầu mơ hồ của người dùng thành một spec rõ ràng, tránh việc code sai thứ họ không muốn.

## Việc cần làm

1. Đọc lại yêu cầu, liệt kê:
   - Mục tiêu chính (1-2 câu).
   - Phạm vi (scope): cái gì làm, cái gì KHÔNG làm trong lần này.
   - Ràng buộc: ngôn ngữ/framework hiện có, style code hiện tại, deadline, tương thích ngược...
2. Nếu dự án đã có `.agent/PROJECT.md`, đọc nó trước để không đề xuất thứ mâu thuẫn với kiến trúc đã chọn.
3. Nếu là dự án mới hoặc lần đầu chạm vào codebase này, **khảo sát code thật** (view cấu trúc thư mục, đọc file chính, xem package.json/requirements.txt...) trước khi viết spec — không đoán stack.
4. Viết spec ngắn gọn (không cần quá hình thức) gồm:
   - Bối cảnh & mục tiêu
   - Yêu cầu chức năng (functional requirements)
   - Acceptance criteria — tiêu chí để biết "xong" (đây là phần quan trọng nhất, sẽ được dùng lại ở bước test)
   - Rủi ro / điểm chưa rõ cần hỏi lại người dùng
5. Nếu có điểm mơ hồ ảnh hưởng lớn đến hướng làm (VD: chọn thư viện A hay B, có cần backward-compatible không), hỏi người dùng 1 câu ngắn gọn trước khi đi tiếp. Đừng hỏi nếu có thể tự chọn phương án hợp lý và ghi giả định vào `DECISIONS.md`.
6. Ghi spec vào `.agent/PROJECT.md` (phần "Yêu cầu hiện tại") hoặc file spec riêng nếu task đủ lớn (VD `.agent/specs/<ten-task>.md`).

## Output của bước này

Một đoạn spec ngắn, đủ để bước 2 (chia việc) dùng trực tiếp — không cần người dùng phải suy diễn thêm.
