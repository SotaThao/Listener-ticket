{{#block notice.issue}}
Em đã ghi nhận nội dung này và tạo ticket: {{issue_link}} rồi nhé 😊
{{/block}}
{{#block notice.needs_human}}
Em đã ghi nhận nội dung này rồi nhé. Nhờ Anh/Chị hoặc Owner bổ sung/xác nhận thông tin ngay trong group để làm rõ yêu cầu.
{{#if issue_link}}
Em đã tạo ticket để tiện theo dõi: {{issue_link}}
{{/if}}
Thông tin cần làm rõ:
• {{@questions}}
{{/block}}
{{#block notice.error}}
Chưa xác nhận được kết quả tạo GitHub issue cho nội dung này. Nhờ Owner kiểm tra repo và hỗ trợ ngay trong group. Issue có thể đã được tạo; kiểm tra repo trước khi thử tạo lại.
{{/block}}
{{#block notice.rate_limited}}
Chưa tạo GitHub issue vì đã đạt giới hạn tạo issue. Nhờ Owner kiểm tra và xác nhận cách xử lý ngay trong group.
{{/block}}
{{#block notice.context_pending}}
Em chưa phân tích được phạm vi tin này lúc này nên chưa gộp vào ticket nào. Anh/Chị trả lời tin gốc này hoặc phản hồi này, nêu đầy đủ yêu cầu cần xử lý (hoặc xác nhận đây chỉ là trao đổi) giúp Em nhé.
{{/block}}
{{#block question.fallback}}
Anh/Chị muốn team xử lý gì, ở màn hình/chức năng nào và kết quả mong muốn là gì?
{{/block}}
{{#block question.legacy_pending}}
Anh/Chị muốn xử lý phần nào trong nội dung này và kết quả mong muốn là gì?
{{/block}}
{{#block question.technical_context}}
Em đã lưu các tin vừa nhận. Lượt phân tích đang lỗi nên chưa tạo được ticket.
{{/block}}
{{#block followup.updated}}
Em đã bổ sung thông tin vào ticket rồi nhé 😊
{{/block}}
{{#block followup.split}}
Em đã tách các yêu cầu thành từng ticket riêng rồi nhé 😊
{{/block}}
{{#block followup.recurrence}}
Em đã tạo ticket cho lần lỗi tái diễn và gắn ticket liên quan rồi nhé 😊
{{/block}}
