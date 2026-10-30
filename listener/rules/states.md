<!-- rule-file: states · version 2 · status DRAFT · audience: code -->
# Trạng thái kết quả

Danh sách **đóng**: mỗi chủ đề nhận đúng một mã dưới đây, do code tính theo `readiness.md`. Model không trả trạng thái. Bot nói ở đâu: [`channels.md`](channels.md).

## Bảng: states

| Mã | Nghĩa | Ticket | Group gốc | Group xử lý | Pending | Rule nghiệp vụ |
|---|---|---|---|---|---|---|
| `READY` | Việc đủ rõ | Tạo mới | ❤️ khi nhận, 👌 khi có số ticket | Link ticket | Đóng | TG-08, TG-11 |
| `APPEND` | Reply thật vào ticket đang open | Comment vào ticket đó | ❤️ | Link ticket đã bổ sung | Đóng | TG-16, TG-18 |
| `NEED_INFO` | Là việc nhưng thiếu trường bắt buộc | Không | ❤️ | Một tin hỏi theo `questions.md` | Mở | TG-08, TG-12 |
| `MEDIA_ONLY` | Chỉ có ảnh/tài liệu, không có chữ | Không | Không | Không | Mở | TG-15 |
| `CHAT` | Không phải việc | Không | Không | Không | Không | TG-09 |
| `HOLD` | Bot không trích được dữ kiện (model lỗi, sai schema, hết quota) | Không | Không | Chỉ báo Owner khi `ESCALATED` | Mở | TG-08 |

## Bảng: pending_states

Vòng đời pending của một chủ đề. Mỗi dòng: đang ở trạng thái nào, gặp sự kiện gì, chuyển sang đâu.

| Từ | Sự kiện | Sang |
|---|---|---|
| `OPEN` | Có tin hợp lệ nối vào chủ đề (`questions.md` · `answer_link` = `LINK`, hoặc tin cùng chủ đề ở group gốc) | `ANSWERED` |
| `ANSWERED` | Chạy lại `readiness.md` từ R1 | `CLOSED` nếu ra `READY`/`APPEND`/`CHAT`; `OPEN` nếu ra `NEED_INFO`/`MEDIA_ONLY`/`HOLD` |
| `OPEN` | Quá `pending_expiry_days` ngày từ tin cuối của chủ đề | `EXPIRED` |
| `OPEN` | Chủ đề `HOLD` thử lại quá `hold_max_retries` lần | `ESCALATED` |
| `ESCALATED` | Owner xử lý xong | `CLOSED` |

`EXPIRED` và `CLOSED` là trạng thái cuối: bot không nhắn gì, chỉ ghi nhật ký. Tin mới sau đó được xét như chủ đề mới.

## Bảng: limits

| Khóa | Giá trị | Nghĩa |
|---|---|---|
| `pending_expiry_days` | `7` | Số ngày giữ pending không có tin mới |
| `hold_max_retries` | `3` | Số lần thử trích lại khi `HOLD` trước khi báo Owner |
