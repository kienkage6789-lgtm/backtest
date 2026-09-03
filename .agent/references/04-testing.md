# Bước 4 — Test (vai trò: QA)

Mục tiêu: không bao giờ báo "xong"/"đã chạy được" mà chưa thực sự kiểm chứng. Đây là chốt chặn cuối cùng chống lại việc báo cáo sai sự thật (dù vô tình).

## Quy tắc bắt buộc

- **Không được nói "đã test" nếu không thực sự chạy lệnh và thấy output.** Nếu môi trường không cho phép chạy (thiếu tool, thiếu quyền), phải nói rõ điều đó với người dùng thay vì giả vờ đã chạy.
- Ưu tiên theo thứ tự khả dụng:
  1. Chạy test tự động có sẵn trong repo (`pytest`, `npm test`, `dotnet test`...).
  2. Nếu chưa có test, viết test mới nhắm đúng acceptance criteria của task (bước 2), rồi chạy.
  3. Nếu không thể viết test tự động (VD: cần UI thật, cần hardware), viết kịch bản kiểm tra thủ công (manual test steps) rõ ràng và nói với người dùng đây là phần họ cần tự xác nhận.
- Đối chiếu kết quả test với **acceptance criteria đã ghi trong `TASKS.md`** — không tự bịa tiêu chí mới để "cho qua".
- Nếu test fail: quay lại bước 3 (code), không được sửa test cho khớp với code sai chỉ để test pass.
- Test edge case tối thiểu: input rỗng/null, input sai định dạng, giới hạn biên (nếu liên quan).

## Sau khi test xong

- Cập nhật acceptance criteria trong `TASKS.md` (tick từng mục `[x]` đã đạt, để trống mục chưa đạt).
- Ghi vào `CHANGELOG.md`: đã test bằng cách nào, kết quả ra sao (kể cả khi có phần chưa test được và tại sao).
- Nếu tất cả tiêu chí đạt → chuyển sang bước 5 (Review). Nếu chưa → quay lại bước 3, KHÔNG chuyển trạng thái task sang `done`.
