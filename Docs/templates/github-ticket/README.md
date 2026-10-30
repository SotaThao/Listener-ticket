# Mẫu nội dung GitHub ticket và phản hồi

Nguồn mẫu chạy cùng listener nằm tại [`listener/templates/github-ticket/bug.md`](../../../listener/templates/github-ticket/bug.md) cùng sáu tệp bên cạnh. Các tệp Markdown được đóng gói theo release; không có thư mục override. Đổi mẫu qua PR, chạy kiểm thử, phát hành rồi chạy `check-config` và so sánh fingerprint trên bản cài.

Chỉ nội dung giữa `{{#block NAME}}` và `{{/block}}` được runtime sử dụng. `{{slot}}` chèn giá trị; `{{slot|giá trị mặc định}}` dùng mặc định khi rỗng. `{{#if a b}}` chọn nhánh khi ít nhất một giá trị không rỗng; `{{#else}}` là nhánh còn lại. Dòng có `{{@list}}` lặp một lần cho mỗi phần tử, `{{@index}}` là số thứ tự từ 1. Không đặt nội dung người dùng vào chính mẫu.

| Mẫu | Slot chính trong `body` |
|---|---|
| `bug.md` | summary, environment, steps, expected, actual, evidence, notes, questions, original, footer |
| `feedback.md` | summary, problem, user_story, proposal, alternatives, acceptance, context, questions, original, footer |
| `task.md` | summary, work, reason, scope_in, scope_out, due, task_evidence, done, questions, original, footer |
| `needs-human.md` | summary, evidence, questions, original, footer |

Khi soạn tay, đọc block `body` tương ứng và theo đúng thứ tự, nhãn, điều kiện và dấu đầu dòng. Chọn nhánh `if` theo dữ kiện đã xác minh; bỏ dòng lặp khi danh sách rỗng. `original` và `footer` có khuôn trong `_common.md`; `comments.md` và `group-replies.md` chứa các block bổ sung tương ứng. Liên kết và nội dung người dùng phải được xử lý theo quy tắc TG-31.
