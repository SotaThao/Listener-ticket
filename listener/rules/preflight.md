<!-- rule-file: preflight · version 3 · status DRAFT · audience: người + agent -->
# Cổng bật tính năng — điều kiện trước khi bot chạy

File cho **người và Agent**. Áp mỗi lần bật, cài bản mới hoặc đổi config máy chạy của bot có quyết định theo `rules/` (bước 0–6 của [tài liệu luồng](../../Docs/01-product/telegram-triage-noissue_business-flow_261028_v1.0.0.md)). Quyền ký và miễn theo [`roles.md`](roles.md) · A26, A27.

## Bảng: gate_states

| Mã | Nghĩa |
|---|---|
| `GO` | Mọi mục ở bảng `checks` là `PASS`, hoặc `WAIVED` ở mục có cột "Miễn được?" = `có` — được bật |
| `NO_GO` | Còn ít nhất một mục `FAIL`, `UNCHECKED`, hoặc `WAIVED` ở mục `không` miễn được — không bật |

## Bảng: item_states

| Mã | Nghĩa |
|---|---|
| `UNCHECKED` | Chưa kiểm, hoặc chưa có bằng chứng (mặc định của mọi mục) |
| `PASS` | Đạt, có bằng chứng ghi trong phiếu |
| `FAIL` | Không đạt |
| `WAIVED` | Owner miễn — ghi `WAIVED · <ngày> · Owner · <lý do>`; chỉ hợp lệ khi "Miễn được?" = `có` |

Bằng chứng là **output máy** (lệnh đã chạy + kết quả), commit SHA, hoặc link PR/ticket — không phải lời khẳng định.

## Bảng: checks

| # | Kiểm | Cách kiểm / bằng chứng | Ai ký | Miễn được? |
|---|---|---|---|---|
| P1 | Mọi file quy tắc dùng cho quyết định (`states`, `facts`, `readiness`, `questions`, `channels`, `examples`) là `ACTIVE` | Header dòng 1 từng file; K1, K7 của [README](README.md) §4 | `OWNER` | `không` |
| P2 | `test_rules_markdown` PASS: bảng hợp lệ, mọi ví dụ đúng, mỗi luật có ví dụ | Output `python -m unittest tests.test_rules_markdown` | `DEV` | `không` |
| P3 | Bot nạp rules + template thành công, fingerprint được ghi lại | `check-config` thoát 0, chép fingerprint rules và template vào phiếu | `DEV` | `không` |
| P4 | Toàn bộ test listener PASS trên đúng commit sẽ release | Output `unittest` (số test, OK) kèm commit SHA | `DEV` | `không` |
| P5 | Drift checker PASS | Output `python scripts/check-telegram-triage-policy.py` và `test_telegram_triage_policy.py` | `DEV` | `không` |
| P6 | Không còn lỗi HIGH mở | Danh sách lỗi HIGH và trạng thái. **HIGH** = lỗi làm tạo sai/thiếu/trùng ticket, gửi sai kênh, mất ảnh, hoặc lộ dữ liệu | `OWNER` | `có` |
| P7 | Smoke dùng fake tools PASS, không replay tin thật | Output `telegram-autonomous-model-smoke.py` / `telegram-thinking-model-smoke.py` | `DEV` | `có` |
| P8 | Model đang cấu hình trích dữ kiện đúng **100%** ví dụ của `examples.md` | Chạy trích trên cột "Tin" → áp rules → so cột "Kết quả" (README §7) | `DEV` | `không` |
| P9 | Config máy chạy đã đối chiếu: repo, chat nguồn, `ops_chat_id`, `mode`, `autonomous_enabled`, giới hạn ảnh | Output `check-config` trên bản cài (chỉ in độ dài trường bí mật) | `OPERATOR` | `không` |
| P10 | Có đường quay lại bản cũ đã thử | Wheel bản trước còn giữ; đã cài lại thử theo [deploy guide](../../deploy/README.md) | `OPERATOR` | `không` |
| P11 | Test trên bản cài + service đọc lại sau restart | Output test bản cài, PID service, fingerprint trong log khớp P3 | `OPERATOR` | `không` |
| P12 | Group xử lý hoạt động: bot là thành viên, gửi được tin, group gốc chỉ nhận reaction | Dry-run gửi tin thử vào `ops_chat_id`; đọc log không có tin chữ gửi vào chat nguồn | `OPERATOR` | `không` |
| P13 | Owner ký bật | Dòng `Owner: GO · <ngày>` trong phiếu | `OWNER` | `không` |

Thứ tự: P1–P8 trước khi release; P9–P12 trên máy chạy; P13 cuối cùng. Một mục `FAIL` ⇒ dừng, không làm mục sau.

## Bảng: halt

Khi đang chạy, gặp một trong các điều kiện dưới đây ⇒ dừng bot (`roles.md` · A25), giữ nguyên DB và log, báo Owner. Không retry, không sửa DB.

| # | Điều kiện dừng |
|---|---|
| H1 | Bot tạo ticket trùng cho cùng chủ đề |
| H2 | Ticket tạo vào sai repo hoặc sai nền tảng |
| H3 | Ticket, comment hoặc tin của bot lộ token, session, config bí mật |
| H4 | Có thao tác `ambiguous` chưa được Owner kiểm |
| H5 | Fingerprint rules/template trong log khác phiếu đã ký |
| H6 | Bot tạo ticket cho chủ đề mà bảng `decision` ra `CHAT`, `NEED_INFO`, `MEDIA_ONLY` hoặc `HOLD` |
| H7 | Bot gửi tin chữ vào group nguồn |

## Phiếu bật tính năng (chép vào PR release)

```
Phiếu bật · release <tag> · commit <sha> · ngày <yyyy-mm-dd>
Fingerprint rules: <...>   Fingerprint template: <...>
P1  PASS|FAIL|UNCHECKED|WAIVED — <bằng chứng> — ký: <vai, tên>
...
P13 PASS — Owner: GO · <ngày>
Kết luận: GO|NO_GO
```

## Hiện trạng (2026-11-05)

`GO`:

```
Phiếu bật · release main 2eae517 · ngày 2026-11-05
Fingerprint rules: 6f40ec4377b2cc47c41b38860e0c512982801e68c7324881bb09babc5d9f3df0
Fingerprint template: 7f0087efa70689014396bb703266520df9040003bff5b89fe3a048bbf6f30a22
P1  PASS — readiness.md, examples.md v4 ACTIVE; Owner “Duyệt lọc trước” 2026-11-05 — ký: OWNER, AlexChu
P2  PASS — test_rules_markdown trong full suite — ký: DEV
P3  PASS — check-config + kiểm khởi động trên ledger thật (chỉ đọc), fingerprint như trên — ký: DEV
P4  PASS — 1757 test OK (skipped=2) trên 2eae517; bản cài trên máy chạy 1757 OK — ký: DEV
P5  PASS — policy checker 33/61 + policy test — ký: DEV
P6  PASS — review GATE 0b APPROVED cho 96cce8e (Việc cần làm), 10ba5fd (kiểm khởi động ESCALATED), 2eae517 (lọc trước) — ký: DEV
P7  PASS — fake-tool test: pipeline live, batch, extension bật/tắt — ký: DEV
P8  WAIVED — chưa chạy lại corpus model thật; ví dụ gửi model không đổi so với 7ba210b, lọc trước chạy trước model — ký: OWNER, AlexChu
P9  PASS — config máy chạy không đổi (SHA trước/sau giống nhau), source_reactions=false, group ops như phiếu trước — ký: OPERATOR
P10 PASS — rollback thật 2026-11-05 16:02: script deploy tự cài lại bản trước — ký: OPERATOR
P11 PASS — deploy 2eae517, restart 21:39, fingerprint trong log khớp P3, 0 lỗi — ký: OPERATOR
P12 UNCHECKED — group nguồn là PROD, không gửi tin thử; ticket thật #111 xác nhận mục "Việc cần làm"; theo dõi tin review PR thật phải ra F1 — ký: OPERATOR
P13 PASS — Owner: GO · 2026-11-05
Kết luận: GO
```

Owner 2026-11-05: “Ký GO” (P8 WAIVED, P12 theo dõi tin thật).

Phiếu trước (thay thế bởi phiếu trên):

```
Phiếu bật · release main 7ba210b · ngày 2026-11-04
Fingerprint rules: 80422e2871b95601fad9c1652e37556322c6d9c6d3f149be53b85fa434bc66b9
Fingerprint template: 8bc960370883bc8855a0e3e204f22656f957e5f0ae1998cbd8343ef184d9ef2f
P1  PASS — sáu file quyết định ACTIVE theo Owner 2026-10-30; lifecycle/analysis-status ACTIVE 2026-11-03 — ký: OWNER, AlexChu
P2  PASS — test_rules_markdown trong full suite — ký: DEV
P3  PASS — check-config thoát 0, fingerprint như trên — ký: DEV
P4  PASS — 1670 test OK (skipped=2) trên commit 7ba210b — ký: DEV
P5  PASS — policy checker 33/61 và 18 policy test — ký: DEV
P6  PASS — Owner ký 2026-11-03 — ký: OWNER, AlexChu
P7  PASS — fake-tool smoke core + extension — ký: DEV
P8  PASS — corpus 24/24 đúng kết quả (model cấu hình) — ký: DEV
P9  PASS — check-config bản cài: ops -1003582326670 (supergroup), repo PersonalProjectJob/telegram-agent — ký: OPERATOR
P10 PASS — cài lại bản trước a81355f bằng script deploy thành công — ký: OPERATOR
P11 PASS — deploy 7ba210b, restart, fingerprint trong log khớp P3 — ký: OPERATOR
P12 PASS — tin thật READY/NEED_INFO/ANALYZE/STATUS: chữ chỉ vào ops, nguồn chỉ reaction — ký: OPERATOR
P13 PASS — Owner: GO · 2026-11-04
Kết luận: GO
```

Lịch sử: phiếu 2026-10-29 là `NO_GO` (P1 rules còn DRAFT, P3/P8/P12 chưa có code nạp rules và group xử lý, P6 lỗi HIGH giới hạn ảnh vision); các mục này đã được giải quyết trước phiếu trên. Mỗi lần cài bản mới hoặc đổi config máy chạy phải lập phiếu mới.
