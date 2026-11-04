<!-- rule-file: channels · version 4 · status ACTIVE · audience: code -->
# Bot nói gì, ở đâu

Owner chốt 2026-10-29: **group gốc chỉ có reaction**; mọi tin chữ của bot (câu hỏi, link ticket, báo lỗi, hết quota) gửi vào **một group xử lý chung** cho mọi group nguồn, để group gốc không bị loãng.

Owner 2026-11-04: Đồng ý cả 4 + Duyệt, chạy tiếp (L1 chỉ cho NEED_INFO).
Không trạng thái nào được gửi tin chữ vào group nguồn.

## Bảng: channels

| Kênh | Là gì | Config | Bot được gửi |
|---|---|---|---|
| `source` | Group Telegram nơi team báo việc (có thể nhiều group) | Danh sách chat nguồn trong config hiện tại | **Chỉ reaction** ❤️ 👌 theo TG-13. Không tin chữ nào |
| `ops` | Group xử lý chung | `ops_chat_id` — **bắt buộc**; thiếu ⇒ không khởi động | Mọi tin chữ của bot theo bảng `outputs` |

## Bảng: outputs

Mỗi trạng thái đúng một dòng. Ô `Không` = bot không làm gì ở kênh đó.

| Trạng thái | Group gốc | Group xử lý | Khối template |
|---|---|---|---|
| `READY` | ❤️ khi nhận, 👌 khi có số ticket | Link ticket mới | `ops.header` `notice.issue` |
| `APPEND` | ❤️ | Link ticket đã bổ sung | `ops.header` `followup.updated` |
| `NEED_INFO` | ❤️ | Một tin hỏi theo `questions.md` | `ops.header` `ask.object` `ask.observed` `ask.work` `ask.problem` `question.fallback` |
| `MEDIA_ONLY` | Không | Không | — |
| `CHAT` | Không | Không | — |
| `HOLD` | Không | Hết quota: báo hỗ trợ (TG-12). `ESCALATED`: báo Owner | `ops.header` `notice.rate_limited` `notice.hold_escalated` |
| `ANALYZE` | ❤️ | Nội dung phân tích | `ops.header` |
| `STATUS` | ❤️ | Trạng thái hoặc câu hỏi thiếu target | `ops.header` |
| `LIFECYCLE_QUESTION` | Không | Câu hỏi xác nhận vòng đời | `ops.header` |
| `RELATED` | Không | Thông báo episode và link ticket mới | `ops.header` `followup.recurrence` |

Lỗi ghi GitHub (`ambiguous`, TG-24/TG-26) vẫn theo Business DOCS, gửi ở group xử lý với khối `notice.error`.

## Bảng: ops_message_fields

Mọi tin ở group xử lý mở đầu bằng khối `ops.header`, **bắt buộc** đủ các trường dưới đây để người trả lời biết đang nói về chủ đề nào:

Nếu không có link Telegram, dòng tin gốc là `Tin gốc: (group thường, không có link) · mã tin <message_id>`.

| Trường | Nguồn |
|---|---|
| Tên group nguồn | Tên chat Telegram trả về, code lấy |
| Link tin đầu chủ đề | Code dựng từ `chat_id` + `message_id`, không lấy từ model |
| Mã chủ đề | ID chủ đề trong DB |
| Người gửi | Tên hiển thị; xưng hô theo TG-28/TG-29, không đoán giới tính |

## Bảng: blocks

Danh mục mọi khối template rules dùng. `có` = đã có trong file template; `cần thêm` = phải thêm khi triển khai (Owner duyệt câu chữ).

| Khối template | File | Có sẵn |
|---|---|---|
| `notice.issue` | `group-replies.md` | `có` |
| `followup.updated` | `group-replies.md` | `có` |
| `question.fallback` | `group-replies.md` | `có` |
| `notice.rate_limited` | `group-replies.md` | `có` |
| `ops.header` | `group-replies.md` | `có` |
| `ask.object` | `group-replies.md` | `có` |
| `ask.observed` | `group-replies.md` | `có` |
| `ask.work` | `group-replies.md` | `có` |
| `ask.problem` | `group-replies.md` | `có` |
| `notice.hold_escalated` | `group-replies.md` | `có` |
| `followup.recurrence` | `group-replies.md` | `có` |
