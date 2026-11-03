<!-- rule-file: lifecycle · version 2 · status ACTIVE · audience: code -->
# Vòng đời ticket

Owner AlexChu duyệt ACTIVE ngày 2026-11-03: “Duyệt lifecycle.md và analysis-status.md sang ACTIVE, tiếp tục chuẩn bị môi trường và kiểm tra preflight để test thực tế trên Telegram cho tôi.”
Phê duyệt hai file nguồn không thay phiếu preflight, cài đặt hay GO vận hành.
Chỉ host xác lập target từ Reply thật cùng chat, chứng cứ ledger và issue canonical
thuộc repository cấu hình. Đọc trạng thái hiện tại bằng trusted GET trước quyết định
và trước mỗi lần tạo related bị trì hoãn/khôi phục. Không đọc được thì HOLD.

| Điều kiện đã chứng minh | Kết quả |
|---|---|
| Open, cùng sự cố | Bổ sung ticket hiện hữu |
| Closed, bằng chứng cũ hoặc lỗi chưa từng hết | Bổ sung ticket cũ, giữ closed |
| Closed, đã xác nhận chạy tốt rồi tái diễn | Tạo episode mới liên quan parent qua reservation và verified readback |
| Success evidence đã dùng cho episode mới nhất | Không tạo trùng; đối chiếu episode hoặc hỏi |
| Xác nhận chạy tốt mới sau episode mới nhất rồi tái diễn | Có thể tạo episode tiếp theo sau khi kiểm lại ranh giới |
| Không rõ chưa từng hết hay tái diễn | Hỏi đúng sự khác biệt, không tạo hoặc reopen |

Closed hoặc lời “đã xử lý” không chứng minh đã chạy tốt. Evidence cần provenance,
thứ tự thời gian và nội dung trích dẫn được host kiểm chứng. Không tự reopen.
Sau related create, nguồn lỗi mới và notice episode trỏ về ticket mới; nguồn cũ
giữ parent. POST chưa rõ phải được đối chiếu, không tự retry.

Ví dụ: parent closed, reply “vẫn lỗi từ trước” → bổ sung parent. Reply “đã chạy
tốt hôm qua, hôm nay lại lỗi” có evidence hợp lệ → episode liên quan; dùng lại
cùng xác nhận đó lần nữa → không tạo thêm. Không xác định được mốc → hỏi.

## Bảng: disposition

| Bằng chứng host đã kiểm | Quyết định |
|---|---|
| `evidence` | `APPEND_CLOSED` |
| `recurrence` | `RELATED` |
| `reused_success` | `QUESTION` |
| `unclear` | `QUESTION` |
| `unavailable` | `HOLD` |

## Bảng: explicit_evidence

| Xác nhận trực tiếp | Bằng chứng |
|---|---|
| `Lỗi này chưa từng hết.` | `evidence` |

## Bảng: clarification

| Khóa | Văn bản |
|---|---|
| `question` | `Anh/Chị xác nhận giúp: A — lỗi chưa từng hết; B — đã chạy tốt sau ticket gần nhất rồi lỗi tái diễn? Vui lòng reply A hoặc B.` |
| `evidence_answer` | `A` |
| `recurrence_answer` | `B` |
| `evidence_context_prefix` | `Bổ sung bằng chứng cùng lỗi:` |
