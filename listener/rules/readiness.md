<!-- rule-file: readiness · version 2 · status DRAFT · audience: code -->
# "Đủ rõ" — bảng quyết định (TG-08)

Input: dữ kiện code tính + các trường CÓ theo `facts.md`. Output: một mã trong `states.md`. **Model không thấy file này.**

Cách đọc mọi bảng ở đây:

- Đọc từ trên xuống, **dòng đầu tiên khớp mọi cột là kết quả**.
- Ô `*` = bất kỳ. Ô có một hoặc nhiều giá trị trong backtick = giá trị phải là **một trong** các giá trị đó.
- `cần CÓ tất cả`: mọi trường liệt kê phải CÓ. `cần CÓ ít nhất một`: ít nhất một trường CÓ. Ô `—` = không yêu cầu.

## Bảng: intent_resolve

Tính `intent` dùng cho bảng `decision` (Owner chốt 2026-10-29: command quyết trước model).

| # | command | intent (model) | cần CÓ | intent dùng |
|---|---|---|---|---|
| I1 | `∅` | `question` | `work` | `task` |
| I2 | `∅` | * | — | `=model` |
| I3 | * | * | — | `=command` |

`=model`: lấy `intent` model trả (ngoài danh sách ⇒ `unknown`). `=command`: tra `facts.md` · `command_intent`.

## Bảng: decision

| # | command | command_payload | text | media | extraction_ok | reply_target | intent | cần CÓ tất cả | cần CÓ ít nhất một | Kết quả | Ghi chú |
|---|---|---|---|---|---|---|---|---|---|---|---|
| R1 | `/question` | * | * | * | * | * | * | — | — | `CHAT` | TG-09 |
| R2 | * | `empty` | * | * | * | * | * | — | — | `CHAT` | Command không nội dung là noise (TG-04) |
| R3 | `∅` | * | `không` | `có` | * | `none` | * | — | — | `MEDIA_ONLY` | Ảnh không chữ: im lặng, giữ pending (TG-15). Không cần model |
| R4 | * | * | * | * | `false` | * | * | — | — | `HOLD` | Lỗi của bot, không hỏi người gửi |
| R5 | * | * | * | * | * | `ticket_open` | `chat` | — | — | `CHAT` | "Cảm ơn", "dạ" vào ticket: bỏ qua |
| R6 | * | * | * | * | * | `ticket_open` | * | — | — | `APPEND` | Luôn bổ sung, không xét R9–R11 (Owner chốt 2026-10-29) |
| R7 | * | * | * | * | * | * | `chat` | — | — | `CHAT` | |
| R8 | * | * | * | * | * | * | `question` | — | — | `CHAT` | Câu hỏi không kèm việc (có việc đã thành `task` ở I1) |
| R9 | * | * | * | * | * | * | `bug` | `object` `observed` | — | `READY` | Thiếu `expected` ⇒ mặc định "hoạt động đúng như trước", không hỏi |
| R10 | * | * | * | * | * | * | `task` | `object` `work` | — | `READY` | Redesign panel/mode đã nhận diện là `READY` |
| R11 | * | * | * | * | * | * | `feedback` | — | `problem` `proposal` | `READY` | |
| R12 | * | * | * | * | * | * | `bug` `task` `feedback` | — | — | `NEED_INFO` | Hỏi trường thiếu theo `questions.md` · `ask_order` |
| R13 | * | * | * | * | * | * | * | — | — | `NEED_INFO` | Gồm `unknown`: câu mặc định `questions.md` · `fallback` |

## Bảng: never_blocks

Thiếu những thứ dưới đây **không bao giờ** làm mất `READY`. Có thì ghi vào ticket; thiếu thì liệt kê ở mục "Cần làm rõ" của ticket, không hỏi.

| Thứ không chặn |
|---|
| thiết bị, trình duyệt, bản build |
| log, mã lỗi chi tiết |
| bước tái hiện |
| tài khoản thử |
| deadline, người phụ trách, thứ tự ưu tiên |
| bản thiết kế/layout tương lai (task redesign) |
| `expected` |
| ảnh model chưa xem (`media_unseen` > 0) |
| điểm `confidence` của model |

## Bảng: unseen_media

Owner chốt D7 = A (2026-10-29).

| # | Quy tắc |
|---|---|
| U1 | `media_unseen` không tham gia bảng `decision`; kết quả chỉ dựa trên trường CÓ từ chữ và ảnh đã xem |
| U2 | Ticket `READY`/`APPEND` đính **mọi** ảnh của chủ đề (kể cả ảnh model chưa xem) và ghi "model chưa xem N ảnh" |
| U3 | Giá trị chỉ đọc được từ ảnh chưa xem không tồn tại (V4), nên không thể làm trường thành CÓ |

## Bảng: confidence

Owner chốt = A (2026-10-29).

| # | Quy tắc |
|---|---|
| C1 | Điểm `confidence` của model **không** tham gia quyết định |
| C2 | Bot vẫn ghi `confidence` vào nhật ký quyết định để theo dõi chất lượng model |
