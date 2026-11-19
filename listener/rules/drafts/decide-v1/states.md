<!-- rule-file: states · version 6 · status DRAFT · audience: code -->
# Trạng thái kết quả

Với `authority = "propose"` (mặc định), code tính trạng thái theo `readiness.md` và các luật mở rộng. Với `authority = "decide"`, trợ lý trả một trạng thái trong `decide/v1` cho mỗi nhóm sau khi code lọc đầu vào. Code kiểm bằng chứng và quyền trước khi thực thi. Bot phản hồi theo [`channels.md`](../../channels.md).

Danh sách trạng thái của executor gồm sáu mã nền trong bảng dưới và `APPEND_CLOSED`, `LIFECYCLE_QUESTION`, `RELATED` theo [`lifecycle.md`](../../lifecycle.md), cùng `ANALYZE`, `STATUS` theo [`analysis-status.md`](../../analysis-status.md). Với decide, `APPEND` và `APPEND_CLOSED` có thể dùng đích reply đã chứng minh hoặc ticket trong kết quả tìm kiếm của lượt hiện tại, đúng repo và trạng thái. Đích tìm kiếm còn cần quan hệ `duplicate` và độ tin cậy đạt ngưỡng.

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
| `OPEN` | Có tin hợp lệ nối vào chủ đề (`questions.md` · `answer_link` = `LINK`, hoặc Reply thật vào nguồn của topic ở group gốc, hoặc tin nguồn không Reply được `questions.md` · `source_followup` SF1–SF4 xác nhận đủ intent/pixel và commit) | `ANSWERED` |
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
