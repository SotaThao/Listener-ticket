<!-- rule-file: examples · version 20 · status ACTIVE · audience: code -->
# Ví dụ chuẩn

## Bảng: source_followup_cases

Các ca SF chỉ áp cho tin đơn ở rules mode (`questions.md` · `source_followup`); không gửi bảng này cho model. Các tin và sender trong bảng đều giả định.

| # | Tin | Bối cảnh | Kết quả | Luật |
|---|---|---|---|---|
| SF1 | “POS kẹt giấy” | Cùng người gửi, một topic OPEN NEED_INFO, sau 5 phút; model gộp đủ nguồn, intent bug có object và observed | Nối, một ticket đủ hai tin `READY` | SF1+SF2+SF5, R9 |
| SF2 | “chỗ đó vẫn vậy” | Cùng người gửi, một topic OPEN NEED_INFO, sau 5 phút; model gộp đủ nguồn nhưng vẫn unknown; fallback đã hỏi | Nối, vẫn `NEED_INFO`, không hỏi lại | SF1+SF2+SF5, R13 |
| SF3 | “thêm mục xuất file” | Candidate hợp lệ nhưng model tách thành hai nhóm | Topic mới; topic cũ nguyên trạng kể cả attempts, không notice cho topic cũ | SF2 |
| SF4 | “POS kẹt giấy” | Khác người gửi với mọi tin nguồn của topic | Topic mới | SF1 |
| SF5 | “POS kẹt giấy” | Cùng người gửi nhưng tin cuối đã 31 phút (từ 30 phút trở lên đều hết cửa sổ) | Topic mới | SF1 |
| SF6 | “POS kẹt giấy” | Có hai topic OPEN NEED_INFO cùng người gửi còn trong cửa sổ | Topic mới | SF1 |
| SF7 | Reply “POS kẹt giấy” | Reply thật vào nguồn pending | Theo đường source Reply hiện hữu | SF1 |
| SF8 | “ok anh” | Cùng người gửi với một pending còn trong cửa sổ | `CHAT` trước khi tìm candidate, không gọi model | F4 |
| SF9 | “chỗ đó vẫn vậy” | Candidate hợp lệ, model gộp đủ nguồn, dry-run | Trả pending_review hoặc dry_run_issue theo readiness, không gửi notice | SF5 |
| SF10 | “ticket này tới đâu?” | Cùng sender có candidate, extraction là status | Không nối, topic cũ nguyên trạng; kết quả/notice STATUS giống sender khác | SF2 |
| SF11 | “phân tích phần bị cắt” | Cùng sender có candidate, extraction analyze có task | Không nối, chạy ANALYZE theo đường cũ | SF2 |
| SF12 | “phân tích giúp” | Extraction analyze không task, extensions; chạy nhiều sweep | Không nối; một HOLD topic, số lần gọi model/retry/escalation/notice từng sweep giống sender khác, không đổi thành R13 trên topic cũ | SF2+SF4 |
| SF13 | Ảnh trần rồi “POS kẹt giấy” | Một MEDIA_ONLY cùng sender còn cửa sổ; provider vision nhận pixel; model một nhóm READY | Một ticket có cả ảnh lẫn text và ảnh đính kèm | SF1+SF2+SF3+SF5 |
| SF14 | Ảnh trần rồi text | Provider không vision | Text thành topic mới; MEDIA_ONLY cũ nguyên trạng | SF3 |
| SF15 | Text rồi ảnh trần | NEED_INFO còn cửa sổ, pixel được gửi, model một nhóm | Nối và áp readiness | SF3+SF5 |
| SF16 | Text rồi ảnh trần | Pixel thiếu do không vision/tải lỗi/sai ảnh/quá lớn | Không nối; ảnh riêng MEDIA_ONLY R3 không model call cho ảnh | SF3 |
| SF17 | Hai ảnh trần rồi text | Cùng sender, mọi pixel đều gửi; model xác nhận mỗi lượt | Hai ảnh cùng MEDIA_ONLY rồi một ticket đủ ảnh và text | SF1+SF3+SF5 |
| SF18 | Tin có document | Có một candidate cùng sender | Không xét nối; đường cũ để Reply sau không bị HOLD vì document mới được nối | SF1 |
| SF19 | “/unknown payload” | Không khớp rules.COMMAND, có candidate hợp lệ | Được xét confirmation như text thông thường | SF1 |
| SF20 | Confirmation đang gọi model thì process chết | Topic mới có HOLD checkpoint, topic cũ chưa ghi | Gate startup chấp nhận; replay/sweep an toàn, topic cũ nguyên trạng | SF4 |
| SF21 | Link commit rồi process chết trước completion hoặc trước đánh DONE | Replay plan đã lưu | Không ghép tin hai lần, không thêm ticket/notice, không còn inflight claim | SF4 |
| SF22 | Ảnh trần thứ hai | Vượt giới hạn ảnh của prepare_images nên một ảnh không có pixel | Không nối, không dùng cùng sender để xác nhận ảnh chưa xem | SF3 |
| SF23 | Text mới | Đồng thời có một NEED_INFO và một MEDIA_ONLY cùng sender hợp lệ | Hai candidate đếm chung nên topic mới | SF1 |
| SF24 | Extraction có lifecycle | Có candidate hợp lệ | Không nối, đi đường cũ | SF2 |
| SF25 | “@tuan_dev check log dang nhap giup anh” | Riêng tin có team mention T3–T4; đường cũ trích task | Không confirmation; CHAT/F2 giống sender khác; mặc định chờ Owner xác nhận | SF1, F2 |
| SF26 | Hai tin cách nhau hai giờ, xử lý liên tiếp sau downtime | last_at mới nhưng received_at hai tin cách nhau ngoài cửa sổ | Không nối; nếu received_at chỉ cách 5 phút thì vẫn được xét dù xử lý trễ | SF1 |
| SF27 | Provider lỗi hoặc request quá lớn | Có candidate; đường cũ HOLD/escalate, hoặc chết giữa confirmation rồi retry HOLD | Không còn HOLD checkpoint phụ; nhiều sweep có trạng thái/retry/notice giống sender khác ở cả hai mode | SF4 |
| SF28 | Link READY rồi chết trong create_single | Restart requeue cả hai nguồn, sweep chạy trước worker; hoặc replay lỗi tạm thời | Expiry/HOLD vẫn chạy, sweep không văng lỗi; nhường queued cho worker, retry replay sau, đúng một ticket | SF4 |
| SF29 | Tin đã có checkpoint DONE được replay sau đổi rules | Fingerprint mới khác fingerprint kết quả đã hoàn tất | Trả kết quả đã lưu, không gọi model/tạo ticket/notice lần nữa | SF4 |
| SF30 | Một OperationalError khi resume link READY | Lỗi thông thường trong completion, không phải process chết | Worker quarantine và RulesHalt ngay; startup gate chặn, không chờ sweep hay restart loop | SF4 |
| SF31 | Hai lần process chết khi replay | Nguồn đã hết attempts hoặc thành error/quarantine | Gate RulesHalt trước khi phục vụ; không reset attempts để chạy lại | SF4 |
| SF32 | Nguồn/claim/case của plan thay đổi hoặc không còn khả dụng | Plan source-followup PLANNED không thể replay an toàn | Gate và worker chặn, Owner inspect ledger và outcome; không tự bỏ claim/reservation | SF4 |
| SF33 | Process chết sau commit link, nguồn và reservation còn sạch | PLANNED khỏe | Gate OK, ambiguity False, replay đúng một ticket không gọi model lại; batch khác giữ hành vi HEAD | SF4 |
| SF34 | Fingerprint plan khác rules đang chạy | Gate chỉ có connection, không có running RulesSnapshot | Gate không so fingerprint; worker/sweep RulesHalt, Owner đối chiếu header với check-config trước restart | SF4 |

<!-- SF-test SF1: test_link_ready_creates_exactly_one_ticket_with_both_messages -->
<!-- SF-test SF2: test_link_need_info_sends_no_second_fallback -->
<!-- SF-test SF3: test_model_splits_new_topic_old_snapshot_and_attempts_byte_identical -->
<!-- SF-test SF4: test_different_sender_creates_new_topic -->
<!-- SF-test SF5: test_older_than_window_and_exact_boundary_create_new_topic -->
<!-- SF-test SF6: test_two_matching_open_topics_create_new_topic -->
<!-- SF-test SF7: test_reply_uses_existing_path_even_with_other_sender_after_window -->
<!-- SF-test SF8: test_courtesy_filtered_before_candidate_or_model -->
<!-- SF-test SF9: test_dry_run_sends_nothing_for_need_info_or_ready -->
<!-- SF-test SF10: test_readonly_intents_preserve_old_topic_and_match_different_sender -->
<!-- SF-test SF11: test_readonly_intents_preserve_old_topic_and_match_different_sender -->
<!-- SF-test SF12: test_analyze_without_task_multi_sweep_matches_different_sender -->
<!-- SF-test SF13: test_image_first_text_second_seen_image_ready_uploads_one_ticket -->
<!-- SF-test SF14: test_image_first_without_vision_text_is_new -->
<!-- SF-test SF15: test_text_first_seen_image_is_linked -->
<!-- SF-test SF16: test_rejected_or_missing_image_is_not_linked,test_unseen_bare_image_does_not_link_or_call_model -->
<!-- SF-test SF17: test_two_bare_images_then_text_are_confirmed_together -->
<!-- SF-test SF18: test_document_is_excluded_before_confirmation -->
<!-- SF-test SF19: test_slash_text_that_is_not_command_can_link -->
<!-- SF-test SF20: test_confirmation_death_leaves_recoverable_new_hold_old_unchanged,test_confirmation_death_retry_hold_multi_sweep_matches_different_sender -->
<!-- SF-test SF21: test_committed_link_replays_without_model_or_duplicate_effect -->
<!-- SF-test SF22: test_image_budget_does_not_allow_unseen_bare_image_link -->
<!-- SF-test SF23: test_need_info_and_media_only_count_together_for_ambiguity -->
<!-- SF-test SF24: test_lifecycle_confirmation_is_explicitly_not_linked -->
<!-- SF-test SF25: test_team_request_matches_different_sender_without_confirmation -->
<!-- SF-test SF26: test_received_timestamps_control_window_despite_processing_backlog -->
<!-- SF-test SF27: test_provider_down_multi_sweep_matches_different_sender,test_request_too_large_multi_sweep_matches_different_sender,test_confirmation_death_retry_hold_multi_sweep_matches_different_sender -->
<!-- SF-test SF28: test_sweep_yields_queued_committed_group_to_worker_and_expires,test_transient_replay_failure_is_isolated_after_expiry_and_hold -->
<!-- SF-test SF29: test_done_checkpoint_survives_rules_fingerprint_change -->
<!-- SF-test SF30: test_followup_operational_error_halts_worker_and_restart_gate_immediately -->
<!-- SF-test SF31: test_two_crashes_quarantined_link_member_halts_instead_of_deferring -->
<!-- SF-test SF32: test_followup_sources_change_halts_gate_and_worker_before_sweep,test_followup_gate_rejects_other_unreplayable_stored_states -->
<!-- SF-test SF33: test_healthy_followup_plan_passes_gate_and_replays_once,test_no_followup_gate_and_ambiguity_match_head_for_all_batch_states -->
<!-- SF-test SF34: test_followup_fingerprint_comparison_remains_at_worker_and_sweep -->

Bộ test cố định (`listener/tests/test_rules_markdown.py`): cho **dữ kiện** đầu vào, áp `facts.md` → `readiness.md` → `questions.md` phải ra đúng **Kết quả**, **Luật**, **Hỏi** — không phụ thuộc model. Mỗi luật R1–R13 có ít nhất một ví dụ.

X26, X27 chỉ gửi model khi nhóm đang đùa (`facts.md` · `group_mood`): X26 dạy câu nghe như góp ý nhưng nằm trong mạch đùa là `chat`, X27 là câu tương tự khi nhóm không đùa vẫn là `feedback`.

Khi gửi model làm ví dụ trích dữ kiện, bot chỉ gửi cột "Tin (rút gọn)", "intent (model)" và "Dữ kiện" — **không** gửi các cột còn lại.

Cách ghi cột "Dữ kiện": `trường=giá trị` cách nhau bằng `;`; `∅` = model không trả trường nào. Đuôi đánh dấu nguồn:

- `[ảnh-đã-xem]`: giá trị đọc từ ảnh đã gửi model — hợp lệ.
- `[ảnh-chưa-xem]`: giá trị chỉ có từ ảnh model chưa xem — **không** hợp lệ (V4).
- `[ngoài-chủ-đề]`: `source_ids` không thuộc chủ đề — **không** hợp lệ (V2).

## Bảng: examples

| # | Tin (rút gọn) | command | command_payload | text | media_seen | media_unseen | extraction_ok | reply_target | intent (model) | Dữ kiện | Kết quả | Luật | Hỏi |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| X1 | "POS không in được receipt, cả app và web, máy in có dây" (#47) | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `bug` | object=máy in POS; observed=không in được receipt | `READY` | R9 | — |
| X2 | "Login đúng pass mà không login được" + ảnh "Invalid username or password" | `∅` | `none` | `có` | 1 | 0 | `true` | `none` | `bug` | object=đăng nhập; observed=báo sai mật khẩu dù nhập đúng [ảnh-đã-xem] | `READY` | R9 | — |
| X3 | Ảnh panel "Salon information & visibility" + "design đổi bảng này" (#46) | `∅` | `none` | `có` | 1 | 0 | `true` | `none` | `task` | object=panel Salon information & visibility [ảnh-đã-xem]; work=design lại | `READY` | R10 | — |
| X4 | "bitcoinnailbar.com/#gallery sao những hình Nail trước đó em không mang qua" (#82) | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `task` | object=gallery bitcoinnailbar; work=mang hình Nail trước đó qua | `READY` | R10 | — |
| X5 | "Nên có bộ lọc theo ngày ở giao dịch" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `feedback` | proposal=bộ lọc theo ngày ở giao dịch | `READY` | R11 | — |
| X6 | "Nút này lỗi" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `bug` | object=nút này; observed=lỗi | `NEED_INFO` | R12 | `ask.object` `ask.observed` |
| X7 | "Check giúp em cái này" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `task` | object=cái này; work=check giúp | `NEED_INFO` | R12 | `ask.object` `ask.work` |
| X8 | Một ảnh, không chữ | `∅` | `none` | `không` | 1 | 0 | `true` | `none` | `unknown` | ∅ | `MEDIA_ONLY` | R3 | — |
| X9 | "Dạ để em check" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `chat` | ∅ | `CHAT` | R7 | — |
| X10 | "/question API có rate limit không?" | `/question` | `text` | `có` | 0 | 0 | `true` | `none` | `question` | ∅ | `CHAT` | R1 | — |
| X11 | "/bug Thanh toán bị trừ tiền 2 lần" | `/bug` | `text` | `có` | 0 | 0 | `true` | `none` | `bug` | object=thanh toán; observed=bị trừ tiền 2 lần | `READY` | R9 | — |
| X12 | "/task" không kèm gì | `/task` | `empty` | `không` | 0 | 0 | `true` | `none` | `unknown` | ∅ | `CHAT` | R2 | — |
| X13 | "/bug" + ảnh lỗi `COMMON_VALIDATION_ERROR` ở màn đặt lịch | `/bug` | `media` | `không` | 1 | 0 | `true` | `none` | `unknown` | object=màn đặt lịch [ảnh-đã-xem]; observed=báo COMMON_VALIDATION_ERROR [ảnh-đã-xem] | `READY` | R9 | — |
| X14 | "Màn thanh toán bị treo" — gọi model lỗi | `∅` | `none` | `có` | 0 | 0 | `false` | `none` | `∅` | ∅ | `HOLD` | R4 | — |
| X15 | Reply vào tin của ticket open: "vẫn bị" | `∅` | `none` | `có` | 0 | 0 | `true` | `ticket_open` | `bug` | observed=vẫn bị | `APPEND` | R6 | — |
| X16 | Reply vào tin của ticket open: "Cảm ơn em" | `∅` | `none` | `có` | 0 | 0 | `true` | `ticket_open` | `chat` | ∅ | `CHAT` | R5 | — |
| X17 | "Em hỏi chút, API có rate limit không?" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `question` | ∅ | `CHAT` | R8 | — |
| X18 | "Thêm được export CSV ở màn báo cáo không anh?" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `question` | object=màn báo cáo; work=thêm export CSV | `READY` | R10 | — |
| X19 | "Cái này sao vậy anh" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `unknown` | object=cái này | `NEED_INFO` | R13 | `question.fallback` |
| X20 | "Máy in POS in ra giấy trắng" + 7 ảnh (5 đã xem, 2 chưa xem) | `∅` | `none` | `có` | 5 | 2 | `true` | `none` | `bug` | object=máy in POS; observed=in ra giấy trắng | `READY` | R9 | — |
| X21 | "Lỗi ở màn này" + 1 ảnh model chưa xem | `∅` | `none` | `có` | 0 | 1 | `true` | `none` | `bug` | object=màn thanh toán [ảnh-chưa-xem]; observed=lỗi | `NEED_INFO` | R12 | `ask.object` `ask.observed` |
| X22 | Trả lời câu hỏi của bot ở group xử lý: "nút Thanh toán, bấm không phản hồi" | `∅` | `none` | `có` | 0 | 0 | `true` | `bot_question` | `bug` | object=nút Thanh toán; observed=bấm không phản hồi | `READY` | R9 | — |
| X23 | "Màn giao dịch không ổn" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `feedback` | object=màn giao dịch; problem=không ổn | `NEED_INFO` | R12 | `ask.problem` |
| X24 | "Không in được" — đối tượng model lấy từ tin của chủ đề khác | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `bug` | object=máy in POS [ngoài-chủ-đề]; observed=không in được | `NEED_INFO` | R12 | `ask.object` |
| X25 | "Mấy anh ơi, link zoom daily 10h-11h các anh có dùng không? Báo giùm em nha" — hỏi ý cả team, người khác trả lời ngay trong group (#115) | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `question` | object=link zoom daily 10h-11h; work=báo giùm em | `CHAT` | R8 | — |
| X26 | Nhóm đang đùa (nhiều tin 😂 😆 ngay trước, `group_mood`): "Thiếu tính năng fake mắt xem rồi" (#118) | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `chat` | ∅ | `CHAT` | R7 | — |
| X27 | Nhóm không đùa: "Thiếu nút xuất Excel ở màn báo cáo" | `∅` | `none` | `có` | 0 | 0 | `true` | `none` | `feedback` | object=màn báo cáo; proposal=thêm nút xuất Excel | `READY` | R11 | — |

## Bảng: prefilter_cases

Bot chạy bảng này mỗi lần nạp rules (`readiness.md` · `prefilter`). `CHAT` = khớp lọc trước, không gọi model, không ticket; `không` = đi tiếp như cũ. PF1, PF2 là dạng ẩn danh của tin live đã thành ticket #95, #111.

| # | Tin | Kết quả |
|---|---|---|
| PF1 | review giúp em nha anh @tên https://github.com/org/repo/pull/2585 | `CHAT` |
| PF2 | https://github.com/org/repo/pull/1427 anh @tên review giúp em pr nha | `CHAT` |
| PF3 | anh merge PR này giúp em https://github.com/org/repo/pull/12 | `CHAT` |
| PF4 | xem issue này giúp em https://github.com/org/repo/issues/80 | `CHAT` |
| PF5 | PR #12 rv giúp em với 🙏 https://github.com/org/repo/pull/12 | `CHAT` |
| PF6 | merge PR này xong màn Queue bị trắng https://github.com/org/repo/pull/12 | `không` |
| PF7 | anh xem issue này https://github.com/org/repo/issues/80, khách bị trừ tiền 2 lần | `không` |
| PF8 | review giúp em nha anh | `không` |
| PF9 | review giúp em https://gitlab.com/org/repo/-/merge_requests/3 | `không` |
| PF10 | https://github.com/org/repo/pull/12 | `không` |
| PF11 | sửa lại PR này giúp em https://github.com/org/repo/pull/12 | `không` |
| PF12 | review giúp em https://github.com/org/repo | `không` |
| PF13 | anh xem giúp https://github.com/org/repo/issues/80,lỗi | `không` |
| PF14 | review giúp @tên,lỗi https://github.com/org/repo/pull/12 | `không` |
| PF15 | anh check giúp em https://staging.example.com/pos/checkout https://github.com/org/repo/issues/80 | `không` |
| PF16 | fix giúp em PR này https://github.com/org/repo/pull/12 | `không` |
| PF17 | làm giúp em issue này https://github.com/org/repo/issues/80 | `không` |
| PF18 | test giúp em PR này https://github.com/org/repo/pull/12 | `không` |
| PF19 | deploy PR này giúp em https://github.com/org/repo/pull/12 | `không` |
| PF20 | PR này merge chưa anh https://github.com/org/repo/pull/12 | `không` |
| PF21 | [PR](https://github.com/org/repo/pull/12) review giúp em | `CHAT` |
| PF22 | review giúp em https://github.com/org/repo/pull/12/files | `CHAT` |
| PF23 | PR#12 review giúp em https://github.com/org/repo/pull/12 | `CHAT` |

## Bảng: link_cases

Bot chạy bảng này mỗi lần nạp rules (`readiness.md` · `link_only`). `CHAT` = chỉ có link, không gọi model; `không` = đi tiếp như cũ. LC1 là tin live 6435. Bảng này **không** gửi cho model.

| # | Tin | Kết quả |
|---|---|---|
| LC1 | https://www.onorca.dev/ | `CHAT` |
| LC2 | www.example.com/tools 👍 | `CHAT` |
| LC3 | https://a.example.com/x https://b.example.com/y | `CHAT` |
| LC4 | ​https://example.com/blog​ | `CHAT` |
| LC5 | (https://example.com/a), https://example.com/b! | `CHAT` |
| LC6 | lỗi https://app.example.com/checkout | `không` |
| LC7 | https://app.example.com/checkout bị trắng | `không` |
| LC8 | check https://example.com | `không` |
| LC9 | example.com/pricing | `không` |
| LC10 | https://example.com/a 2 | `không` |
| LC11 | xem | `không` |
| LC12 | https://pay.example.vn/checkout,lỗi | `không` |
| LC13 | https://example.com/lỗi | `không` |
| LC14 | https://example.com/a;bị trắng | `không` |

## Bảng: chat_cases

Bot chạy bảng này mỗi lần nạp rules (`readiness.md` · `chat_only`). `CHAT` = chỉ có từ xã giao, không gọi model; `không` = đi tiếp như cũ. Bảng này **không** gửi cho model.

| # | Tin | Kết quả |
|---|---|---|
| CC1 | ok anh | `CHAT` |
| CC2 | Dạ em cảm ơn ạ! | `CHAT` |
| CC3 | 👍 | `CHAT` |
| CC4 | xong rồi nha @devmember02 | `CHAT` |
| CC5 | haha | `CHAT` |
| CC6 | Oke team, thanks nhiều 🙏 | `CHAT` |
| CC7 | vẫn không được | `không` |
| CC8 | ok anh, lỗi rồi | `không` |
| CC9 | ok 2 | `không` |
| CC10 | ok https://example.com | `không` |
| CC11 | được chưa anh | `không` |
| CC12 | xong rồi, nhưng màn Queue bị trắng | `không` |
| CC13 | xong rồi à anh? | `không` |
| CC14 | ok? | `không` |

## Bảng: banter_cases

Bot chạy bảng này mỗi lần nạp rules (`readiness.md` · `banter_only`). `Bối cảnh` là các tin ngay trước, cũ đến mới, mỗi tin cách nhau một phút, dạng `Tên: tin ; Tên: tin` (tên khác nhau là người gửi khác nhau); `—` là chưa có tin nào. BC1 là dạng ẩn danh của tin live đã thành ticket #119; BC5 là dạng của #118 — không có dấu cười trong tin nên **không** khớp luật cục bộ, model đọc bối cảnh theo `facts.md`. Bảng này **không** gửi cho model.

| # | Tin | Bối cảnh | Kết quả |
|---|---|---|---|
| BC1 | để e mở tab ẩn danh login 10 acc test vào xem live 😁 | Ngọc: em thấu hiểu tâm lý người dùng mà 😆 ; LEO: 😂😂😂 ; Men: Thiếu tính năng fake mắt xem rồi ; Men: Kéo dòng họ vô xem live nè | `CHAT` |
| BC2 | để e mở tab ẩn danh login 10 acc test vào xem live 😁 | — | `không` |
| BC3 | để e mở tab ẩn danh login 10 acc test vào xem live 😁 | Ngọc: 😆 ; Ngọc: haha ; Men: Thiếu tính năng fake mắt xem rồi | `không` |
| BC4 | haha vậy là xong demo live luôn | Ngọc: em thấu hiểu tâm lý người dùng mà 😆 ; LEO: 😂😂😂 ; Men: Thiếu tính năng fake mắt xem rồi ; Men: Kéo dòng họ vô xem live nè | `CHAT` |
| BC5 | Thiếu tính năng fake mắt xem rồi | Ngọc: em thấu hiểu tâm lý người dùng mà 😆 ; LEO: 😂😂😂 ; Men: Thiếu tính năng fake mắt xem rồi ; Men: Kéo dòng họ vô xem live nè | `không` |
| BC6 | Màn Queue không in được bill 😄 | Ngọc: em thấu hiểu tâm lý người dùng mà 😆 ; LEO: 😂😂😂 ; Men: Thiếu tính năng fake mắt xem rồi ; Men: Kéo dòng họ vô xem live nè | `không` |
| BC7 | lỗi lắm 😂 | Ngọc: em thấu hiểu tâm lý người dùng mà 😆 ; LEO: 😂😂😂 ; Men: Thiếu tính năng fake mắt xem rồi ; Men: Kéo dòng họ vô xem live nè | `không` |
| BC8 | để e mở tab ẩn danh login 10 acc test vào xem live 😁 | Ngọc: 😆 ; LEO: ok anh ; Men: Thiếu tính năng fake mắt xem rồi | `không` |
| BC9 | nạp tiền bị trừ 2 lần mà ví vẫn 0đ 😄 | Ngọc: em thấu hiểu tâm lý người dùng mà 😆 ; LEO: 😂😂😂 ; Men: Thiếu tính năng fake mắt xem rồi ; Men: Kéo dòng họ vô xem live nè | `không` |
| BC10 | app bi loi roi a 😄 | Ngọc: em thấu hiểu tâm lý người dùng mà 😆 ; LEO: 😂😂😂 ; Men: Thiếu tính năng fake mắt xem rồi ; Men: Kéo dòng họ vô xem live nè | `không` |
| BC11 | xem live nè 😄 | Ngọc: prod sập rồi =)) ; LEO: lại lỗi 😂 | `không` |
| BC12 | Màn Queue k in được bill 😅 | Ngọc: em thấu hiểu tâm lý người dùng mà 😆 ; LEO: 😂😂😂 ; Men: Thiếu tính năng fake mắt xem rồi ; Men: Kéo dòng họ vô xem live nè | `không` |

## Bảng: reply_chat_cases

Bot chạy bảng này mỗi lần nạp rules (`readiness.md` · `reply_chat`). `Reply vào`: `CHAT` = tin được Reply vào do người khác gửi và đã quyết `CHAT` bằng `F2`; `CHAT-self` = như vậy nhưng do chính người gửi tin đang xét; `CHAT-F1` = đã quyết `CHAT` bằng luật ngoài `F2`/`F4`/`F5` (vd nhờ review PR); `ticket` = đã tạo ticket; `none` = không phải Reply. RC1 là dạng ẩn danh của tin 6508 (ticket #121). Bảng này **không** gửi cho model.

| # | Tin | Reply vào | Kết quả |
|---|---|---|---|
| RC1 | lát họp xong nói qua v3 này nha anh Mến | `CHAT` | `CHAT` |
| RC2 | lát họp xong nói qua v3 này nha anh Mến | `ticket` | `không` |
| RC3 | lát họp xong nói qua v3 này nha anh Mến | `none` | `không` |
| RC4 | ok anh để em xem | `CHAT` | `CHAT` |
| RC5 | vẫn treo màn login nè anh | `CHAT` | `không` |
| RC6 | mất tiền rồi anh | `CHAT` | `không` |
| RC7 | /task cập nhật màn báo cáo | `CHAT` | `không` |
| RC10 | rồi giờ call ct kéo ảnh vào zoom cho ảnh review đi @Sellisloss =)) | `CHAT-self` | `không` |
| RC11 | Cần thêm nút xuất Excel ở màn báo cáo | `CHAT-F1` | `không` |
| RC12 | Cần thêm nút xuất Excel ở màn báo cáo | `CHAT` | `CHAT` |

## Bảng: image_split_cases

Bot chạy bảng này mỗi lần nạp rules (`readiness.md` · `image_split`). `Nhóm` là các tin model đã gộp, `Tên: tin ; Tên: tin`, `[ảnh]` = ảnh trần, `↩k` = Reply vào tin thứ k; `Kết quả` là số thứ tự các tin bị tách ra (`không` nếu giữ nguyên). IS1 là dạng ẩn danh của nhóm 6507 + 6508 (ticket #121). Bảng này **không** gửi cho model.

| # | Nhóm | Kết quả |
|---|---|---|
| IS1 | Ngọc: [ảnh] ; Mai: lát họp xong nói qua v3 này nha anh Mến | `1` |
| IS2 | Ngọc: [ảnh] ; Ngọc: staging lỗi timeout nè | `không` |
| IS3 | Ngọc: [ảnh] ; Mai: đây là ảnh lỗi login ↩1 | `không` |
| IS4 | Ngọc: [ảnh] ; Mai: ảnh nè ; Ngọc: xem giúp em | `không` |
| IS5 | Ngọc: [ảnh] ; Mai: [ảnh] | `1,2` |
| IS6 | Ngọc: [ảnh] | `không` |

## Bảng: team_cases

Bot chạy bảng này mỗi lần nạp rules (`readiness.md` · `team_request`). Mỗi ca coi như model đã trả `intent` ở cột "intent (model)" và đủ `object`, `work`, `observed`, `problem`; cột "Luật" là luật phải ra. TC1, TC2 là dạng ẩn danh của tin live đã thành ticket #112, #113. Bảng này **không** gửi cho model.

| # | Tin | tag bot | intent (model) | Luật |
|---|---|---|---|---|
| TC1 | @teamlead01 e qua phụ @devmember02 làm ticket chỉnh sửa sidebar nha | `không` | `task` | F2 |
| TC2 | anh @leaddev01 check giúp em key MAPS_API_KEY ở staging có chưa anh | `không` | `question` | F2 |
| TC3 | @devmember02 thêm nút xuất Excel ở báo cáo | `không` | `task` | F2 |
| TC4 | @devmember02 màn thanh toán bị trắng khi bấm Pay | `không` | `bug` | R9 |
| TC5 | @devmember02 khách góp ý màn báo cáo khó đọc | `không` | `feedback` | R11 |
| TC6 | @botaccount01 thêm nút xuất Excel ở báo cáo | `có` | `task` | R10 |
| TC7 | /task @devmember02 thêm nút xuất Excel ở báo cáo | `không` | `task` | R10 |
| TC8 | thêm nút xuất Excel, gửi file cho ngan@example.com | `không` | `task` | R10 |
| TC9 | @dev1 thêm nút xuất Excel ở báo cáo | `không` | `task` | R10 |
| TC10 | thêm nút xuất Excel ở báo cáo | `không` | `task` | R10 |
| TC11 | cập nhật link https://www.tiktok.com/@shopofficial ở footer trang chủ | `không` | `task` | R10 |
| TC12 | nâng @angular/core lên 17 và sửa `@Transactional` ở PaymentService | `không` | `task` | R10 |
| TC13 | rồi giờ call ct kéo ảnh vào zoom cho ảnh review đi @Sellisloss =)) | `không` | `task` | F2 |

## Bảng: normalize_cases

Kiểm `facts.md` · `normalize` + `generic_values` (V3).

| # | Trường | Giá trị | Chung chung |
|---|---|---|---|
| G1 | `object` | `Nút này` | `có` |
| G2 | `object` | `nút Thanh toán` | `không` |
| G3 | `observed` | `Bị lỗi!` | `có` |
| G4 | `work` | `Em check giúp` | `có` |
| G5 | `work` | `check giúp em cái này` | `không` |
| G6 | `object` | `  CÁI   NÀY. ` | `có` |
| G7 | `observed` | `không in được` | `không` |
| G8 | `problem` | `Chưa ổn…` | `có` |
| G9 | `work` | `Báo giùm em!` | `có` |
| G10 | `work` | `anh cho em biết` | `có` |
| G11 | `work` | `báo giùm em nha` | `có` |
| G12 | `work` | `check giúp với nha!` | `có` |
| G13 | `work` | `cho mình biết ạ` | `có` |
| G14 | `observed` | `không đi` | `không` |
| G15 | `work` | `nha` | `không` |

G5: cả cụm không bằng cụm chung chung nào ⇒ tính là CÓ. Vì vậy model phải ghi **cụm ngắn nhất** (`facts.md`, cách ghi giá trị 1): "Check giúp em cái này" ⇒ `work`=check giúp, `object`=cái này (X7).
