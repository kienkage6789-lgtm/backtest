# Bước 5 — Review (vai trò: Reviewer / Senior Dev)

Bước cuối trước khi đánh dấu task `done`. Tự đóng vai người review code của chính mình, khách quan như thể đang review code của người khác.

## Checklist review

- [ ] Code có đúng những gì spec/acceptance criteria yêu cầu — không thừa, không thiếu (scope creep)?
- [ ] Không còn hàm/biến/import bịa ra không thật sự tồn tại (double-check bằng grep nếu nghi ngờ)?
- [ ] Xử lý lỗi hợp lý (không nuốt exception âm thầm, không crash với input hợp lệ)?
- [ ] Style nhất quán với phần còn lại của codebase?
- [ ] Không để lại code chết (dead code), debug print, TODO mập mờ không giải thích?
- [ ] Không hardcode thứ nên là config/tham số (trừ khi cố ý và có ghi chú)?
- [ ] Đã cập nhật tài liệu liên quan nếu có (README, comment, docstring) khi hành vi thay đổi?
- [ ] Không phá vỡ chức năng khác đang hoạt động (nếu sửa file dùng chung, kiểm tra ảnh hưởng)?

## Nếu review phát hiện vấn đề

Quay lại bước 3, sửa, rồi test lại (bước 4) trước khi review lại — không tự "cho qua" vấn đề đã thấy.

## Khi review đạt

1. Đánh dấu task `[x] done` trong `TASKS.md`.
2. Tóm tắt ngắn gọn cho người dùng: đã làm gì, test thế nào, còn gì cần họ tự kiểm tra thêm (nếu có).
3. Nếu còn task khác trong `TASKS.md` ở trạng thái `todo`, hỏi người dùng có muốn tiếp tục ngay hay dừng ở đây.
