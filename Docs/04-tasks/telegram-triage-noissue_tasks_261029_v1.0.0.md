# Kế hoạch cấu trúc lại listener (nháp để Owner duyệt)

**Version**: v1.0.0 · **Date**: 2026-10-29 (Asia/Saigon) · **Owner**: AlexChu · **Author**: Claude · **Status**: Draft — chưa thực hiện
**Đầu vào**: [Audit listener](../02-engineering/telegram-triage-noissue_listener-audit_261029_v1.0.0.md) · PR #79, #81 (chưa merge, chưa deploy)

## 0. Nguyên tắc nền (Owner, 2026-10-29)

> **Mọi quyết định của bot phải dựa trên một file markdown cụ thể với các trạng thái được liệt kê cụ thể, không dựa hoàn toàn vào model. Nâng cấp model chỉ giúp cải thiện chất lượng hoặc đẩy nhanh tiến độ, không đổi quy tắc.**

Hệ quả kiến trúc (mở rộng mẫu PR #81 từ "câu chữ" sang "quyết định"):

```
 Tin Telegram ──► MODEL: trích DỮ KIỆN có nguồn            ──► CODE: áp QUY TẮC markdown ──► Hành động
                  (đối tượng, hành vi, mong muốn, loại,          (bảng điều kiện → trạng thái)    (tạo / hỏi / bổ sung / bỏ qua)
                   ảnh nào thuộc tin nào, source_ids)              listener/rules/*.md
                                                                   validate lúc khởi động, fingerprint,
                                                                   trace trong Business DOCS
```

- Model **không** trả kết luận cuối (NEW/CLARIFY, actionable); model trả dữ kiện đã điền + nguồn. Thiếu dữ kiện ⇒ quy tắc markdown quyết định hỏi gì.
- Quy tắc là **bảng điều kiện → trạng thái** trong markdown (người đọc được, máy đọc được), cùng cơ chế với template TG-33: đóng gói theo release, sai ⇒ không khởi động.
- Đổi model chỉ đổi chất lượng trích dữ kiện; regression test chạy trên bảng dữ kiện mẫu, không phụ thuộc model.

### Các quyết định hiện do model phán (cần chuyển sang file quy tắc)

| Quyết định | Hiện tại | Đích |
|---|---|---|
| Đủ rõ → tạo ticket hay hỏi (TG-08) | Model trả `actionable`, `confidence`, `missing_info`; code kiểm cờ (`autonomous.py:40-43`) | `rules/readiness.md`: checklist bắt buộc theo loại bug/task/feedback; model chỉ điền trường |
| Chat hay việc, loại bug/task/feedback (TG-07, TG-09) | Model chọn `category` | `rules/classification.md`: định nghĩa + dấu hiệu theo dữ kiện; model trích dữ kiện |
| Hỏi gì khi thiếu (TG-12) | Model viết câu hỏi | Câu hỏi theo trường thiếu trong `readiness.md` (đã có mẫu ở template) |
| Gộp/tách chủ đề (TG-05/06) | Model chia partition | Phần cứng (Reply thật, quyền nguồn) đã ở code; phần ngữ nghĩa ghi tiêu chí trong `rules/topics.md` |
| Vòng đời: bổ sung / tái diễn / hỏi (Vòng đời ticket) | Một phần code (`lifecycle.py`), một phần model | `rules/lifecycle.md`: bảng trạng thái issue × bằng chứng → hành động |
| Ảnh model chưa xem (D7) | Code ép hỏi lại | Một dòng trong `readiness.md` |
| Ưu tiên (priority) | Model | `rules/priority.md` hoặc bỏ nếu không dùng |

### Luồng chuẩn (Owner chốt thứ tự, 2026-10-29)

```
0. Nạp + kiểm quy tắc, template, prompt, cấu hình nền tảng  (sai ⇒ không khởi động)
1. Bot tổng hợp tin theo quy tắc gom (Reply thật, gói context)
2. Gửi model: CHỈ trích dữ kiện (danh sách trường sinh từ quy tắc)
3. Code áp quy tắc → trạng thái READY / NEED_INFO / MEDIA_ONLY / CHAT
4. Chọn template theo loại + trạng thái + nền tảng đích
5. Tạo / bổ sung ticket qua adapter nền tảng (GitHub, Jira, Azure…)
6. Reply group theo template
```

### Nền tảng ticket linh hoạt (Owner, 2026-10-29: "tạo 1 hệ thống linh hoạt có thể thay đổi nền tảng tạo ticket")

```
 Bot (trạng thái READY + nội dung) ──► TicketPlatform (giao diện chung)
                                         ├─ create(ticket)        → mã + link (đọc lại xác nhận)
                                         ├─ append(ticket_id, …)  → bổ sung
                                         ├─ read(ticket_id)       → trạng thái open/closed, nội dung, nhãn
                                         └─ attach(file)          → link ảnh/tài liệu
                                                │
                     ┌──────────────────────────┼──────────────────────────┐
                 GitHub adapter            Jira adapter               Azure DevOps adapter
                 (có sẵn, tách ra)          (thêm khi cần)             (thêm khi cần)
```

- **Chọn nền tảng bằng cấu hình**, không sửa code: `[ticket] platform = "github" | "jira" | "azure"` cùng thông tin kết nối từng nền tảng.
- **Ánh xạ nghiệp vụ → nền tảng nằm trong markdown** `listener/platforms/<nền tảng>.md`: loại ticket → issue type/label, ưu tiên → field, trạng thái open/closed, định dạng nội dung (Markdown / Jira ADF / HTML). Đúng nguyên tắc §0: đổi ánh xạ không cần sửa code.
- **Template theo định dạng nền tảng**: giữ một bộ template nội dung chung; adapter chuyển định dạng (Markdown → ADF/HTML) bằng code, không để model làm.
- **Giữ nguyên bảo đảm hiện có cho mọi nền tảng**: ghi một lần (reserve → gửi → đọc lại), không tự gửi lại khi kết quả không rõ (TG-24), xác minh đúng dự án/repo đích (TG-17), marker nguồn để bổ sung đúng ticket.
- **Dữ liệu lưu**: thay `issue_number` gắn cứng GitHub bằng cặp `(platform, ticket_key)` (vd `github:#82`, `jira:SUP-123`); migration chỉ thêm cột, giữ dữ liệu cũ.
- **Thứ tự**: tách GitHub thành adapter đầu tiên (không đổi hành vi, golden giữ nguyên) → cấu hình chọn nền tảng → thêm Jira/Azure khi có dự án thật để test.

## 1. Mục tiêu Owner chốt (2026-10-29)

| # | Mục tiêu | Đo bằng |
|---|---|---|
| D | **Làm rõ Business DOCS** | Một nguồn quy tắc duy nhất, đọc được; Agent yếu tra một chỗ là ra |
| G3 | **Ticket thật tốt hơn** | Ticket trên production có đủ link + ảnh đúng topic; ghi chú ảnh giải thích được |
| G1 | **Bàn giao an toàn** | Mọi PR listener tự chạy 940 test + drift check trong CI |
| G4 | **Code gọn** | Bớt trùng lặp, bớt đường code không dùng, module nhỏ hơn — không đổi hành vi |

Tạm hoãn: G2 (Owner tự sửa mọi câu chữ — W1/W2/W3 trong audit).

## 2. Thứ tự đề xuất và lý do

```
P1 Làm rõ Business DOCS ──┐  (chỉ tài liệu, không đụng runtime)
P2 Ship PR #79/#81 + fix ảnh ─┤  (song song với P1 được: khác file)
                          ▼
P3 CI cho listener  ──────► P4 Code gọn (W5 → W4 → W6), mỗi bước một PR nhỏ
```

- **P1 trước/đồng thời P2**: quy tắc rõ thì mọi việc sau có chuẩn để kiểm; không đụng code nên không cản ship.
- **P2 sớm**: mọi cải tiến (template, link evidence) chưa tới người dùng; G3 chỉ đạt khi deploy.
- **P3 trước P4**: refactor bot production mà CI không chạy test là đánh cược; CI là lưới an toàn của G1 và điều kiện của G4.
- **P4 theo thứ tự rủi ro tăng dần** (strangler, không big-bang): gộp trùng → bỏ legacy (cần quyết định) → tách module.

## 3. Chi tiết từng giai đoạn

### P1 — Làm rõ Business DOCS (mục tiêu D, G1) + quy tắc quyết định dạng markdown (§0)
**Lát đầu tiên (ưu tiên cao nhất): [`listener/rules/README.md`](../../listener/rules/README.md) cho TG-08 "đủ rõ"** (Owner chốt vị trí `listener/rules/` ngày 2026-10-29). Đã tách thành: `README.md` (quy tắc nạp rules vào bot và model), `states.md`, `facts.md`, `readiness.md`, `questions.md`, `examples.md` — tất cả DRAFT, chờ duyệt; còn mở D7 và ngưỡng confidence — checklist theo loại, trường bắt buộc, câu hỏi theo trường thiếu, xử lý ảnh model chưa xem (D7), bộ ví dụ đạt/không đạt từ ticket thật (#46, #47, #82). Đổi model ingress sang trả dữ kiện; code áp bảng. Các file quy tắc còn lại làm theo từng lát sau khi lát đầu chạy ổn.

Vấn đề hiện tại: ~1.700 dòng nhưng ~1.200 là JSON trace; quy tắc trộn lý do nghiệp vụ, chi tiết kỹ thuật, debug và lịch sử; Rulebook lặp lại quy tắc (hai nguồn).
- Tách **JSON trace** ra file riêng (`…_trace.json`), sửa `check-telegram-triage-policy.py` đọc file đó.
- Mỗi TG viết lại theo khuôn ngắn: **Quy tắc (MUST/MUST NOT) · Vì sao · Ví dụ · Ngoại lệ**; chi tiết kỹ thuật (byte, giây, tên bảng) chuyển sang Engineering doc, TG chỉ trỏ tới.
- Gắn nhãn **[legacy] / [autonomous] / [chung]** cho từng TG.
- Gộp **Rulebook vào Business DOCS** (hoặc Rulebook thành bảng tra cứu sinh từ Business DOCS) ⇒ còn một nguồn quy tắc.
- Phần "Bộ nhớ, truy vấn, debug" chuyển sang runbook vận hành (02-engineering).
- Sửa `POLICY_VERSION` lỗi thời; thêm "TG = Telegram triage" vào thuật ngữ.
- Verify: checker PASS, mọi link nội bộ đúng, không đổi nghĩa quy tắc (đối chiếu từng TG cũ ↔ mới).

### P2 — Ship và sửa evidence ảnh (G3)
- Merge PR #79 → main, PR #81 → main; build release qua `deploy/build_wheel.py`; cài THINKPAD theo `deploy/README.md`; `check-config` + so fingerprint; readback service. *(Owner thực hiện bước cài đặt.)*
- Fix: chỉ ghi chú ảnh bị bỏ thuộc đúng topic của ticket (`attachments.py:213`); log đếm ảnh theo `message_id` (không ghi nội dung).
- Sửa 3 LOW của review PR #81 (IPv6 host, trùng link IDN, comment cũ).
- Verify: golden + test mới; ticket thật sau deploy có link evidence (quan sát tin mới, không replay).

### P3 — CI cho listener (G1)
- Workflow mới: `unittest` listener (Ubuntu + Windows, Python 3.11/3.12) + `check-telegram-triage-policy.py` + `test_telegram_triage_policy.py` + `test_reformat_telegram_issue.py`.
- Đánh dấu/ổn định test hẹn giờ đang thỉnh thoảng fail (`test_usage_fallback`).
- Không sửa console CI đang fail (việc riêng).

### P4 — Code gọn (G4), mỗi bước một PR, golden giữ nguyên
1. **W5 gộp trùng**: một hàm dựng URL issue; một đường gửi notice; một nguồn regex email/phone; một `CASE_STATUS`; xóa `NEEDS_CLARITY_LINE` không dùng.
2. **W4 bỏ legacy** *(Owner đã chốt; điều kiện: đã thử cài lại release cũ ở P2)*: chuyển `resume_case` sang đường autonomous; xóa ~650 dòng + `classify.md`, `case.md`, `topics.md`, `thinking-topics.md`; cập nhật TG đánh dấu [legacy].
3. **W6 tách module**: `pipeline.py` (985) và `classifier.py` (732) tách theo trách nhiệm; gỡ 7 vòng import lười; `store.py` giữ mixin nhưng gom schema về một chỗ.

## 4. Quyết định

**Đã chốt (Owner, 2026-10-29):**
1. **Bỏ đường legacy trong P4** (P4.2). Hệ quả:
   - Rollback không còn là tắt cờ `autonomous_enabled`; rollback = cài lại wheel release trước (theo `deploy/README.md`). P2 phải chứng minh quy trình cài lại release cũ chạy được **trước khi** làm P4.2.
   - P1 chỉ **gắn nhãn [legacy]** cho các TG/đoạn chỉ áp cho legacy, không viết lại chúng; P4.2 xóa chúng cùng code.
   - `resume_case` phải chuyển sang đường autonomous trước khi xóa `_analyze_case`.
2. **Gộp Rulebook vào Business DOCS**: một nguồn quy tắc; Rulebook cũ thành trang trỏ sang.

**Còn mở:**
3. **Deploy P2**: ai thực hiện và lịch nào (cần SSH/sudo trên THINKPAD).
4. Thứ tự P1 ∥ P2 rồi P3 → P4 có đúng ưu tiên không?

## 5. Ngoài phạm vi
G2 (W1 câu chữ, W2 prompt, W3 tham số vào config); console-ui, nailhub; console contract CI.
