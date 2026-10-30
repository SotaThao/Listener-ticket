<!-- rule-file: questions · version 2 · status DRAFT · audience: code -->
# Hỏi khi thiếu (trạng thái `NEED_INFO`)

Code chọn câu hỏi theo trường thiếu; **câu chữ** lấy từ khối template ghi ở cột "Khối template" (danh mục khối: `channels.md` · `blocks`). Model không viết câu hỏi. Câu hỏi **chỉ gửi ở group xử lý**, không bao giờ ở group gốc (`channels.md`).

Chỉ hỏi khi trạng thái là `NEED_INFO`. Không hỏi khi `MEDIA_ONLY`, `HOLD`, `CHAT`, `READY`, `APPEND`.

## Bảng: ask_order

Lấy các dòng có `intent` khớp và trường **chưa CÓ**, xếp theo "Thứ tự" tăng dần, bỏ khối trùng, lấy tối đa `max_fields_per_ask` khối, gộp thành **một tin**. Trường đã hỏi trong chủ đề này thì không hỏi lại (`max_asks_per_field`).

| Thứ tự | intent | Trường thiếu | Khối template |
|---|---|---|---|
| 1 | `bug` | `object` | `ask.object` |
| 2 | `bug` | `observed` | `ask.observed` |
| 1 | `task` | `object` | `ask.object` |
| 2 | `task` | `work` | `ask.work` |
| 1 | `feedback` | `problem` | `ask.problem` |

Feedback chỉ rơi vào `NEED_INFO` khi thiếu cả `problem` và `proposal`; khi đó hỏi `problem`.

## Bảng: fallback

| Khi | Khối template |
|---|---|
| `intent` không có dòng nào trong `ask_order` (gồm `unknown`), hoặc mọi trường thiếu đã hỏi | `question.fallback` |

## Bảng: limits

| Khóa | Giá trị | Nghĩa |
|---|---|---|
| `max_fields_per_ask` | `3` | Số khối hỏi tối đa trong một tin (TG-12) |
| `max_asks_per_field` | `1` | Mỗi trường chỉ hỏi một lần trong một chủ đề (chống hỏi lặp, TG-12) |

Không ai trả lời ⇒ pending hết hạn theo `states.md` · `limits` · `pending_expiry_days`, bot không nhắc.

## Bảng: answer_link

Tin ở group xử lý chỉ được nối về chủ đề theo bảng này. Đọc từ trên xuống, dòng đầu khớp là kết quả.

| # | Kênh | Là Reply vào | Pending | Kết quả |
|---|---|---|---|---|
| L1 | `ops` | Tin hỏi của bot đã gửi thành công cho chủ đề đó | `OPEN` | `LINK` |
| L2 | `ops` | Tin hỏi của bot đã gửi thành công cho chủ đề đó | `EXPIRED` `CLOSED` | `IGNORE` |
| L3 | `ops` | Tin khác của bot (link ticket, báo lỗi) hoặc tin của người | * | `IGNORE` |
| L4 | `ops` | Không Reply tin nào | * | `IGNORE` |

- `LINK`: tin trả lời (chữ và ảnh) được gộp vào chủ đề, `reply_target` = `bot_question`, chạy lại `readiness.md` từ R1.
- `IGNORE`: không xử lý, không tạo chủ đề mới, không trả lời.
- Group xử lý **không** phải group nguồn: tin mới ở đây không bao giờ thành ticket mới.
