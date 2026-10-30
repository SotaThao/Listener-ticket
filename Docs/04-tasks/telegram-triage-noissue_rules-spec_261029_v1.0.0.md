# Telegram triage — Implementation plan: định nghĩa chặt bộ rules

**Version**: v1.0.0 · **Ngày**: 2026-10-29 · **Owner**: AlexChu · **Author**: Claude · **Trạng thái**: Owner duyệt 2026-10-29 ("chốt rule thành từng file markdown"); đã viết vào `listener/rules/` (DRAFT). Điểm chỉnh khi viết: Q10 thành "mỗi **trường** chỉ hỏi một lần" (`max_asks_per_field`) để câu trả lời thiếu một phần vẫn được hỏi trường còn lại; `MEDIA_ONLY` dùng dữ kiện code `text` thay vì dữ kiện model; preflight thêm `UNCHECKED` và đánh số lại P1–P13
**Branch**: `feat/noissue_decision-rules` (PR #83) · **Phạm vi**: `listener/rules/*.md`, `listener/AGENTS.md`, một test tự kiểm, ghi chú quyết định trong Business DOCS. **Không đổi runtime**: bot chạy thật giữ hành vi hiện tại cho tới PR triển khai code.

## 1. Mục tiêu và tiêu chí "chặt"

Một bộ rules là chặt khi **hai người/Agent bất kỳ, đọc cùng bảng, với cùng dữ kiện, ra cùng kết quả** — không cần hiểu ngữ cảnh, không cần hỏi model.

| # | Tiêu chí | Cách kiểm |
|---|---|---|
| C1 | Mọi ô quyết định là **giá trị trong danh sách đóng** (mã trạng thái, tên trường, `*` = bất kỳ, `∅` = trống) — không còn điều kiện văn xuôi kiểu "(khi không nói chức năng nào)" | Test đọc bảng, ô lạ ⇒ FAIL |
| C2 | Bảng quyết định đọc **từ trên xuống, dòng đầu khớp thắng**, dòng cuối là "còn lại" | Test |
| C3 | Mọi luật có **ít nhất một ví dụ** trong `examples.md`; mọi ví dụ ra đúng kết quả và đúng mã luật | Test chạy bảng trên ví dụ |
| C4 | Mọi tên trường/trạng thái/khối template tham chiếu chéo đều tồn tại | Test |
| C5 | So khớp chuỗi có **thuật toán chuẩn hóa** viết sẵn, không "hiểu nghĩa" | Bảng `normalize` |
| C6 | Không còn bảng **CHỜ OWNER CHỐT** nào cho các mục đã chốt hôm nay | grep |

## 2. Quyết định Owner (2026-10-29)

| Mã | Câu hỏi | Chốt |
|---|---|---|
| Q1 | Ảnh đơn lẻ không chữ | **Theo TG-15: im lặng.** Giữ pending, không hỏi; tin có chữ sau đó cùng chủ đề thì gộp và xét lại. Bỏ khối `ask.media_intent` |
| Q2 | Ai quyết `intent` | **Command > model.** `/bug` `/task` `/feedback` `/feature` ép intent; `/question` ⇒ `CHAT`. Không có command: model trích intent trong danh sách đóng + `unknown` |
| Q3 | D7 — ảnh model chưa xem | **A: không ảnh hưởng trạng thái.** Ticket đính đủ ảnh, ghi "model chưa xem N ảnh" |
| Q4 | Ngưỡng confidence 0.7 | **A: bỏ khỏi quyết định.** Chỉ ghi log |
| Q5 | Model lỗi (gọi lỗi, sai schema, hết quota) | **Thêm trạng thái `HOLD`**: không ticket, không hỏi; thử lại tối đa 3 lần rồi báo Owner |
| Q6 | So giá trị chung chung | **Khớp nguyên cụm sau chuẩn hóa** |
| Q7 | Reply thật vào ticket open, nội dung chưa đủ rõ | **Luôn bổ sung** (trừ `CHAT`) |
| Q8 | Bot nói ở đâu | **Group xử lý chung (một group).** Group gốc **chỉ có reaction**; mọi tin chữ của bot (câu hỏi, link ticket, báo lỗi, hết quota) sang group xử lý |
| Q9 | Trả lời trong group xử lý nối về chủ đề | **Chỉ Reply thật vào tin của bot** mới tính; tin không reply bị bỏ qua, không tạo chủ đề mới |
| Q10 | Câu hỏi không ai trả lời | **Hỏi đúng 1 lần, đóng pending sau 7 ngày**, im lặng, ghi nhật ký |

Ảnh hưởng tới Business DOCS (đích, **chưa** sửa câu TG vì runtime chưa đổi): TG-08 (bỏ confidence, `HOLD`), TG-11/TG-12 (nói ở group xử lý), TG-16 (tin bot đã gửi ở group xử lý là bằng chứng root của chủ đề — hiện TG-16 nói cross-chat không cấp quyền), TG-15 (khớp Q1). Ghi thành mục "Quyết định đích 2026-10-29" trong Business DOCS; câu TG chỉ sửa ở PR triển khai code.

## 3. Thiết kế từng file

### 3.1 `states.md` — 6 trạng thái, danh sách đóng

| Mã | Ticket | Group gốc | Group xử lý | Pending |
|---|---|---|---|---|
| `READY` | Tạo mới | ❤️ khi nhận, 👌 khi có số ticket | Link ticket (`reply.created`) | Đóng |
| `APPEND` | Comment vào ticket open | ❤️ | Link ticket đã bổ sung | Đóng |
| `NEED_INFO` | Không | ❤️ | Câu hỏi theo `questions.md` | Mở, hết hạn 7 ngày |
| `MEDIA_ONLY` | Không | Không | Không | Mở, hết hạn 7 ngày |
| `CHAT` | Không | Không | Không | Không |
| `HOLD` | Không | Không | Không (chỉ báo Owner sau 3 lần thử) | Mở |

Thêm bảng `pending_states`: `OPEN` → `ANSWERED` (có reply hợp lệ, xét lại) · `EXPIRED` (7 ngày, im lặng) · `ESCALATED` (`HOLD` quá 3 lần).

### 3.2 `facts.md` — dữ kiện model trích + dữ kiện code tính

- Bảng `fields` (model trả): thêm cột **Bắt buộc `source_ids`**, **Độ dài tối đa** (200 ký tự), **Ngôn ngữ** (giữ nguyên lời người gửi, không dịch); `intent` thêm `unknown`.
- Bảng **`code_facts`** (code tính, model không trả): `command` (∅ hoặc mã command), `reply_target` (`none` / `ticket_open` / `ticket_closed` / `bot_question`), `media_seen`, `media_unseen`, `extraction_ok` (true/false), `retry_count`.
- Bảng `command_intent`: `/bug`→`bug`, `/task`→`task`, `/feature`→`feedback`, `/feedback`→`feedback`, `/question`→`question`; command không nội dung ⇒ `CHAT` (TG-04).
- Bảng `field_valid` V1–V4: thêm **V4** — giá trị lấy từ ảnh chỉ hợp lệ khi `source_ids` trỏ tới tin ảnh **thuộc `media_seen`**.
- Bảng **`normalize`** N1–N5: chữ thường · bỏ dấu câu đầu/cuối · gộp khoảng trắng · bỏ đại từ xưng hô đầu câu ("em", "anh", "chị") · **giữ** dấu tiếng Việt. So `generic_values` bằng **bằng nguyên cụm** sau chuẩn hóa.
- Bảng `generic_values`: danh sách thuần (một cụm một dòng, theo trường), **bỏ** điều kiện trong ngoặc; thêm cụm cho `problem`, `proposal`.

### 3.3 `readiness.md` — bảng quyết định có cột, không văn xuôi

Cột: `#` · `extraction_ok` · `command` · `reply_target` · `intent` · `cần CÓ tất cả` · `cần CÓ ít nhất một` · `Kết quả`. Ô dùng giá trị đóng, `*` = bất kỳ.

| # | Điều kiện (tóm tắt) | Kết quả |
|---|---|---|
| R1 | `extraction_ok` = false | `HOLD` |
| R2 | `command` = `/question`, hoặc command rỗng | `CHAT` |
| R3 | `reply_target` = `ticket_open`, intent ≠ `chat` | `APPEND` |
| R4 | `intent` = `chat` | `CHAT` |
| R5 | `intent` = `question`, không có `work` | `CHAT` |
| R6 | `intent` = `question`, có `work` | xét như `task` (R9/R11) |
| R7 | không trường chữ nào CÓ, `media_seen`+`media_unseen` > 0 | `MEDIA_ONLY` |
| R8 | `bug` + `object` + `observed` | `READY` |
| R9 | `task` + `object` + `work` | `READY` |
| R10 | `feedback` + (`problem` hoặc `proposal`) | `READY` |
| R11 | `bug`/`task`/`feedback` thiếu trường | `NEED_INFO` |
| R12 | còn lại (gồm `unknown`) | `NEED_INFO` (câu mặc định) |

- `unseen_media` và `confidence` thành bảng **đã chốt** (Q3, Q4): không ảnh hưởng kết quả, chỉ ghi ticket/log.
- `never_blocks` giữ nguyên.
- `reply_target` = `ticket_closed`: **ngoài phạm vi** lát này — theo bảng Vòng đời ticket trong Business DOCS cho tới khi có `lifecycle.md` (ghi rõ trong file).

### 3.4 `questions.md` — hỏi ở group xử lý

- Bảng `ask_order` giữ thứ tự theo intent; **một tin hỏi mỗi chủ đề**, gộp tối đa 3 trường thiếu, **hỏi đúng 1 lần**.
- Bảng `ask_rules`: chỉ hỏi khi `NEED_INFO`; không hỏi `MEDIA_ONLY`, `HOLD`, `CHAT`; hết hạn 7 ngày ⇒ `EXPIRED`, không nhắn.
- Bảng `answer_link`: chỉ Reply thật vào tin hỏi của bot ở group xử lý mới nối về chủ đề ⇒ gộp dữ kiện ⇒ chạy lại `readiness.md` từ R1.
- Bỏ `ask.media_intent`. Khối `ask.object` / `ask.observed` / `ask.work` / `ask.problem` vẫn *(cần thêm)*.

### 3.5 File mới `channels.md` — bot được nói gì, ở đâu

- Bảng `channels`: `source` (group gốc: chỉ reaction) · `ops` (group xử lý chung: mọi tin chữ).
- Bảng `outputs`: mỗi trạng thái × kênh ⇒ hành động/khối template (như bảng 3.1).
- Tin ở group xử lý **bắt buộc** có: tên group gốc, link tin đầu chủ đề, mã chủ đề — để người trả lời biết đang nói về gì.
- Config: `ops_chat_id` bắt buộc; thiếu ⇒ không khởi động (thêm vào preflight).

### 3.6 `examples.md` — mỗi luật ≥ 1 ví dụ

Đổi bảng sang cột có cấu trúc giống `readiness.md` (dữ kiện từng cột) để test chạy được. Giữ X1–X10, thêm tối thiểu: command `/bug` có nội dung · command rỗng · reply vào ticket open "vẫn bị" (`APPEND`) · reply "cảm ơn" vào ticket (`CHAT`) · model lỗi (`HOLD`) · `question` + `work` · `unknown` · bug có ảnh chưa xem (D7 ⇒ `READY`) · giá trị lấy từ ảnh đã xem (V4 hợp lệ) và từ ảnh chưa xem (V4 không hợp lệ ⇒ `NEED_INFO`) · "nút Thanh toán" (không chung chung) vs "nút này". Dự kiến ~22 ví dụ.

### 3.7 `roles.md`, `preflight.md`

- `roles.md`: định nghĩa **"Owner đồng ý trong phiên"** = tin của Owner trong cuộc trò chuyện hiện tại, nêu đúng mã việc (vd `A12`) hoặc đúng việc; Agent trích nguyên văn vào báo cáo. Thêm việc: gửi tin vào group xử lý (theo A19), đổi `ops_chat_id` (theo A15).
- `preflight.md`: thêm **P13** group xử lý đã cấu hình, bot gửi được tin thử bằng dry-run; P8 ngưỡng **100%** ví dụ; định nghĩa "lỗi HIGH"; mẫu ghi `WAIVED` (ai, ngày, lý do). Thêm H7: bot gửi tin chữ vào group gốc.

### 3.8 `README.md` của rules, `AGENTS.md`

- Danh mục thêm `channels.md`; trình tự nạp: states → facts → readiness → questions → channels → examples.
- Kiểm thêm K9 (mỗi luật ≥ 1 ví dụ), K10 (ô bảng chỉ dùng giá trị đóng), K11 (`ops_chat_id` có trong config).
- §10 "Còn chờ Owner chốt": xóa D7, confidence; giữ câu chữ 4 khối hỏi + duyệt bảng quyền/cổng.
- `AGENTS.md`: thêm dòng group gốc chỉ reaction, mọi trao đổi ở group xử lý.

## 4. Test tự kiểm (viết trước — đỏ rồi xanh)

`listener/tests/test_rules_markdown.py` — **chỉ đọc markdown, không import runtime**, chạy cùng bộ test listener:

1. Đọc header mọi file `rules/*.md`: đúng định dạng, tên khớp (K1).
2. Đọc mọi `## Bảng:`; đủ bảng bắt buộc, đúng cột (K2).
3. Tham chiếu chéo trạng thái/trường/khối (K3, K4, K6).
4. Ô bảng `decision` chỉ giá trị đóng (K10, C1).
5. Áp `decision` lên từng ví dụ ⇒ đúng kết quả + đúng mã luật (K8); mỗi luật có ví dụ (K9).
6. Hàm chuẩn hóa N1–N5 + so `generic_values` trên các ví dụ chung chung/không chung chung.

Đây là bản tham chiếu cho code nạp rules sau này (PR triển khai sẽ dùng chung parser).

## 5. File thay đổi

| File | Việc |
|---|---|
| `listener/rules/states.md` | Viết lại (6 trạng thái, pending) |
| `listener/rules/facts.md` | Viết lại (code_facts, command_intent, V4, normalize) |
| `listener/rules/readiness.md` | Viết lại (bảng cột R1–R12, chốt D7/confidence) |
| `listener/rules/questions.md` | Viết lại (group xử lý, 1 lần, 7 ngày) |
| `listener/rules/channels.md` | **Mới** |
| `listener/rules/examples.md` | Viết lại (~22 ví dụ, cột cấu trúc) |
| `listener/rules/roles.md`, `preflight.md` | Bổ sung (3.7) |
| `listener/rules/README.md`, `listener/AGENTS.md` | Bổ sung (3.8) |
| `listener/tests/test_rules_markdown.py` | **Mới** |
| `Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md` | Mục "Quyết định đích 2026-10-29" + `related_documents` thêm `channels.md` |

Không xóa file. Không đổi `src/`, `prompts/`, `templates/`.

## 6. Tiêu chí nghiệm thu

- [ ] C1–C6 đạt; `test_rules_markdown` PASS.
- [ ] Toàn bộ test listener PASS (hiện 940 + test mới).
- [ ] `check-telegram-triage-policy.py` PASS; `test_telegram_triage_policy.py` OK; mọi link trong `rules/`, `AGENTS.md` trỏ đúng file.
- [ ] Mọi file rules vẫn `DRAFT`; preflight hiện trạng vẫn `NO_GO` (chưa có code nạp, chưa duyệt câu chữ khối hỏi).

## 7. Còn mở sau plan này

- Câu chữ 4 khối hỏi (`ask.object`, `ask.observed`, `ask.work`, `ask.problem`) và tin ở group xử lý.
- `lifecycle.md` cho reply vào ticket đã đóng.
- Harness yêu cầu ghi `Decisions/D-NNN` trong vault Nexora — vault chưa có thư mục `Decisions`; cần Owner chỉ chỗ lưu.
