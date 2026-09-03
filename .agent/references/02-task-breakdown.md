# Bước 2 — Chia việc / Task Breakdown (vai trò: Tech Lead)

Mục tiêu: cắt spec thành các task nhỏ, độc lập kiểm tra được, để không bị "code lan man" và để mỗi lượt làm việc có ranh giới rõ ràng — điều này cũng là cách chống quên ngữ cảnh: mỗi task tự chứa đủ thông tin để tiếp tục dù bị ngắt phiên.

## Nguyên tắc chia task

- Mỗi task nên hoàn thành được trong một lượt làm việc (không quá lớn).
- Mỗi task có **acceptance criteria** riêng, kiểm tra được (không mơ hồ kiểu "làm cho tốt").
- Task nào phụ thuộc task nào phải ghi rõ (dependency).
- Ưu tiên thứ tự: nền tảng/hạ tầng trước → chức năng chính → chức năng phụ → polish/UI → test → docs.

## Format 1 task (ghi vào `TASKS.md`)

```
### [ ] T01 - <tên task ngắn gọn>
- Mô tả: <1-2 câu>
- File liên quan: <đường dẫn file, nếu biết trước>
- Acceptance criteria:
  - [ ] <tiêu chí 1, kiểm tra được>
  - [ ] <tiêu chí 2>
- Phụ thuộc: <T00 hoặc "không>
- Trạng thái: todo | doing | blocked | review | done
```

Dùng `[x]` khi task xong hoàn toàn (tất cả acceptance criteria đạt và đã test).

## Việc cần làm

1. Liệt kê toàn bộ task theo format trên vào `.agent/TASKS.md`.
2. Sắp xếp theo thứ tự thực hiện hợp lý.
3. Nếu người dùng muốn xem trước danh sách task trước khi code, trình bày tóm tắt (không cần dán nguyên file) và xác nhận.
4. Khi bắt đầu làm 1 task, đổi trạng thái sang `doing` NGAY LẬP TỨC trong file — đừng đợi làm xong mới ghi, vì nếu phiên bị ngắt giữa chừng, task tiếp theo (có thể là một agent/phiên khác) cần biết task này đang dang dở, không phải chưa bắt đầu.
5. Không tự ý gộp nhiều task lại làm một lượt để "cho nhanh" nếu chúng không thực sự phụ thuộc nhau — làm vậy phá vỡ khả năng theo dõi tiến độ.

## Khi nhận lại một dự án đang dang dở

Trước khi làm gì, luôn:
1. Đọc `.agent/TASKS.md` để biết task nào done, doing, todo.
2. Đọc `.agent/DECISIONS.md` để biết các quyết định đã chốt (tránh làm lại/đổi ngược).
3. Đọc `.agent/CHANGELOG.md` gần nhất để biết code thực tế đã thay đổi gì.
4. Chỉ sau đó mới tiếp tục — không đoán trạng thái dự án từ trí nhớ hội thoại.
