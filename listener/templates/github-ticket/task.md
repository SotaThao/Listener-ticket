{{#block body}}
### 📝 Tóm tắt
{{summary}}

### 🛠 Việc cần làm
{{work|(chưa có thông tin)}}
{{#if reason}}
**Lý do:** {{reason}}
{{/if}}
{{#if scope_in scope_out}}
{{#if scope_in}}
{{#if scope_out}}
**Phạm vi:** Làm — {{scope_in}} · Không làm — {{scope_out}}
{{#else}}
**Phạm vi:** Làm — {{scope_in}}
{{/if}}
{{#else}}
**Phạm vi:** Không làm — {{scope_out}}
{{/if}}
{{/if}}
{{#if due}}
**Hạn / người được nhắc:** {{due}}
{{/if}}
{{#if task_evidence}}

### 🖼 Minh họa / Evidence (đầu vào)
{{task_evidence}}
{{/if}}

### 🏁 Xong khi
- [ ] {{@done}}
- [ ] Đính kèm evidence kết quả (ảnh/link/log) khi đóng issue
{{#if questions}}

### ❓ Cần làm rõ
- {{@questions}}
{{/if}}

{{original}}

{{footer}}
{{/block}}
{{#block question.work}}
Việc cần làm là gì?
{{/block}}
