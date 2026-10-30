{{#block original}}
<details><summary>💬 Tin gốc ({{count}} tin) — {{senders}}, {{time}}</summary>

{{messages}}

</details>
{{/block}}
{{#block footer}}
---
<sub>Phân loại: {{provenance}} · tạo tự động bởi tg-triage</sub>
{{/block}}
{{#block provenance.manual}}
xác nhận thủ công
{{/block}}
{{#block provenance.rule}}
tầng 1 lệnh
{{/block}}
{{#block provenance.model}}
tầng 2 {{model}}, confidence {{confidence}}
{{/block}}
{{#block provenance.case}}
case {{count}} tin
{{/block}}
{{#block confidence.none}}
không có
{{/block}}
{{#block original.open_link}}
mở trên Telegram
{{/block}}
{{#block original.key_mark}}
tin chính
{{/block}}
{{#block original.hidden}}
(còn {{count}} tin khác trong case, xem nhóm Telegram)
{{/block}}
{{#block original.image_only}}
(chỉ có ảnh)
{{/block}}
{{#block original.document_only}}
(chỉ có tài liệu)
{{/block}}
{{#block original.unknown_sender}}
(không rõ)
{{/block}}
{{#block document.fallback_label}}
tài liệu {{index}}
{{/block}}
{{#block fallback.summary}}
(không có tóm tắt)
{{/block}}
{{#block fallback.summary_needs_human}}
(agent không chắc chắn, cần người đọc)
{{/block}}
{{#block fallback.needs_clarity}}
Chưa đủ thông tin để phân loại, cần người đọc xem lại.
{{/block}}
{{#block note.unseen_images}}
Model chưa xem {{count}} ảnh đính kèm trong lượt phân tích; người xử lý cần mở các ảnh này để kiểm tra.
{{/block}}
