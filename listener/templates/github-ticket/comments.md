{{#block enrichment}}
### Bổ sung từ hội thoại Telegram
{{summary}}

Phản hồi của người gửi:
{{reply_quote}}
{{#if reply_link}}

Nguồn phản hồi: {{reply_link}}
{{/if}}
{{/block}}
{{#block scope_confirmation}}
### Xác nhận phạm vi từ người gửi
{{source}} thuộc phạm vi ticket {{ordinal}}.
{{#if source_link}}
Tin nguồn: {{source_link}}
{{/if}}
Ảnh nguồn: {{@image_urls}}
Tài liệu nguồn: {{@document_urls}}
{{/block}}
{{#block scope_correction}}
### Điều chỉnh phạm vi sau phản hồi Telegram
Ticket này theo dõi yêu cầu thứ nhất: {{title}}
{{summary}}
ID tin nguồn: {{message_ids}}
{{#if questions}}
Cần làm rõ:
• {{@questions}}
{{/if}}
{{#if has_unresolved}}

Tin/ảnh chưa được gán, phạm vi chưa xác nhận (không mặc định thuộc ticket này):
• {{@unresolved}}
Ảnh chưa được gán: {{@unresolved_image_urls}}
Tài liệu chưa được gán: {{@unresolved_document_urls}}
{{/if}}
{{/block}}
{{#block append.source}}
Ticket nguồn: {{url}}
{{/block}}
{{#block append.recurrence}}
Lỗi tái diễn; ticket liên quan: {{url}}
{{/block}}
{{#block append.related}}
Ticket liên quan: {{url}}
{{/block}}
