{{#block body}}
### 📝 Tóm tắt
{{summary}}

### 🐞 Lỗi
**Môi trường:** {{environment|(chưa có thông tin)}}
{{#if steps}}
**Các bước tái hiện:**
{{@index}}. {{@steps}}

{{#else}}
**Các bước tái hiện:** (chưa có thông tin)
{{/if}}
**Mong đợi:** {{expected|(chưa có thông tin)}}
**Thực tế:** {{actual|(chưa có thông tin)}}

### 🖼 Minh họa / Evidence
{{evidence|(chưa có thông tin)}}
{{#if notes}}

{{@notes}}
{{/if}}
{{#if questions}}

### ❓ Cần làm rõ
- {{@questions}}
{{/if}}

{{original}}

{{footer}}
{{/block}}
{{#block question.environment}}
Lỗi xảy ra trên môi trường nào (app/bản build, thiết bị, trình duyệt)?
{{/block}}
{{#block question.steps}}
Các bước để tái hiện lỗi là gì?
{{/block}}
{{#block question.expected_actual}}
Kết quả mong đợi và kết quả thực tế?
{{/block}}
{{#block question.evidence}}
Gửi thêm ảnh/video màn hình lỗi
{{/block}}
