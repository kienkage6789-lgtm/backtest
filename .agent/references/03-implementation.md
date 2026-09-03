# Bước 3 — Code (vai trò: Developer)

Đây là bước quan trọng nhất để chống "bịa code". Agent bịa code thường vì: đoán tên hàm/API thay vì tra cứu, giả định cấu trúc file thay vì đọc thật, hoặc tự tin từ "trí nhớ huấn luyện" thay vì từ code hiện tại trong repo.

## Quy tắc bắt buộc trước khi viết/sửa code

1. **Luôn đọc file thật trước khi sửa.** Không dựa vào bản đã xem ở lượt trước — file có thể đã đổi (do người dùng sửa tay, hoặc do lượt trước của chính agent).
2. **Không đoán API.** Trước khi gọi một hàm/thư viện/class:
   - Nếu là code nội bộ trong repo: `grep`/`view` để xác nhận hàm đó tồn tại, đúng chữ ký (tham số, kiểu trả về).
   - Nếu là thư viện ngoài (npm/pip package...): kiểm tra `package.json`/`requirements.txt` xem đã có sẵn chưa và version nào; nếu không chắc API đúng, tra cứu docs/README thay vì đoán từ trí nhớ.
3. **Không tự thêm dependency mới** mà không nói với người dùng — bịa ra một thư viện "chắc là có" là lỗi phổ biến.
4. **Giữ style code hiện có** (đặt tên biến, cách format, cách xử lý lỗi) — đọc 1-2 file lân cận để bắt chước convention thay vì áp phong cách riêng.
5. Nếu một task đụng vào nhiều file, sửa từng file một, và sau mỗi file quan trọng, cân nhắc chạy thử (compile/run/import) trước khi sang file tiếp theo — phát hiện lỗi sớm rẻ hơn phát hiện muộn.

## Trong lúc code

- Nếu phát hiện spec/acceptance criteria có mâu thuẫn với thực tế code (VD: acceptance criteria giả định có sẵn 1 hàm nhưng thực ra chưa có), dừng lại, cập nhật `TASKS.md`/`DECISIONS.md` ghi rõ vấn đề, rồi mới tiếp tục — đừng âm thầm tự chế ra hàm đó và giả vờ nó luôn tồn tại.
- Commit thông tin quan trọng (quyết định đổi hướng, lý do chọn giải pháp X thay vì Y) vào `DECISIONS.md` ngay lúc đó, không để cuối buổi mới nhớ lại.

## Sau khi code xong 1 task

1. Cập nhật `TASKS.md`: chuyển trạng thái sang `review` (chưa phải `done` — `done` chỉ gán sau bước test + review ở bước 4-5).
2. Ghi vào `CHANGELOG.md`: file nào đổi, đổi gì, vì sao (ngắn gọn, vài dòng).
3. Chuyển sang bước 4 (Testing) — không dừng ở "tôi nghĩ nó chạy được", phải kiểm chứng thật.
