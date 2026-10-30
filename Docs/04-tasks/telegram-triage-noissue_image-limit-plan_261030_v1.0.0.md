# Telegram triage — Plan: giới hạn ảnh theo từng ticket (HIGH)

**Version**: v1.0.0 · **Ngày**: 2026-10-30 · **Owner**: AlexChu · **Planner**: `nx-planner` (Claude) · **Trạng thái**: Owner duyệt 2026-10-30 — Q1 = A (rào D7 đủ 5 điều kiện), Q2 = min(5, `max_per_case`), Q3 = PR sau · executor Codex `gpt-6.1-terra` · high
**Branch**: `bug/noissue_image-batch-limit` (từ `main` `4b01cb3`) · **Theo dõi**: [Kế hoạch P2](telegram-triage-noissue_tasks_261029_v1.0.0.md) — lỗi HIGH · **GATE 0b** (production)

## 1. Root cause

- `attachments.py:449-456` `PreparedImages` gọi `_download` một lần cho **cả đợt vision**. Dòng `:243-246` cắt `max_per_case` (mặc định 5) trên toàn đợt.
- `process(prepared=…)` (`:212-219`) chỉ lấy ảnh của case từ đợt đó. Hệ quả:
  - Ảnh của case bị cắt vì đợt thì **không bao giờ upload**, và ticket vẫn ghi "vượt giới hạn" dù case có ≤5 ảnh.
  - Ảnh của case nằm ngoài đợt bị bỏ **im lặng**.
- Log `Images found=` chỉ có ở đợt vision, nên con số là của cả đợt, không phải của case.
- Ba nơi tạo đợt: autonomous `autonomous.py:151-165`, legacy `pipeline.py:922`, followup `followups.py:472`. Followup đã tự `clarify` khi có quá 5 ảnh mới (`:475`), nên lỗi không lọt ra ở đó.
  - Ví dụ autonomous: chủ đề A có 3 ảnh, B có 4 ảnh ⇒ B bị ghi chú "(2 ảnh) vượt giới hạn" và chỉ upload 2.
- `_vision_unavailable` ở `autonomous.py:216-217` hạ NEW → CLARIFY khi một ảnh của group không có trong đợt. Vì ngân sách vision đang dùng chung `max_per_case`, việc cắt theo đợt có thể làm mất ticket.
- Transport giới hạn vision ở 5 ảnh (`ai_transport.py:205-206`).

## 2. Thiết kế (2 commit, rollback D7 riêng được)

**Commit 1: upload theo ticket, ngân sách vision tách riêng.** Không đổi template.

1. `attachments.py`:
   - `VISION_MAX_IMAGES = 5`.
   - Lý do nội bộ `vision_budget`, không bao giờ in ra ticket.
   - `_download(..., limit=None, limit_reason=..., reuse=None, label=...)`:
     - Ảnh đã có trong đợt ⇒ dùng lại, **không tải lại**.
     - Ảnh có kết quả thật (size/type/lỗi) ⇒ chép nguyên, không thử lại.
     - Còn lại ⇒ tải mới với cùng các kiểm như hiện nay.
   - `PreparedImages` dùng `limit=min(5, max_per_case)`, nhãn log `Vision batch`.
   - `process()` nhánh prepared dùng `_download(..., reuse=prepared)`:
     - Ưu tiên ảnh model đã xem.
     - Mỗi ticket tối đa `max_per_case` ảnh **của chính nó**.
     - Ghi chú "vượt giới hạn" chỉ khi case có > N ảnh.
     - Mỗi case một dòng log `Images found=`.
2. `autonomous.py:148-150`: chỉ sửa comment cho đúng thứ tự ưu tiên (tin hiện tại → exact-reply → read_context lấp slot trống).
3. `followups.py`: không sửa.

**Commit 2: D7 có rào, ghi chú từ template (TG-33).**

4. `templates/github-ticket/_common.md`: block `note.unseen_images` — "Model chưa xem {{count}} ảnh đính kèm trong lượt phân tích; người xử lý cần mở các ảnh này để kiểm tra."
5. `ticket_templates.py`: thêm entry CATALOG `note.unseen_images` (`count:T`).
6. `attachments.process()`: case có ảnh không nằm trong đợt đã xem ⇒ thêm ghi chú render từ template.
7. `pipeline.py`: thêm `_unseen_tolerated(messages)`. Hàm trả True **chỉ khi đủ 5 điều kiện**:
   1. Provider hỗ trợ vision.
   2. Model đã nhận **≥1 ảnh** của group.
   3. Group có **chữ thật** (bỏ chữ command).
   4. Ảnh chưa xem chỉ vì **ngân sách**, không phải lỗi tải/size/type.
   5. Số ảnh của group ≤ `max_per_case` (để ticket đính được đủ).
8. `autonomous.py:216`: chỉ hạ NEW → CLARIFY khi `_vision_unavailable` **và không** `_unseen_tolerated`.
   - Ca nguy hiểm "ảnh duy nhất mang nội dung, chữ chỉ 'cái này lỗi'": model xem 0 ảnh ⇒ điều kiện 2 không đạt ⇒ vẫn CLARIFY.
9. Tài liệu:
   - Business DOCS: TG-14, TG-15, dòng Q3, changelog v1.14.0, trace JSON + `runtime_inventory`.
   - Rulebook: TG-14.2 (giới hạn tính **mỗi ticket**), TG-15.7 (ngân sách vision và rào D7).

## 3. Test (file mới `listener/tests/test_image_case_limit.py`)

| # | Kịch bản | Kỳ vọng |
|---|---|---|
| T1 | Đợt 8 ảnh: 5 ảnh lịch sử + 3 ảnh case A | A upload 3, không ghi chú; downloader thêm đúng 3 lượt |
| T2 | Case 7 ảnh, max 5 | Upload 5, ghi chú "(2 ảnh)"; có prepared thì thêm ghi chú unseen 2 |
| T3 | Ảnh case ngoài đợt vision | Tải riêng, upload; ảnh đã prepared không tải lại |
| T4 | Ảnh lỗi trong đợt | Chép nguyên skip/failure, không tải lại |
| T5 | Log | `Images found=` theo case; đợt mang nhãn `Vision batch` |
| T6 | Autonomous A(3) + B(4) | 2 ticket, A 3 ảnh, B 4 ảnh, B không bị hạ, B có ghi chú unseen 2 |
| T7 | D7 phủ định (a–e) | Vẫn CLARIFY khi: model xem 0 ảnh · group >5 ảnh · không có chữ · ảnh lỗi tải · provider từ chối ảnh |
| T8 | Followup | `process([incoming], prepared)` tải riêng; >5 ảnh mới vẫn clarify |
| T9 | Template | `validate()` PASS; render đúng; thiếu block ⇒ `TemplateError`; golden không đổi |

Sửa `test_attachment_note_scope.py:16-25`: kỳ vọng chuyển sang "(1 ảnh)", `(a, b) = (1, 5)`.

## 4. Scope

- **Được sửa:**
  - `attachments.py`
  - `autonomous.py` (comment + dòng 216)
  - `pipeline.py` (thêm `_unseen_tolerated`)
  - `ticket_templates.py` (1 entry)
  - `templates/github-ticket/_common.md`
  - `tests/test_attachment_note_scope.py`
  - `tests/test_image_case_limit.py` (mới)
  - Business DOCS, Rulebook
- **Cấm sửa:**
  - `listener/rules/**`
  - `followups.py`, `classifier.py`, `thinking.py`, `ai_transport.py`, `config.py`, `trace.py`, `*store.py`
  - config example, `pyproject.toml`, prompts, `tests/fixtures/template_golden.json`
  - Không thêm dependency, config key hay migration.

## 5. Rủi ro và rollback

- Upload nhiều ảnh hơn (ảnh trước đây bị bỏ nhầm) ⇒ chạm `max_per_hour` sớm hơn.
- Thêm lượt tải Telegram lúc tạo ticket.
- D7: một số chủ đề trước đây hỏi lại nay thành ticket.
- Template mới phải cài cùng release; thiếu block thì bot không khởi động.
- Không đổi DB, config hay migration.
- **Rollback:**
  - Revert commit 2 ⇒ bỏ D7, giữ phần sửa upload.
  - Revert cả hai ⇒ cài lại wheel cũ.

## 6. Owner chốt

| # | Câu hỏi | Đề xuất |
|---|---|---|
| Q1 | Rào D7 | A: đủ 5 điều kiện |
| Q2 | Ngân sách vision | min(5, `max_per_case`) |
| Q3 | Ảnh read_context không có reply (LOW-4) | PR sau — đang có test hợp đồng khoá hành vi cho yêu cầu phân tích |
