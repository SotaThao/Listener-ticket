{{#block body}}
### 📝 Tóm tắt
{{summary}}

### 💡 Vấn đề / nhu cầu
{{problem|(chưa có thông tin)}}
{{#if user_story}}
**User story:** {{user_story}}
{{/if}}
{{#if proposal}}
**Đề xuất:** {{proposal}}
{{/if}}
{{#if alternatives}}
**Phương án khác:** {{alternatives}}
{{/if}}
{{#if acceptance}}

### ✅ Tiêu chí chấp nhận (nháp)
- [ ] {{@acceptance}}
{{/if}}
{{#if context}}

### 🧭 Ngữ cảnh
{{context}}
{{/if}}
{{#if questions}}

### ❓ Cần làm rõ
- {{@questions}}
{{/if}}

{{original}}

{{footer}}
{{/block}}
{{#block question.problem}}
Vấn đề / nhu cầu là gì?
{{/block}}
{{#block question.user_story}}
Là vai trò nào, bạn muốn hành động gì để có lợi ích gì?
{{/block}}
