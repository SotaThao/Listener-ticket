<!-- rule-file: facts · version 5 · status ACTIVE · audience: model + code -->
# Dữ kiện

Hai loại dữ kiện, không lẫn nhau:

- **Dữ kiện model trích** (bảng `fields`): model chỉ trả các trường này, mỗi trường kèm `source_ids`. Model **không** trả kết luận (đủ rõ / hỏi / tạo ticket), không trả trạng thái.
- **Dữ kiện code tính** (bảng `code_facts`): code tự tính từ Telegram và DB, không hỏi model.

Code dùng `field_valid`, `normalize`, `generic_values` để quyết định trường nào được tính là **CÓ**. Chỉ trường CÓ mới được dùng trong `readiness.md`.

## Bảng: fields

| Trường | Kiểu | Ý nghĩa | Ví dụ |
|---|---|---|---|
| `intent` | một trong `bug` `task` `feedback` `question` `chat` `unknown` | Ý định của chủ đề. Không chắc ⇒ `unknown` | `bug` |
| `object` | chuỗi | Đối tượng cụ thể: màn hình / chức năng / nút / setting / thiết bị | "máy in POS" |
| `observed` | chuỗi | Hành vi đang xảy ra | "không in được receipt" |
| `expected` | chuỗi | Kết quả mong muốn | "in được receipt" |
| `work` | chuỗi | Việc cần làm | "đưa ảnh Nail cũ vào gallery" |
| `problem` | chuỗi | Vấn đề hoặc nhu cầu (feedback) | "khó tìm giao dịch" |
| `proposal` | chuỗi | Đề xuất (feedback) | "thêm bộ lọc ngày" |
| `media_ids` | danh sách ID tin | Tin có ảnh/tài liệu thuộc chủ đề | `[843]` |

Cách model ghi giá trị:

1. Giá trị là **cụm ngắn nhất trong lời người gửi** nói đúng trường đó, giữ nguyên ngôn ngữ, không dịch, không thêm chữ. Ví dụ "Check giúp em cái này" ⇒ `work` = "check giúp", `object` = "cái này".
2. Không chắc ⇒ để trống. Không đoán thiết bị, môi trường, nguyên nhân, bước tái hiện.
3. Giá trị đọc từ ảnh (heading panel, thông báo lỗi) được phép; `source_ids` phải là tin chứa ảnh đó.
4. Một chủ đề chỉ có một `intent`. Gom/tách chủ đề theo TG-05/TG-06, trước bước này.

## Bảng: code_facts

| Trường | Giá trị | Cách tính |
|---|---|---|
| `command` | `∅` hoặc một command trong bảng `command_intent` | Command đầu tin đầu chủ đề (TG-04), không phân biệt hoa thường, bỏ `@BotName` |
| `command_payload` | `none` `text` `media` `empty` | `none`: không có command · `text`: có chữ sau command · `media`: chỉ có ảnh/tài liệu kèm command · `empty`: không có gì |
| `text` | `có` `không` | `có` khi chủ đề có ít nhất một tin chữ hoặc caption của người gửi, không tính chữ command |
| `media` | `có` `không` | `có` khi `media_seen` + `media_unseen` > 0 |
| `media_seen` | số ≥ 0 | Số ảnh của chủ đề đã gửi cho model xem |
| `media_unseen` | số ≥ 0 | Số ảnh của chủ đề **không** gửi cho model (vượt giới hạn, lỗi tải) |
| `extraction_ok` | `true` `false` | `false` khi gọi model lỗi, hết quota, hoặc output sai schema sau khi lọc |
| `reply_target` | `none` `ticket_open` `bot_question` | `ticket_open`: Reply thật vào tin đã chứng minh thuộc ticket đang open (TG-16) · `bot_question`: Reply thật vào câu hỏi của bot ở group xử lý (`questions.md` · `answer_link`) · còn lại `none` |

Reply vào ticket **đã đóng** không đi qua `readiness.md`: xử lý theo bảng **Vòng đời ticket** và **Ticket đã đóng** trong Business DOCS (sẽ thành `lifecycle.md`).

## Bảng: command_intent

| Command | intent |
|---|---|
| `/bug` | `bug` |
| `/task` | `task` |
| `/feature` | `feedback` |
| `/feedback` | `feedback` |
| `/question` | `question` |

## Bảng: field_valid

Một trường chữ chỉ được tính là **CÓ** khi thỏa **tất cả**:

| # | Điều kiện |
|---|---|
| V1 | Giá trị sau `normalize` không rỗng |
| V2 | Có ít nhất một `source_id` thuộc chủ đề đang xét |
| V3 | Giá trị sau `normalize` **không bằng nguyên cụm** với một dòng của trường đó trong `generic_values` |
| V4 | Nếu `source_ids` chỉ trỏ tới tin ảnh: ảnh đó thuộc nhóm đã gửi model xem (`media_seen`) |

Trường thừa ngoài bảng `fields` ⇒ bỏ. Trường thiếu ⇒ trống. `intent` ngoài danh sách ⇒ `unknown`. Giá trị dài quá `max_value_chars` ⇒ cắt bớt, vẫn tính CÓ.

## Bảng: normalize

Áp đúng thứ tự, cho cả giá trị model trả và cụm trong `generic_values`:

| # | Bước |
|---|---|
| N1 | Chuẩn Unicode NFC |
| N2 | Chuyển chữ thường (casefold); **giữ** dấu tiếng Việt |
| N3 | Gộp mọi khoảng trắng liên tiếp thành một dấu cách, cắt khoảng trắng đầu/cuối |
| N4 | Bỏ dấu câu ở đầu và cuối: `. , ! ? ; : … " ' “ ” ‘ ’ ( ) [ ] -` |
| N5 | Nếu từ đầu tiên là `em` `anh` `chị` `mình` `bạn` và còn từ khác phía sau ⇒ bỏ từ đó (một lần) |
| N6 | Lặp lại khi còn hơn một từ: nếu từ cuối (bỏ dấu câu dính kèm) là chữ đệm `nha` `nhé` `nhe` `nhá` `với` `ạ` `đi` `nè` ⇒ bỏ từ đó; có bỏ thì bỏ tiếp dấu câu ở cuối như N4 |

## Bảng: generic_values

Giá trị chung chung: bằng nguyên cụm (sau `normalize`) ⇒ **không** tính là CÓ. Chứa cụm nhưng còn chữ khác (vd "nút Thanh toán") ⇒ không phải chung chung.

Owner 2026-11-06 (live #115): “Duyệt + thêm ví dụ” — lời nhờ báo lại thông tin ("báo giùm em", "cho em biết") không phải việc cần làm. Owner 2026-11-06: “Duyệt cả ba” — thêm biến thể (báo dùm/hộ, cho mình/anh/chị biết, trả lời/xác nhận giúp) và bước N6 bỏ chữ đệm cuối ("báo giùm em nha" ⇒ "báo giùm em").

| Trường | Cụm |
|---|---|
| `object` | `nút này` |
| `object` | `cái này` |
| `object` | `cái đó` |
| `object` | `chỗ này` |
| `object` | `màn này` |
| `object` | `trang này` |
| `object` | `nó` |
| `object` | `app` |
| `object` | `web` |
| `observed` | `lỗi` |
| `observed` | `bị lỗi` |
| `observed` | `lỗi rồi` |
| `observed` | `không được` |
| `observed` | `có vấn đề` |
| `observed` | `không chạy` |
| `observed` | `hỏng` |
| `work` | `xử lý` |
| `work` | `check` |
| `work` | `check giúp` |
| `work` | `xem giúp` |
| `work` | `kiểm tra giúp` |
| `work` | `làm giúp` |
| `work` | `sửa giúp` |
| `work` | `báo giùm` |
| `work` | `báo giúp` |
| `work` | `báo giùm em` |
| `work` | `báo giúp em` |
| `work` | `cho em biết` |
| `work` | `báo em biết` |
| `work` | `báo dùm` |
| `work` | `báo hộ` |
| `work` | `báo giùm e` |
| `work` | `cho e biết` |
| `work` | `cho mình biết` |
| `work` | `cho anh biết` |
| `work` | `cho chị biết` |
| `work` | `nói em biết` |
| `work` | `báo lại` |
| `work` | `báo lại giùm` |
| `work` | `trả lời giúp` |
| `work` | `xác nhận giúp` |
| `problem` | `có vấn đề` |
| `problem` | `không ổn` |
| `problem` | `chưa ổn` |
| `proposal` | `sửa lại` |
| `proposal` | `làm lại` |
| `proposal` | `cải thiện` |

## Bảng: limits

| Khóa | Giá trị | Nghĩa |
|---|---|---|
| `max_value_chars` | `200` | Độ dài tối đa một giá trị trường chữ |
