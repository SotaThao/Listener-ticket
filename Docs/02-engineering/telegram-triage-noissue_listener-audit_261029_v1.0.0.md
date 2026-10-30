# Audit listener Telegram triage (trước khi cấu trúc lại)

**Version**: v1.0.0 · **Date**: 2026-10-29 (Asia/Saigon) · **Owner**: AlexChu · **Author**: Claude (plan-eng-review, bước audit) · **Baseline**: branch `feat/noissue_ticket-template-runtime` @ `76d6f7d` (PR #81)

**Status**: Báo cáo hiện trạng. **Chưa chọn giải pháp.** Owner yêu cầu audit và báo cáo trước khi quyết định hướng cấu trúc lại (2026-10-29). Mọi con số lấy từ đọc code tại baseline; phần "chưa đo" ghi rõ.

## 1. Tóm tắt

| Hạng mục | Đánh giá | Ghi chú ngắn |
|---|---|---|
| Kiến trúc | 🟠 Cần cải thiện | 2 đường xử lý (legacy + autonomous) cùng tồn tại; 5 module > 700 dòng; 7 vòng import chỉ gỡ bằng import lười |
| Tách nội dung khỏi code | 🟡 Một phần | Form ticket đã ra template (PR #81); ~80 câu chữ khác và ~6 khối prompt vẫn trong Python |
| Tham số nghiệp vụ | 🟠 Rải rác | ~25 hằng số (ngân sách model, giới hạn, emoji) cứng trong code; config chỉ lộ ~12 |
| Trùng lặp (DRY) | 🟠 Đáng kể | URL issue dựng ≥10 chỗ, 2 đường gửi notice, regex email/phone 3 nơi |
| Kiểm thử | 🟢 Mạnh, nhưng giòn | 49 file test, 940 ca, golden 352 ca; ~1.680 dòng test so khớp nguyên văn chữ tiếng Việt |
| Truy vết nghiệp vụ | 🟢 Tốt | Business DOCS + trace + checker hash; `POLICY_VERSION` lỗi thời |
| Hiệu năng / vận hành | ⚪ Chưa đo | Ngân sách 8 KB / 40 s / 2 lần gọi model mỗi lượt; chưa có số đo p95 |

Quy mô: `src/tg_triage` 31 file, **9.122 dòng**; `tests` **12.439 dòng** (49 file `test_*.py`).

## 2. Kiến trúc

### 2.1 Module lớn và trách nhiệm

| Module | Dòng | Vấn đề |
|---|---|---|
| `pipeline.py` | 985 | Vừa chứa toàn bộ đường legacy (~450 dòng) vừa chứa helper riêng mà `autonomous.py` gọi (`_safety`, `_plan_group`, `_run`, `_combine`) |
| `store.py` | 927 | Một lớp `Store` ghép 3 mixin (`followup_store`, `autonomy_store`, `topic_store`) trên một kết nối SQLite; bảng thinking tạo riêng ở `thinking.init_store` |
| `followups.py` | 825 | Hội thoại bổ sung/tách/tái diễn + ~30 câu hỏi/notice viết cứng + đường gửi notice riêng |
| `telegram_io.py` | 747 | Transport, worker, đăng nhập |
| `classifier.py` | 732 | Mọi lời gọi model (6 method) + mọi schema + prompt nhúng |

### 2.2 Phụ thuộc (vòng import chỉ gỡ bằng import lười)

```
pipeline ──lazy──► autonomous ──lazy──► pipeline
pipeline ──► followups ──lazy──► store ──mixin──► followup_store ──lazy──► store
classifier ──lazy──► autonomous._validate        classifier ◄──► followup_model / ingress_model
autonomy_store ──lazy──► classifier, thinking, group_notices
```

Hệ quả: thứ tự import mong manh, khó tách module, test phải import cả cụm.

### 2.3 Hai đường xử lý

```
              autonomous_enabled?
        ┌──────── false ────────┐         ┌──────── true (PRODUCTION THINKPAD) ────────┐
  pipeline.close_due_cases           telegram_io ChatCollector (30–60s)
  pipeline._process → _analyze_case   autonomous.process → classifier.analyze_ingress
  classifier.classify/split_topics    autonomy_store, ingress_model, autonomous.md
  prompts: classify/case/topics/      │
           thinking-topics.md         │
        └──────────────┬──────────────┘
                 dùng chung: followups, issue_format, group_notices,
                 Pipeline._safety/_plan_group/_run/_combine
```

- Legacy chiếm ~650 dòng (pipeline ~450, classifier ~170) + 4 prompt.
- **Chưa thể coi là code chết:** `autonomous.resume_case` gọi `pipeline._analyze_case` cho case mở chưa có plan (`autonomous.py:246`); legacy cũng là đường rollback khi tắt autonomous (TG-32).
- `case.md` vẫn được nạp nhưng bị thay bằng `thinking.CASE_PROMPT` khi có lượt thinking (`classifier.py:357-362`) ⇒ sửa `case.md` không có tác dụng trên production.

## 3. Chất lượng code

### 3.1 Nội dung còn nằm trong Python

| Loại | Số lượng | Nơi chính |
|---|---|---|
| Câu hỏi/notice cho group | ~30 | `followups.py` (:14-15, :114, :246-:592, :663-:796) |
| Ghi chú ảnh/tài liệu | ~20 | `attachments.py:161, 233-294, 365-405` |
| Câu mặc định / fallback | ~15 | `pipeline.py:49-51, 154-170, 436, 517-525, 598`; `rules.py:29`; `lifecycle.py:2` |
| Lỗi vận hành | 4 | `store.py:18, 60, 234, 576` |
| Khác | ~10 | `reactions.py:12-13` (emoji), `group_notices.py:27,45,76`, `telegram_io.py:303,331,694,696`, `issue_format.py:18-20,37-38,193,246,383` |
| Prompt nhúng | ~6 khối | `thinking.CASE_PROMPT` (:19-36), `COMPACT_*` (:47-57); `classifier.DETAILS_PROMPT` (:46-60), luật vision (:302-312), dòng phụ (:299, :319-321, :364-365, :516, :559) |

Rủi ro ghép cặp: regex ở `ai_transport.py:153-154` khớp nguyên văn câu prompt ở `classifier.py:319, 327` ⇒ đổi chữ prompt mà quên regex sẽ vỡ redaction.

### 3.2 Trùng lặp

| Trùng lặp | Vị trí |
|---|---|
| Dựng URL issue GitHub | `followups._link:22`, `pipeline.py:43,860`, `group_notices.py:86`, `autonomous.py:150`, `autonomy_store.py:209`, `followup_store.py:340,419`, `github_issues.py:63,129`, `trace.py:319` |
| Gửi notice (reserve → send → finish) | `followups._notify:78-100` và `group_notices.notify:115-142` (giới hạn 3500 vs 1100) |
| Regex email/số điện thoại | `pipeline.py:72-73`, `issue_format.py:44-52`, `ai_transport.py:146` |
| Câu chữ lặp | quarantine `autonomous.py:290` = `followups.py:663`; noise `rules.py:29` = `pipeline.py:154-155, 598` |
| Khối TopicGroup → Plan | `autonomous.py:206-215` ≈ `:262-270` |
| `CASE_STATUS` | `pipeline.py:46` và `store.py:34` |

### 3.3 Tham số nghiệp vụ cứng (chọn lọc)

| Tham số | Giá trị | Vị trí | Trong config? |
|---|---|---|---|
| Ngân sách byte / thời gian / số lần gọi model | 8000 B / 40 s / 2 | `thinking.py:107-186`, `classifier.py:458-615` | Không |
| Ingress: nguồn / reply / candidate | 20 / 6 / 6 | `autonomous.py:99-119`, `autonomy_store.py:202-229` | Không |
| Tách ticket tối đa | 5 | `followup_model.py:5`, regex `followups.py:119,127` | Không |
| Câu hỏi / ký tự / notice | 3 / 180 / 1100 | `group_notices.py:12-15` | Không |
| Emoji reaction | ❤ / 👌 | `reactions.py:12-13` | Không |
| Retention usage | 90 ngày | `store.py:17` | Không |
| Nhãn, loại ticket, regex command | — | `models.py:9-18`, `rules.py:12` | Không (có chủ đích, TG-31) |

Đã có trong config: aggregation, attachments, `max_issues_per_hour`, `confidence_threshold`, model/timeout/pricing, nhịp 30/60 s.

### 3.4 Lỗi / nợ phát hiện khi audit

| Mức | Phát hiện | Bằng chứng |
|---|---|---|
| MED | Ghi chú bỏ ảnh của cả đợt bị chép vào từng ticket; không có log đếm ảnh nên không truy được nguồn (ca #82) | `attachments.py:213`; điều tra THINKPAD 2026-10-29 |
| LOW | `POLICY_VERSION` vẫn `TG-01..TG-32` dù đã có TG-33 | `thinking.py:15` |
| LOW | `NEEDS_CLARITY_LINE` khai báo nhưng không dùng (đã thay bằng template) | `issue_format.py:18` |
| LOW | `case.md` nạp nhưng bị bỏ qua khi có thinking (production) | `classifier.py:357-362` |
| LOW | 3 điểm LOW từ review PR #81 (IPv6 host, link trùng IDN, comment cũ) | review 2026-10-29 |

## 4. Kiểm thử

- 49 file test, **940 ca OK** (1 skip). Golden 352 ca chốt output form ticket.
- Lớn nhất: `test_topics` 1031, `test_direct_call` 772, `test_cases` 719 dòng.
- **Giòn với câu chữ:** ~1.680 dòng test so khớp nguyên văn tiếng Việt (nặng nhất `test_issue_format` 90, `test_issue_layout` 89, `test_topics` 86). Một số test import hằng chữ (`FALLBACK_NOTE`, `GITHUB_AMBIGUOUS_ERROR`, `FALLBACK_QUESTION`, `LEGACY_PENDING_QUESTION`). Chuyển chữ ra template mà không chuyển test sang đọc template ⇒ vỡ hàng loạt.
- Một test hẹn giờ thỉnh thoảng fail (`test_usage_fallback.test_a_throttled_write_is_not_lost_the_trailing_timer_fires`), pass khi chạy lại.
- **CI GitHub không chạy test listener** (chỉ workflow Console) ⇒ golden, drift check, 940 ca chỉ chạy khi có người chạy tay.

## 5. Hiệu năng và vận hành

- Chưa đo p95 thời gian từ tin tới ticket; mỗi lượt tối đa 2 lời gọi model trong 40 s, xử lý tuần tự theo chat.
- SQLite một kết nối dùng chung cho 5 nhóm bảng (~30 bảng); chưa có số đo tranh chấp khóa.
- Máy dev: Codex sandbox `elevated` lỗi trên Windows (khóa `node_repl.exe`), phải chạy `unelevated`.
- Console contract CI fail sẵn trên `main` (16 file thừa) ⇒ không bảo vệ được listener.

## 6. Các hướng có thể (chưa chọn)

| # | Hướng | Lợi ích | Rủi ro | Khối lượng ước lượng |
|---|---|---|---|---|
| W1 | Đưa ~80 câu chữ còn lại ra template | Owner sửa được mọi câu bot nói | Phải chuyển ~1.680 dòng test sang đọc template | Vừa |
| W2 | Đưa prompt nhúng ra `prompts/` | Một nơi cho mọi prompt | Ghép cặp regex `ai_transport` | Nhỏ |
| W3 | Đưa tham số nghiệp vụ vào config | Chỉnh không cần release | Mở thêm núm có thể phá ngân sách/an toàn | Vừa |
| W4 | Bỏ đường legacy | Bớt ~650 dòng + 4 prompt | Mất rollback; phải xử lý `resume_case` | Lớn |
| W5 | Gộp trùng lặp (URL, notice, regex) | Ít chỗ sửa, ít lệch | Thấp nếu có golden | Nhỏ–vừa |
| W6 | Tách module lớn, gỡ vòng import | Dễ đọc, dễ test | Đụng gần hết bot production | Lớn |
| W0 | Chạy test listener trong CI | Bảo vệ mọi hướng trên | Thấp | Nhỏ |

## 7. Câu hỏi cần Owner quyết

1. Thứ tự ưu tiên giữa: Owner tự sửa nội dung (W1–W3) · bớt code (W4–W6) · an toàn thay đổi (W0).
2. Đường legacy còn cần làm rollback không (quyết định W4).
3. Tham số nào Owner thực sự muốn chỉnh không qua release (phạm vi W3).
4. Chấp nhận đổi ~1.680 dòng test sang đọc template không (điều kiện của W1).
