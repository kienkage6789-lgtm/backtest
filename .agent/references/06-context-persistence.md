# Duy trì ngữ cảnh bền vững (`.agent/`) — chống "agent quên"

Đây là cơ chế cốt lõi giải quyết vấn đề "agent hay quên ngữ cảnh". Ý tưởng: đừng để trạng thái công việc chỉ tồn tại trong cuộc hội thoại (dễ mất khi context bị cắt, đổi phiên, hoặc đổi agent) — hãy ghi nó ra file trong chính repo, để bất kỳ agent/phiên nào (kể cả một Claude khác hoàn toàn) mở lại dự án cũng hiểu ngay tình hình chỉ bằng cách đọc 3-4 file.

## Nguyên tắc

1. **Đọc trước khi làm.** Đầu mỗi phiên/task mới, việc đầu tiên là đọc `.agent/PROJECT.md`, `.agent/TASKS.md`, `.agent/DECISIONS.md`, và vài dòng cuối `.agent/CHANGELOG.md`. Đừng dựa vào trí nhớ hội thoại — hội thoại có thể bị tóm tắt/cắt, file thì không.
2. **Ghi ngay, đừng để cuối.** Cập nhật file ngay khi có thông tin mới (bắt đầu task, đổi quyết định, code xong 1 phần) — không dồn lại "để cuối buổi ghi một lần", vì phiên có thể dừng đột ngột bất cứ lúc nào.
3. **Ngắn gọn, có cấu trúc, dễ máy đọc lại.** Đây là bộ nhớ cho agent đọc, không phải văn bản trình bày cho người — ưu tiên bullet, checklist, heading rõ ràng hơn là văn xuôi dài dòng.
4. **Một nguồn sự thật (single source of truth).** Nếu thông tin trong hội thoại và trong file `.agent/` mâu thuẫn nhau, file trên đĩa thắng — vì hội thoại có thể đã cũ hoặc bị tóm tắt sai.

## 4 file cốt lõi

- `PROJECT.md` — bối cảnh tĩnh: dự án làm gì, stack, kiến trúc, quy ước code, cách chạy/build/test. Hiếm khi đổi.
- `TASKS.md` — trạng thái động: task nào đang làm, đã xong, còn tồn đọng. Đổi liên tục.
- `DECISIONS.md` — "tại sao lại làm thế này mà không phải thế kia" — cực kỳ quan trọng để agent sau không vô tình đảo ngược một quyết định đã cân nhắc kỹ.
- `CHANGELOG.md` — nhật ký "đã thực sự làm gì" theo thời gian, để đối chiếu với TASKS.md (task nói đã done, changelog xác nhận đã có code thật tương ứng).

Xem template mẫu trong `assets/`.

## Khi dự án đã có sẵn `.agent/` (từ phiên trước)

Không tạo lại từ đầu — đọc và tiếp nối. Nếu cấu trúc cũ khác template này, tôn trọng cấu trúc cũ, chỉ bổ sung phần còn thiếu.

## Khi task quá nhỏ để cần cả bộ máy này

Không bắt buộc tạo `.agent/` cho việc sửa 1 dòng code hoặc trả lời 1 câu hỏi. Nhưng nếu repo đã có `.agent/` sẵn từ trước, vẫn nên đọc nhanh `TASKS.md` để biết task nhỏ này có liên quan gì đến các task khác đang dở dang không.
