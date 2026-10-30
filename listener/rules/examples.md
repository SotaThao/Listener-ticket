<!-- rule-file: examples · version 2 · status DRAFT · audience: code -->
# Ví dụ chuẩn

Bộ test cố định (`listener/tests/test_rules_markdown.py`): cho **dữ kiện** đầu vào, áp `facts.md` → `readiness.md` → `questions.md` phải ra đúng **Kết quả**, **Luật**, **Hỏi** — không phụ thuộc model. Mỗi luật R1–R13 có ít nhất một ví dụ.

Khi gửi model làm ví dụ trích dữ kiện, bot chỉ gửi cột "Tin (rút gọn)", "intent (model)" và "Dữ kiện" — **không** gửi các cột còn lại.

Cách ghi cột "Dữ kiện": `trường=giá trị` cách nhau bằng `;`; `∅` = model không trả trường nào. Đuôi đánh dấu nguồn:

- `[ảnh-đã-xem]`: giá trị đọc từ ảnh đã gửi model — hợp lệ.
- `[ảnh-chưa-xem]`: giá trị chỉ có từ ảnh model chưa xem — **không** hợp lệ (V4).
- `[ngoài-chủ-đề]`: `source_ids` không thuộc chủ đề — **không** hợp lệ (V2).

## Bảng: examples

| # | Tin (rút gọn) | command | command_payload | text | media_seen | media_unseen | extraction_ok | reply_target | intent (model) | Dữ kiện | Kết quả | Luật | Hỏi |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| X1 | "POS không in được receipt, cả app và web, máy in có dây" (#47) | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `bug` | object=máy in POS; observed=không in được receipt | `READY` | R9 | — |
| X2 | "Login đúng pass mà không login được" + ảnh "Invalid username or password" | `∅` | `none` | `có` | 1 | 0 | `true` | `none` | `bug` | object=đăng nhập; observed=báo sai mật khẩu dù nhập đúng [ảnh-đã-xem] | `READY` | R9 | — |
| X3 | Ảnh panel "Salon information & visibility" + "design đổi bảng này" (#46) | `∅` | `none` | `có` | 1 | 0 | `true` | `none` | `task` | object=panel Salon information & visibility [ảnh-đã-xem]; work=design lại | `READY` | R10 | — |
| X4 | "bitcoinnailbar.com/#gallery sao những hình Nail trước đó em không mang qua" (#82) | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `task` | object=gallery bitcoinnailbar; work=mang hình Nail trước đó qua | `READY` | R10 | — |
| X5 | "Nên có bộ lọc theo ngày ở giao dịch" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `feedback` | proposal=bộ lọc theo ngày ở giao dịch | `READY` | R11 | — |
| X6 | "Nút này lỗi" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `bug` | object=nút này; observed=lỗi | `NEED_INFO` | R12 | `ask.object` `ask.observed` |
| X7 | "Check giúp em cái này" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `task` | object=cái này; work=check giúp | `NEED_INFO` | R12 | `ask.object` `ask.work` |
| X8 | Một ảnh, không chữ | `∅` | `none` | `không` | 1 | 0 | `true` | `none` | `unknown` | ∅ | `MEDIA_ONLY` | R3 | — |
| X9 | "Dạ để em check" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `chat` | ∅ | `CHAT` | R7 | — |
| X10 | "/question API có rate limit không?" | `/question` | `text` | `có` | 0 | 0 | `true` | `none` | `question` | ∅ | `CHAT` | R1 | — |
| X11 | "/bug Thanh toán bị trừ tiền 2 lần" | `/bug` | `text` | `có` | 0 | 0 | `true` | `none` | `bug` | object=thanh toán; observed=bị trừ tiền 2 lần | `READY` | R9 | — |
| X12 | "/task" không kèm gì | `/task` | `empty` | `không` | 0 | 0 | `true` | `none` | `unknown` | ∅ | `CHAT` | R2 | — |
| X13 | "/bug" + ảnh lỗi `COMMON_VALIDATION_ERROR` ở màn đặt lịch | `/bug` | `media` | `không` | 1 | 0 | `true` | `none` | `unknown` | object=màn đặt lịch [ảnh-đã-xem]; observed=báo COMMON_VALIDATION_ERROR [ảnh-đã-xem] | `READY` | R9 | — |
| X14 | "Màn thanh toán bị treo" — gọi model lỗi | `∅` | `none` | `có` | 0 | 0 | `false` | `none` | `∅` | ∅ | `HOLD` | R4 | — |
| X15 | Reply vào tin của ticket open: "vẫn bị" | `∅` | `none` | `có` | 0 | 0 | `true` | `ticket_open` | `bug` | observed=vẫn bị | `APPEND` | R6 | — |
| X16 | Reply vào tin của ticket open: "Cảm ơn em" | `∅` | `none` | `có` | 0 | 0 | `true` | `ticket_open` | `chat` | ∅ | `CHAT` | R5 | — |
| X17 | "Em hỏi chút, API có rate limit không?" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `question` | ∅ | `CHAT` | R8 | — |
| X18 | "Thêm được export CSV ở màn báo cáo không anh?" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `question` | object=màn báo cáo; work=thêm export CSV | `READY` | R10 | — |
| X19 | "Cái này sao vậy anh" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `unknown` | object=cái này | `NEED_INFO` | R13 | `question.fallback` |
| X20 | "Máy in POS in ra giấy trắng" + 7 ảnh (5 đã xem, 2 chưa xem) | `∅` | `none` | `có` | 5 | 2 | `true` | `none` | `bug` | object=máy in POS; observed=in ra giấy trắng | `READY` | R9 | — |
| X21 | "Lỗi ở màn này" + 1 ảnh model chưa xem | `∅` | `none` | `có` | 0 | 1 | `true` | `none` | `bug` | object=màn thanh toán [ảnh-chưa-xem]; observed=lỗi | `NEED_INFO` | R12 | `ask.object` `ask.observed` |
| X22 | Trả lời câu hỏi của bot ở group xử lý: "nút Thanh toán, bấm không phản hồi" | `∅` | `none` | `có` | 0 | 0 | `true` | `bot_question` | `bug` | object=nút Thanh toán; observed=bấm không phản hồi | `READY` | R9 | — |
| X23 | "Màn giao dịch không ổn" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `feedback` | object=màn giao dịch; problem=không ổn | `NEED_INFO` | R12 | `ask.problem` |
| X24 | "Không in được" — đối tượng model lấy từ tin của chủ đề khác | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `bug` | object=máy in POS [ngoài-chủ-đề]; observed=không in được | `NEED_INFO` | R12 | `ask.object` |

## Bảng: normalize_cases

Kiểm `facts.md` · `normalize` + `generic_values` (V3).

| # | Trường | Giá trị | Chung chung |
|---|---|---|---|
| G1 | `object` | `Nút này` | `có` |
| G2 | `object` | `nút Thanh toán` | `không` |
| G3 | `observed` | `Bị lỗi!` | `có` |
| G4 | `work` | `Em check giúp` | `có` |
| G5 | `work` | `check giúp em cái này` | `không` |
| G6 | `object` | `  CÁI   NÀY. ` | `có` |
| G7 | `observed` | `không in được` | `không` |
| G8 | `problem` | `Chưa ổn…` | `có` |

G5: cả cụm không bằng cụm chung chung nào ⇒ tính là CÓ. Vì vậy model phải ghi **cụm ngắn nhất** (`facts.md`, cách ghi giá trị 1): "Check giúp em cái này" ⇒ `work`=check giúp, `object`=cái này (X7).
