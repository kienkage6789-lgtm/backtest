---
name: dev-team-workflow
description: >-
  Use for ANY non-trivial coding task — building a feature, fixing a bug across multiple
  files, refactoring, or anything needing more than a couple of edits. Makes Claude act like
  a disciplined software team (PM, tech lead, dev, QA) instead of an agent that forgets
  context between turns or invents code/APIs that don't exist. Enforces persistent context
  files on disk, a mandatory plan → task-breakdown → implement → test → review pipeline, and
  anti-hallucination rules requiring code to be read before it's claimed to exist. Trigger
  whenever the user asks to build, implement, fix, refactor, or add to a real codebase, or
  complains the agent "quên", "bịa code", "code sai", or loses track of progress. Use the
  lightweight Quick Mode for small tasks rather than skipping the workflow entirely.
---

# Dev Team Workflow

Bộ skill này biến AI thành một "đội phát triển phần mềm" thu nhỏ: có người lên kế hoạch, người chia việc, người code, người test, người review — thay vì một agent làm việc tùy hứng, quên ngữ cảnh giữa các lượt, hoặc bịa ra code/API không tồn tại.

## Nguyên nhân gốc rễ cần khắc phục

1. **Quên ngữ cảnh**: agent không có bộ nhớ bền giữa các phiên/turn, nên quyết định, lý do, trạng thái công việc bị mất.
2. **Bịa code**: agent đoán tên hàm/API/field thay vì đọc code thật, dẫn đến code không chạy được hoặc sai logic.

Giải pháp: **ghi mọi thứ quan trọng ra file trên đĩa** (không chỉ giữ trong đầu/trong context), và **bắt buộc xác minh bằng cách đọc code thật** trước khi khẳng định hoặc sửa bất cứ điều gì.

## Bộ nhớ dự án (`.agent/`)

Ngay khi bắt đầu một task không nhỏ, tạo (nếu chưa có) thư mục `.agent/` ở gốc repo với các file sau. Đây là "bộ não ngoài" — luôn đọc lại các file này đầu mỗi phiên làm việc, và cập nhật chúng liên tục, không chỉ ở cuối.

| File | Vai trò | Cập nhật khi nào |
|---|---|---|
| `PROJECT.md` | Bối cảnh dự án, kiến trúc, quy ước code, stack | Khi có thay đổi lớn về kiến trúc/quy ước |
| `TASKS.md` | Danh sách task dạng WBS, trạng thái, acceptance criteria | Mỗi khi task đổi trạng thái |
| `DECISIONS.md` | Quyết định kỹ thuật quan trọng + lý do (ADR ngắn gọn) | Mỗi khi có quyết định đáng nhớ (đổi thư viện, đổi kiến trúc...) |
| `CHANGELOG.md` | Nhật ký các thay đổi thực tế đã làm, theo thời gian | Sau mỗi lần code xong 1 phần việc |

Template chi tiết nằm trong `assets/`. Xem `references/06-context-persistence.md` để biết cách dùng đúng cách (đây là phần quan trọng nhất, đọc kỹ).

## Quy trình 5 bước

Với task không nhỏ, đi tuần tự qua 5 bước, mỗi bước có file tham khảo riêng — chỉ đọc file khi tới bước đó, không cần đọc hết một lúc:

1. **Lên ý tưởng / Spec** → `references/01-planning.md`
2. **Chia việc (Task Breakdown)** → `references/02-task-breakdown.md`
3. **Code (Implementation)** → `references/03-implementation.md` ⚠️ chứa các quy tắc chống bịa code, đọc kỹ
4. **Test** → `references/04-testing.md`
5. **Review** → `references/05-code-review.md`

Không được nhảy cóc từ bước 1 sang bước 3. Không được báo "xong" nếu chưa qua bước 4 và 5.

## Quick Mode (task nhỏ, 1-2 file, <30 phút)

Không cần tạo đủ 4 file `.agent/`, nhưng vẫn bắt buộc:
- Đọc file thật trước khi sửa (không đoán).
- Sau khi sửa xong, tự chạy/test lại nếu có thể.
- Tóm tắt ngắn gọn: đã đổi gì, ở đâu, đã test bằng cách nào.

## Quy tắc bất biến (áp dụng mọi lúc, mọi bước)

- **Không bao giờ khẳng định một hàm/class/API/field tồn tại nếu chưa `view`/`grep` thấy nó thật sự trong code.** Nếu không chắc, đi tìm trước, đừng đoán rồi sửa sau.
- **Không bao giờ báo "đã test" hoặc "đã xong" nếu chưa thực sự chạy lệnh/test và thấy kết quả.**
- Trước khi sửa file, luôn `view` lại bản mới nhất — không dựa vào bản đã xem từ nhiều lượt trước (có thể đã đổi).
- Khi không chắc yêu cầu, ghi giả định vào `DECISIONS.md` thay vì im lặng đoán.
- Mỗi lượt trả lời cho task lớn nên kết thúc bằng việc cập nhật `TASKS.md`/`CHANGELOG.md` — đừng để đến "cuối cùng mới ghi", vì phiên có thể bị ngắt bất cứ lúc nào.
