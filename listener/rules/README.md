# Quy tắc nạp rules vào bot và model

> **Trạng thái: DRAFT — chờ Owner duyệt.** Runtime **chưa** đọc thư mục này. File này quy định **cách** bot nạp, kiểm và dùng các file quy tắc; nội dung quy tắc nằm ở các file khác trong thư mục.
>
> **Nguyên tắc (Owner, 2026-10-29):** mọi quyết định của bot dựa trên file markdown trong thư mục này với trạng thái liệt kê cụ thể. **Model chỉ trích dữ kiện; code áp quy tắc.** Đổi hoặc nâng cấp model chỉ làm dữ kiện chính xác hơn hoặc nhanh hơn, không đổi kết quả của cùng một bộ dữ kiện.
>
> Nguồn nghiệp vụ: [Business DOCS](../../Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md) giải thích **vì sao**; thư mục này là **bảng chạy thật**. Luồng tổng: [tài liệu luồng nghiệp vụ](../../Docs/01-product/telegram-triage-noissue_business-flow_261028_v1.0.0.md) (sequence đích, bước 0–6). Đặc tả và quyết định Owner Q1–Q10: [rules-spec](../../Docs/04-tasks/telegram-triage-noissue_rules-spec_261029_v1.0.0.md).

## 1. Danh mục file

File quyết định — bot nạp theo thứ tự:

| File | Nội dung | Ai dùng | Thứ tự nạp | Trạng thái |
|---|---|---|---|---|
| [states.md](states.md) | 6 trạng thái kết quả, vòng đời pending, hạn 7 ngày, số lần thử `HOLD` | Code | 1 | DRAFT |
| [facts.md](facts.md) | Dữ kiện model trích (`fields`), dữ kiện code tính (`code_facts`), command → intent, điều kiện CÓ V1–V4, chuẩn hóa N1–N5, giá trị chung chung | **Model** (`fields`, cách ghi giá trị) + Code | 2 | DRAFT |
| [readiness.md](readiness.md) | Bảng `intent_resolve` I1–I3 và `decision` R1–R13 dạng cột, thứ không bao giờ chặn, ảnh chưa xem (D7 = A), confidence (bỏ) | Code | 3 | DRAFT |
| [questions.md](questions.md) | Hỏi gì khi thiếu, thứ tự, giới hạn, nối câu trả lời ở group xử lý (L1–L4) | Code | 4 | DRAFT |
| [channels.md](channels.md) | Bot nói gì ở group gốc (chỉ reaction) và group xử lý; danh mục khối template | Code | 5 | DRAFT |
| [examples.md](examples.md) | 24 ví dụ X1–X24 phủ mọi luật + 8 ca chuẩn hóa G1–G8 | Code (tự kiểm) + **Model** (cột Tin, intent, Dữ kiện) | 6 | DRAFT |

File cho **người và Agent** — không nằm trong trình tự nạp quyết định, không gửi model:

| File | Nội dung | Ai dùng | Trạng thái |
|---|---|---|---|
| [roles.md](roles.md) | Ai được làm gì: vai trò, bảng việc A01–A27 với `ALLOW` / `ASK_OWNER` / `DENY`, định nghĩa "đồng ý của Owner trong phiên", khi nào dừng và hỏi | Người + Agent | DRAFT |
| [preflight.md](preflight.md) | Cổng bật tính năng: mục kiểm P1–P13, `GO` / `NO_GO`, điều kiện dừng bot H1–H7, phiếu ký | Người + Agent | DRAFT |

Sẽ thêm theo từng lát (chưa soạn): `lifecycle.md` (reply vào ticket đã đóng, Vòng đời ticket) · `topics.md` (TG-05/06) · `priority.md`.

## 2. Định dạng mỗi file quy tắc

1. **Dòng đầu tiên** bắt buộc là header:
   `<!-- rule-file: <tên> · version <số> · status DRAFT|ACTIVE · audience: code | model + code | người + agent -->`
   - `<tên>` trùng tên file (không đuôi `.md`).
   - `status: DRAFT` ⇒ runtime **bỏ qua** file; chỉ `ACTIVE` mới được nạp.
2. **Chỉ bảng được máy đọc.** Mỗi bảng nằm ngay dưới heading `## Bảng: <tên_bảng>`; hàng đầu là tên cột cố định. Văn xuôi, ghi chú, heading khác là hướng dẫn cho người, runtime bỏ qua.
3. Giá trị máy so khớp (mã trạng thái, tên trường, mã luật, tên khối template) viết trong backtick: `` `READY` ``, `` `object` ``.
4. Ô quyết định chỉ dùng: giá trị trong backtick (một hoặc nhiều = một trong số đó), `*` (bất kỳ), `—` (không yêu cầu), `∅` (không có). **Không** viết điều kiện bằng lời trong ô.
5. Không viết nội dung người dùng hay dữ liệu thật vào file quy tắc (ví dụ dùng câu rút gọn, không số điện thoại/email/mật khẩu).

## 3. Trình tự nạp (bước 0 của luồng, lúc `run` và `check-config`)

```
 ┌─ đọc thư mục rules/ (source tree, nếu không có thì bản cài sys.prefix/share/tg-triage/rules)
 ├─ 1. states.md      → tập trạng thái hợp lệ, limits
 ├─ 2. facts.md       → trường, code_facts, command_intent, field_valid, normalize, generic_values
 ├─ 3. readiness.md   → intent_resolve + decision (chỉ dùng trạng thái ở 1, trường/giá trị ở 2)
 ├─ 4. questions.md   → ask_order, fallback, answer_link (chỉ dùng trường ở 2)
 ├─ 5. channels.md    → outputs (đủ mọi trạng thái ở 1), blocks (khối phải có trong template)
 ├─ 6. examples.md    → chạy TỰ KIỂM: mỗi ví dụ ra đúng Kết quả, Luật, Hỏi
 ├─ tính fingerprint SHA-256 (tên file + nội dung chuẩn hóa LF, theo thứ tự tên)
 └─ ghi log fingerprint; check-config in ra "rules": {"fingerprint": …}
```

Một bước lỗi ⇒ **dừng, không khởi động** (mã thoát 2), báo `<file>:<dòng>: <lý do>` — giống template TG-33. Không có bản dự phòng trong code, không đọc lại khi đang chạy (đổi rules = restart).

Bản tham chiếu của các bước kiểm: [`listener/tests/test_rules_markdown.py`](../tests/test_rules_markdown.py) (chỉ đọc markdown, không import runtime). Code nạp rules sau này phải cho cùng kết quả với test này.

## 4. Kiểm tra lúc nạp

| # | Kiểm | Lỗi khi |
|---|---|---|
| K1 | Đủ file theo §1, header đúng định dạng, tên khớp | Thiếu file, sai header, hai file cùng tên |
| K2 | Mỗi file có đúng các bảng bắt buộc, đúng tên cột, mỗi hàng đủ ô | Thiếu/thừa bảng, sai cột |
| K3 | Kết quả trong `decision` ⊆ `states`; `channels.outputs` có đúng mỗi trạng thái một dòng | Trạng thái lạ hoặc thiếu |
| K4 | Tên trường trong `generic_values`, `ask_order`, `examples` ⊆ `facts.fields`; intent của `command_intent` ⊆ danh sách intent | Trường/intent lạ |
| K5 | Mã luật liên tiếp `R1…Rn`, `I1…In`; dòng cuối `decision` là "còn lại" (mọi cột điều kiện `*`) | Trùng/nhảy mã, thiếu dòng mặc định |
| K6 | `channels.blocks` liệt kê đúng các khối được dùng; khối `có` phải có trong template, khối `cần thêm` phải chưa có | Khối không tồn tại hoặc đánh dấu sai |
| K7 | Không file quy tắc nào còn chữ **CHỜ OWNER CHỐT** | Còn quyết định mở |
| K8 | Tự kiểm `examples.md`: mọi ví dụ ra đúng Kết quả, Luật, Hỏi; mọi ca chuẩn hóa đúng | Bất kỳ ví dụ sai |
| K9 | Mỗi luật R1–Rn có ít nhất một ví dụ | Luật không có ví dụ |
| K10 | Ô điều kiện của `decision` chỉ dùng giá trị trong `code_facts`, `command_intent`, danh sách intent | Giá trị lạ, ô rỗng |
| K11 | Config có `ops_chat_id` (`channels.md`) | Thiếu group xử lý |

K1–K10 đã có trong `test_rules_markdown`; K11 kiểm khi có code nạp config.

## 5. Phần nào gửi cho model, phần nào không

| Gửi cho model | Không bao giờ gửi cho model |
|---|---|
| `facts.md`: bảng `fields` và mục "Cách model ghi giá trị" (để model biết trích gì, khi nào để trống) | `states.md`, `readiness.md`, `questions.md`, `channels.md`, `roles.md`, `preflight.md` |
| `examples.md`: **chỉ** cột "Tin (rút gọn)", "intent (model)", "Dữ kiện" làm ví dụ trích | Các cột còn lại của `examples.md`; bảng `code_facts`, `field_valid`, `normalize`, `generic_values` |

- Lý do: model không được biết bảng quyết định hay danh sách chung chung, để không "điền dữ kiện cho khớp kết quả" và để quyết định không đổi khi đổi model.
- Bot **sinh schema JSON** trả về của model từ bảng `fields` (đúng tên trường, đúng kiểu, mỗi trường có `source_ids`). Trường thừa ⇒ bỏ; trường thiếu ⇒ trống; `source_ids` không thuộc chủ đề ⇒ trường trống (V2).
- Prompt model (`listener/prompts/`) chỉ chứa hướng dẫn trích dữ kiện chung; định nghĩa trường lấy từ `facts.md` lúc nạp, không chép tay vào prompt.

## 6. Code dùng các file thế nào (bước 3 của luồng)

```
dữ kiện model ──► facts.field_valid (V1–V4) + normalize + generic_values ──► chỉ còn trường CÓ
dữ kiện code  ──► code_facts (command, command_payload, text, media, extraction_ok, reply_target)
                                                                     │
                         readiness.intent_resolve (I1–I3) ──► intent dùng
                         readiness.decision (dòng đầu khớp R1–R13) ──► mã trạng thái (states)
                                                                     │
         NEED_INFO ──► questions.ask_order / fallback ──► khối template ──► group xử lý (channels)
         READY / APPEND ──► template ticket / comment ──► adapter nền tảng ──► link ở group xử lý
         mọi trạng thái ──► reaction ở group gốc theo channels.outputs
```

Kết quả mỗi chủ đề được ghi vào nhật ký quyết định kèm: mã luật đã khớp (vd `I2+R9`), danh sách trường CÓ và `source_ids`, `confidence` (chỉ để theo dõi), fingerprint rules. Nhờ vậy mọi quyết định truy ngược được về đúng dòng trong file.

## 7. Đổi model

- Đổi/nâng model **không** cần sửa thư mục này.
- Trước khi dùng model mới: chạy trích dữ kiện trên cột "Tin (rút gọn)" của `examples.md`; dữ kiện trích ra áp `readiness.md` phải cho đúng **100%** cột "Kết quả" (`preflight.md` · P8). Sai ⇒ không đổi model (hoặc sửa prompt trích), **không** sửa quy tắc để chiều model.

## 8. Đổi quy tắc

1. Sửa file trên branch; tăng `version` trong header.
2. Thêm/sửa ví dụ trong `examples.md` cho mọi luật bị đổi (K9).
3. Chạy `test_rules_markdown` + test listener; cập nhật Business DOCS (trace, changelog).
4. Owner duyệt PR (`roles.md` · A09, A10). File chuyển `DRAFT → ACTIVE` chỉ khi Owner duyệt và K7 đạt.
5. Release → cài → `check-config` → so fingerprint → restart (`preflight.md`).

Không sửa file quy tắc trực tiếp trên máy chạy; không có thư mục override.

## 9. Đóng gói

`listener/pyproject.toml` thêm `"share/tg-triage/rules" = [các file .md trong thư mục này]`, giống prompts và templates. Bản cài tìm rules ở `sys.prefix/share/tg-triage/rules`.

## 10. Còn chờ Owner trước khi ACTIVE

| Mục | File | Việc |
|---|---|---|
| Câu chữ 6 khối `cần thêm` | `channels.md` · `blocks` → `templates/github-ticket/group-replies.md` | Duyệt câu chữ `ops.header`, `ask.object`, `ask.observed`, `ask.work`, `ask.problem`, `notice.hold_escalated` |
| Bảng quyền A01–A27 | `roles.md` · `actions` | Duyệt từng ô |
| Mục kiểm P1–P13, điều kiện dừng H1–H7 | `preflight.md` | Duyệt danh sách và người ký |
| Reply vào ticket đã đóng | `lifecycle.md` (chưa soạn) | Chuyển bảng Vòng đời ticket thành rules |
