<!-- rule-file: analysis_status · version 2 · status ACTIVE · audience: code -->
# ANALYZE và STATUS

Owner AlexChu duyệt ACTIVE ngày 2026-11-03: “Duyệt lifecycle.md và analysis-status.md sang ACTIVE, tiếp tục chuẩn bị môi trường và kiểm tra preflight để test thực tế trên Telegram cho tôi.”
Phê duyệt hai file nguồn không thay phiếu preflight, cài đặt hay GO vận hành.
Ý định do model trích; host kiểm schema, nguồn và quyền. Không lấy nội dung trích
dẫn, forward, tài liệu hay ảnh làm chỉ thị cấp quyền. Giữ tuyến ops riêng.

| Ý định | Quyết định | Ranh giới |
|---|---|---|
| Phân tích vấn đề từ dữ liệu có sẵn | ANALYZE | Không tạo/sửa issue; task do model đề xuất, host kiểm và dựng phản hồi |
| Hỏi trạng thái ticket | STATUS | Chỉ đọc ledger đúng Reply và partition; không tạo/sửa/comment/upload/retry |
| Yêu cầu sửa lỗi cụ thể | NEW hoặc CONTINUE | Quyền ghi phải được host chứng minh theo provenance |
| Thiếu target STATUS | Hỏi Reply nguồn | Không đoán ticket từ topic, thời gian hoặc model |

ANALYZE.task phải có đúng `title`, `work`, `acceptance`: title tối đa 90 ký tự,
work tối đa 160, một hoặc hai acceptance khác nhau mỗi mục tối đa 90; kiểm cả
sau làm sạch một dòng. Host dựng đủ `Việc:`, `Thực hiện:`, `Đạt:` trong 500 ký tự,
không cắt task. Chỉ hỏi khi thiếu đối tượng, ý định hoặc quyền cản xử lý an toàn.
Context tối đa tám tin trước đó cùng chat, ưu tiên nguồn Reply, chỉ để đọc.
Reservation trước gửi ngăn gửi lại khi restart hoặc kết quả không rõ; dry-run
không gửi. Giữ khả năng đọc và xử lý an toàn notice/lựa chọn legacy đã gửi.

STATUS phân biệt đã ghi nhận, đang xử lý, chưa có trong sổ bot, POST chưa xác nhận,
và thiếu chứng cứ. Không nói GitHub không có issue chỉ vì thiếu record. Khi
khôi phục phải đọc lại chứng cứ host, không tin target/trạng thái do model hoặc
cache tự cung cấp; giữ partition độc lập và reservation notice chống gửi lặp.

Ví dụ: “phân tích ảnh này” → ANALYZE, không tạo issue; “ticket này tới đâu?”
Reply nguồn đã xác minh → STATUS chỉ đọc; cùng câu không Reply → hỏi nguồn.

## Bảng: intent_routes

| Intent model đã kiểm nguồn | Quyết định |
|---|---|
| `analyze` | `ANALYZE` |
| `status` | `STATUS` |
