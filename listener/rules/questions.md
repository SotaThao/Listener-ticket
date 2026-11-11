<!-- rule-file: questions · version 8 · status ACTIVE · audience: code -->
# Hỏi khi thiếu (trạng thái `NEED_INFO`)

Owner 2026-11-04: Đồng ý cả 4 + Duyệt, chạy tiếp (L1 chỉ cho NEED_INFO).

Owner (2026-11-11): “Cùng người + model xác nhận”; “30 phút”; “Duyệt, chạy tiếp”.

Owner (2026-11-11, vòng hai): “Không nối, đi đường cũ”; “Thói quen của user sẽ gửi hình trần và ghi chat tiếp theo. Nên phải gom nhóm để gửi model phân tích chính xác”.

Code chọn câu hỏi theo trường thiếu; **câu chữ** lấy từ khối template ghi ở cột "Khối template" (danh mục khối: `channels.md` · `blocks`). Model không viết câu hỏi. Câu hỏi **chỉ gửi ở group xử lý**, không bao giờ ở group gốc (`channels.md`).

Chỉ hỏi khi trạng thái là `NEED_INFO`. Không hỏi khi `MEDIA_ONLY`, `HOLD`, `CHAT`, `READY`, `APPEND`.

## Bảng: ask_order

Lấy các dòng có `intent` khớp và trường **chưa CÓ**, xếp theo "Thứ tự" tăng dần, bỏ khối trùng, lấy tối đa `max_fields_per_ask` khối, gộp thành **một tin**. Trường đã hỏi trong chủ đề này thì không hỏi lại (`max_asks_per_field`).

| Thứ tự | intent | Trường thiếu | Khối template |
|---|---|---|---|
| 1 | `bug` | `object` | `ask.object` |
| 2 | `bug` | `observed` | `ask.observed` |
| 1 | `task` | `object` | `ask.object` |
| 2 | `task` | `work` | `ask.work` |
| 1 | `feedback` | `problem` | `ask.problem` |

Feedback chỉ rơi vào `NEED_INFO` khi thiếu cả `problem` và `proposal`; khi đó hỏi `problem`.

## Bảng: fallback

| Khi | Khối template |
|---|---|
| `intent` không có dòng nào trong `ask_order` (gồm `unknown`), hoặc mọi trường thiếu đã hỏi | `question.fallback` |

## Bảng: limits

| Khóa | Giá trị | Nghĩa |
|---|---|---|
| `max_fields_per_ask` | `3` | Số khối hỏi tối đa trong một tin (TG-12) |
| `max_asks_per_field` | `1` | Mỗi trường chỉ hỏi một lần trong một chủ đề (chống hỏi lặp, TG-12) |
| `followup_window_minutes` | `30` | Tin cuối của topic phải mới hơn cửa sổ này để xét nối tin nguồn không Reply |

Không ai trả lời ⇒ pending hết hạn theo `states.md` · `limits` · `pending_expiry_days`, bot không nhắc.

## Bảng: source_followup

Chỉ rules mode, đường xử lý **một tin mới**; `_process_batch_inner` và batch nhiều tin ngoài phạm vi. Áp sau `early_decision` (F1/F3/F4/F5/F6); xã giao như “ok anh” không được kéo vào pending. Không đổi Reply thật, `answer_link` L1–L4, APPEND hoặc lifecycle.

| # | Bước |
|---|---|
| SF1 | Tin mới ở group nguồn: không Reply, không command theo `rules.COMMAND`, không forward, không document, biết sender. Mặc định bảo thủ chờ Owner xác nhận: nếu code_facts của riêng tin có team_mention_ids theo team_request T3–T4 thì không xét nối và không gọi confirmation; đường cũ vẫn trích intent rồi áp T2/F2 (bug/feedback không tự thành CHAT). Tìm topic cùng `source_chat_id`, `OPEN`, không có tiền tố `batch:`, phase khác `LEGACY_REVIEW` và `HOLD`, không nằm trong `rules_inflight_topics`, không chứa document; quyết định ghi gần nhất là `NEED_INFO` hoặc `MEDIA_ONLY`; có ít nhất một tin nguồn cùng group từ chính sender đó; khoảng cách từ received_at nguồn cuối tới received_at tin mới không âm và nhỏ hơn `followup_window_minutes`, không dùng thời điểm xử lý last_at/store.clock. Với `MEDIA_ONLY`, toàn bộ nguồn phải là ảnh trần không forward, cùng sender và cùng group. Đếm chung hai trạng thái: chỉ đúng một topic khớp mới là candidate; không hoặc nhiều topic thì đi đường topic mới. |
| SF2 | Gộp tạm tin mới với toàn bộ nguồn candidate; gọi `evaluate_batch`, `allow_mixed=True`, dùng `asked_counts` của topic. Ngay sau extraction, nếu có intent STATUS/ANALYZE hoặc dữ kiện lifecycle thì không nối và đi đường cũ, kể cả ANALYZE có hoặc không có task; không chuyển thành unknown để hỏi lại. Chỉ đúng một nhóm đủ mọi tin mới được xét tiếp. Split, lỗi hoặc request quá lớn: không nối, tiếp tục đường topic mới; topic cũ không đổi snapshot/attempts, không HOLD, không thêm nguồn, không notice. |
| SF3 | Nếu nhóm gộp có ảnh trần, provider phải hỗ trợ vision; mọi ảnh trần phải được `prepare_images` chấp nhận và pixel thực sự có trong request confirmation thành công, kể cả sau fallback provider. Thiếu bất kỳ ảnh trần nào do không vision, ảnh quá lớn/sai/tải lỗi hoặc vượt budget ảnh thì không nối. Không có candidate: ảnh trần vẫn R3 MEDIA_ONLY, không gọi model. Áp cho ảnh trước rồi text, text trước rồi ảnh, hoặc nhiều ảnh trần rồi text; cùng sender/thời gian chỉ chọn candidate, không thay thế xác nhận có pixel của model (TG-05.7/TG-15). |
| SF4 | Trước IO confirmation, checkpoint HOLD chỉ thuộc tin mới để startup gate và replay an toàn; không ghi topic cũ. Không nối: bỏ lượt confirmation khỏi bộ đếm retry đúng một lần; khi đường cũ có trạng thái bền vững thì bàn giao cả HOLD, xóa checkpoint phụ chưa có decision nếu extensions đã sở hữu batch topic, hoặc giữ nguyên retry của topic thật. Không còn hai OPEN/HOLD cùng retry một tin; số lần gọi model mỗi sweep, escalation và notice của đường cũ giống tin không có candidate. Khi SF2–SF3 đạt, recheck candidate/snapshot rồi commit claim, thêm source đúng một lần, journal và replay plan trong một transaction SQLite. Crash trước commit không đổi topic cũ; sau commit replay facts đã lưu, không ghép đôi tin hoặc extraction lại, không lặp ticket/notice và giải phóng claim khi xong. Startup gate chỉ đọc chặn plan source-followup PLANNED không thể replay: nguồn error/quarantine hoặc sẽ hết attempts khi recovery, topic/nguồn thiếu hay đổi, plan sai/không kiểm đủ, claim thuộc operation khác, case/group có reservation hoặc membership không còn đúng, nguồn READY không còn khả dụng. Plan PLANNED khỏe vẫn qua gate và replay; không đổi hành vi các batch khác. Worker gặp lỗi completion thông thường thì quarantine tin và phát RulesHalt ngay, kể cả OperationalError; process chết không bị biến thành lỗi quarantine. Sweep giữ thứ tự expire_due rồi HOLD trước replay; chỉ lỗi transient không phải RulesHalt được hoãn riêng, nhóm còn queued thuộc worker, worker nhận cả nhóm dù dequeue nguồn cũ trước. Checkpoint DONE trả kết quả lưu trước khi kiểm fingerprint. Gate chỉ có connection, không có RulesSnapshot đang chạy: fingerprint thay đổi chỉ bị phát hiện ở worker/sweep và vẫn RulesHalt. Owner kiểm tra/reconcile theo hướng dẫn dưới; không tự xóa claim, reset attempts/reservation hoặc sửa plan để vượt gate. |
| SF5 | Khi đã nối, chạy readiness từ R1: `READY` tạo một ticket đủ các tin; `NEED_INFO` chỉ hỏi trường chưa đạt `max_asks_per_field`, fallback đã hỏi thì im lặng; `CHAT` hoàn tất nguồn và đóng topic; `MEDIA_ONLY` giữ pending. Dry-run không gửi notice. |

Khi SF4 dừng: giữ bot dừng, lấy bản sao SQLite nhất quán. `python -m tg_triage legacy-preflight --db <offline-copy>` cho inventory/recovery HOLD; `trace` giúp đối chiếu nguồn/issue/notice hiện có nhưng không hiển thị payload plan `source-followup:`. Repo chưa có CLI hiển thị chi tiết hay repair plan này. Owner cần inspect SQLite ở `mode=ro`: header/fingerprint trong `rules_batches`, JSON `rules_batch_groups.plan`, `rules_sources`, trạng thái/attempts/issue metadata của `messages`, `rules_inflight_topics`, reservation trong `cases`/`groups` và notice/issue đã gửi. So fingerprint lưu với output `check-config` của cấu hình dự định chạy. Xác minh outcome external và nguồn từ bằng chứng trước khi duyệt reconciliation/bản sửa; chỉ khởi động lại khi gate CLEAR và fingerprint phù hợp. Không dùng `legacy-transition`/`legacy-release` để chữa plan followup; các lệnh đó không repair loại checkpoint này.

Confirmation không nối có thể tốn hai decision turns riêng: confirmation và đường cũ. Mỗi turn rules một phần có trần 3 provider attempts/40 giây (`configure_rules_parts(1)`); tổng trần 6 attempts/80 giây deadline model, không phải SLA toàn luồng tải ảnh/IO. Mỗi request text tối đa 8.000 byte local hoặc 24.000 byte cloud; không nới budget của turn cũ và không split confirmation thành nhiều phần. Không có document nên không áp phần mở rộng budget tài liệu.

## Bảng: answer_link

Tin ở group xử lý chỉ được nối về chủ đề theo bảng này. Đọc từ trên xuống, dòng đầu khớp là kết quả.

| # | Kênh | Là Reply vào | Pending | Kết quả |
|---|---|---|---|---|
| L1 | `ops` | Tin hỏi NEED_INFO của bot đã gửi thành công cho chủ đề đó | `OPEN` | `LINK` |
| L2 | `ops` | Tin hỏi của bot đã gửi thành công cho chủ đề đó | `EXPIRED` `CLOSED` | `IGNORE` |
| L3 | `ops` | Tin khác của bot (link ticket, báo lỗi) hoặc tin của người | * | `IGNORE` |
| L4 | `ops` | Không Reply tin nào | * | `IGNORE` |

- `LINK`: tin trả lời NEED_INFO (chữ và ảnh) được gộp vào chủ đề, `reply_target` = `bot_question`, chạy lại `readiness.md` từ R1. Reply câu hỏi vòng đời theo `lifecycle.md`, Reply câu hỏi thiếu target STATUS theo `analysis-status.md`; không chạy lại readiness.
- `IGNORE`: không xử lý, không tạo chủ đề mới, không trả lời.
- Group xử lý **không** phải group nguồn: tin mới ở đây không bao giờ thành ticket mới.
