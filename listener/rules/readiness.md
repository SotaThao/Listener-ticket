<!-- rule-file: readiness · version 4 · status ACTIVE · audience: code -->
# "Đủ rõ" — bảng quyết định (TG-08)

Owner 2026-11-05: “Duyệt lọc trước” — nhờ review/duyệt/merge PR hoặc xem issue GitHub đã có link không phải task, bug hay feedback, không log ticket.

Input: dữ kiện code tính + các trường CÓ theo `facts.md`. Output: một mã trong `states.md`. **Model không thấy file này.**

Cách đọc mọi bảng ở đây:

- Đọc từ trên xuống, **dòng đầu tiên khớp mọi cột là kết quả**.
- Ô `*` = bất kỳ. Ô có một hoặc nhiều giá trị trong backtick = giá trị phải là **một trong** các giá trị đó.
- `cần CÓ tất cả`: mọi trường liệt kê phải CÓ. `cần CÓ ít nhất một`: ít nhất một trường CÓ. Ô `—` = không yêu cầu.

## Bảng: prefilter

Lọc local **trước khi gọi model** và trước bảng `decision`. Chỉ xét tin mới: không phải Reply, không command, không ảnh, không tài liệu. Khớp ⇒ `CHAT` ngay với luật `F1`, không gọi model. Không khớp ⇒ đi tiếp như cũ. Nhóm nhiều tin chỉ thành `CHAT` khi **mọi** tin đều khớp.

| # | Bước |
|---|---|
| P1 | Tin có ít nhất một link GitHub tới PR hoặc issue: `github.com/<owner>/<repo>/pull/<số>` hoặc `github.com/<owner>/<repo>/issues/<số>` |
| P2 | Bỏ ký tự vô hình; bỏ link GitHub ở P1 (đuôi link dừng ở `,` `;`, khoảng trắng và chữ có dấu); còn link khác ⇒ không khớp; bỏ mọi `@tên`, mọi cụm chỉ gồm số hoặc `#` + số; đổi dấu câu, emoji và `_` thành khoảng trắng; chữ thường; tách theo khoảng trắng |
| P3 | Mọi từ còn lại thuộc bảng `prefilter_words`, và có ít nhất một từ loại `việc` |
| P4 | Khớp P1–P3 ⇒ `CHAT` (luật `F1`); báo lỗi sản phẩm hay có chữ ngoài bảng ⇒ không khớp |

## Bảng: prefilter_words

`việc` = việc được nhờ (phải có ít nhất một). `đệm` = xưng hô, xã giao, tên gọi PR/issue.

| Loại | Từ |
|---|---|
| `việc` | `review` |
| `việc` | `rv` |
| `việc` | `duyệt` |
| `việc` | `approve` |
| `việc` | `merge` |
| `việc` | `xem` |
| `việc` | `coi` |
| `việc` | `check` |
| `đệm` | `anh` |
| `đệm` | `em` |
| `đệm` | `chị` |
| `đệm` | `bạn` |
| `đệm` | `mình` |
| `đệm` | `team` |
| `đệm` | `giúp` |
| `đệm` | `giùm` |
| `đệm` | `dùm` |
| `đệm` | `hộ` |
| `đệm` | `nhờ` |
| `đệm` | `cho` |
| `đệm` | `với` |
| `đệm` | `nha` |
| `đệm` | `nhé` |
| `đệm` | `nhe` |
| `đệm` | `nhá` |
| `đệm` | `ạ` |
| `đệm` | `ơi` |
| `đệm` | `đi` |
| `đệm` | `này` |
| `đệm` | `cái` |
| `đệm` | `đây` |
| `đệm` | `link` |
| `đệm` | `pr` |
| `đệm` | `pull` |
| `đệm` | `request` |
| `đệm` | `issue` |
| `đệm` | `please` |
| `đệm` | `pls` |

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
