<!-- rule-file: preflight · version 2 · status DRAFT · audience: người + agent -->
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

## Hiện trạng (2026-10-29)

`NO_GO`:

- P1 `FAIL`: mọi file `rules/` còn `DRAFT`; câu chữ 6 khối template `cần thêm` (`channels.md` · `blocks`) chưa duyệt.
- P3, P8, P12 `UNCHECKED`: bot chưa có code nạp `rules/`, chưa có group xử lý.
- P6 `FAIL`: lỗi HIGH giới hạn ảnh tính chung cả đợt vision chưa sửa.
