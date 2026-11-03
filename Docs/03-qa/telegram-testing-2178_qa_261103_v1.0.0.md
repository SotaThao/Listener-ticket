# Telegram preflight #2178 — QA / preflight

Version v1.0.0 · 2026-11-03 17:38 Asia/Saigon · Owner identity AlexChu · Executor Codex Sol Medium / independent High review. Kết luận hiện tại **NO_GO**; staged preview chưa phải bản cài vận hành.

## Test Strategy / scope

Owner duyệt ngày 2026-11-03: “Duyệt lifecycle.md và analysis-status.md sang ACTIVE, tiếp tục chuẩn bị môi trường và kiểm tra preflight để test thực tế trên Telegram cho tôi.” Target Owner chọn: gw-01/THINKPAD. Chỉ activation hai extension + related docs/test assumptions và chuẩn bị preflight; không suy thành GO/restart/gửi tin/ghi GitHub.

Source feature/2178_telegram-test-preflight, base ed13f33; worktree Listener-ticket-telegram-test-2178. High APPROVED7/7 raw files;34 extension table lines+117 Business table lines nguyên;8other rules+48runtime nguyên. Independent42tests/19.662s0skip;source270/211.915s0skip;policy18/7.371s PASS. Root full ACTIVE exit0: Ran1655/510.229s =1653pass/2skip Windows symlink/POSIX;196 tracked files unchanged. Evidence .context/activation-full-result.json và source hashes trong activation worktree.

Host gw01 authenticated read-only: service hiện có ACTIVE pid577623 NRestarts0, installed a81355f9498466939db37b7f7fa2c7b94f06888b. Config đang live/all_groups=true/chat_ids[]/opsnull/decision_rulesunset; đây không phải test config đã duyệt. DeepSeek deepseek-flash consent text/image true; fallback Ollama qwen3.5:9b localhost reachable. TG/GH/model credentials PRESENT, không ghi giá trị. Repo hiện tại PersonalProjectJob/telegram-agent chỉ là env hiện hữu, **chưa phải repo test Owner duyệt**. Không mở nội dung session/DB, không start/stop/restart service hoặc ghi config.

Staged preview /home/startechai/listener-test-2178-20261103/source: archiveSHA256204692352be9a53c619c737f76b7a56993ece495316b877b4b8d427b3e3b4476. Actual staged loader/templates khớp; rules fingerprint3942f488a1d1ba0f48d8ca05c7eafdf28da1d9ddfa5b128e3e11ac274a5016c3, template8bc960370883bc8855a0e3e204f22656f957e5f0ae1998cbd8343ef184d9ef2f. Chưa có check-config của test config. Guarded configured DeepSeek deepseek-flash normal2/40 text subset8cases exit1:2/8 PASS(X12,X18);6 FAIL(X1,X5,X6,X9,X10,X11) actual null EXTRACTION_UNAVAILABLE. Primary provider_failed/timeout và Ollama fallback timeout; không kết luận semantic answer mismatch. Bounded X1 diagnostic0/1 exit1: HTTP200 rồi http_response_error RemoteDisconnected; nguyên nhân chưa xác lập, không suy credentials invalid/code bug/network certainty. Safe artifact main .context/telegram-model-text-results.jsonl đã lưu cục bộ. Full canonical24 còn image/Reply chưa chạy; document/lifecycle quality riêng chưa kiểm. Dừng inference thêm và dừng liveP9–P13 vì P8FAIL; không đổi model/budget/config. Không cho probe gọi GH/TG/liveDB/session.

## P1–P13 theo canonical preflight.md

| Gate | Trạng thái hiện tại | Bằng chứng / còn thiếu |
|---|---|---|
| P1 rules ACTIVE | PASS source | Sáu decision rules ACTIVE; hai extension ACTIVE v2 theo Owner; roles/preflight vẫn DRAFT |
| P2 test_rules_markdown | PASS source | Source checks do root/executor báo đạt; Full ACTIVE1655tests/510.229s và independent42tests/19.662s PASS |
| P3 load+fingerprints/check-config | UNCHECKED đầy đủ | Host staged loader PASS; thiếu actual approved test-config check-config exit0+full fingerprints |
| P4 full listener exact release commit | PASS source; release binding pending | 1655tests/510.229s exit0 =1653pass2skip;196tracked files unchanged; source fingerprint ghi trên. Chưa release commit |
| P5 drift checker | PASS source | Checker/source policy và18tests PASS7.371s; không thay host install proof |
| P6 open HIGH | High APPROVED source | Review activation không còn finding; Owner ký P6 chưa có |
| P7 fake-tools smoke | UNCHECKED đầy đủ | Focused fake tests không tự thay exact canonical smoke scripts |
| P8 configured model100%examples | FAIL | Guarded configured text subset2/8PASS,6 extraction-unavailable FAIL(exit1);canonical24image/Reply chưa đủ,không đạt100%;document/lifecycle quality riêng chưa kiểm |
| P9 approved machine config | FAIL scope | Existing allgroups/noops/decisionrulesunset; thiếu sourcegroup/opsgroup/repo Owner chọn |
| P10 tried rollback | UNCHECKED | Chưa thử cài lại bản trước |
| P11 installed tests/service restart | UNCHECKED | Chỉ biết existing service; staged preview chưa install/restart |
| P12 ops delivery/source reaction only | UNCHECKED | Chưa có ops target, chưa gửi test |
| P13 Owner GO | UNCHECKED | Chỉ duyệt activation/preparation, chưa ký GO |

P8 FAIL ⇒ dừng trước liveP9–P13; P9 config-scope FAIL và các mục UNCHECKED không cho live GO. RUNNING là nhãn tiến trình, chưa phải PASS canonical. Không tự miễn gate hoặc coi kiểm một phần là toàn bộ.

## Test Cases — Functional (planned; tất cả NOT_RUN)

| Case | Priority/type | Preconditions | Steps | Expected | Result |
|---|---|---|---|---|---|
| TC01 ready text | P0/happy | Approved test source/ops/repo+GO+isolated state | Gửi một nội dung READY mới vào source; quan sát repo và ops | Một ticket đúng repo; source chỉ reaction; ops có notice đúng nguồn | NOT_RUN |
| TC02 clarification | P0/happy | TC01 scope ready; NEED_INFO fixture | Gửi thiếu một trường; Reply thật vào câu hỏi ops | Hỏi một lần/trường; Reply bind đúng pending; đủ dữ kiện mới ghi | NOT_RUN |
| TC03 image evidence | P1/happy | Synthetic safe image+image consent | Gửi ảnh trong group đã duyệt; quan sát evidence và notice | Nguồn/ảnh đúng ticket; không leak nội dung group khác | NOT_RUN |
| TC04 ANALYZE text/doc | P1/happy | Synthetic short doc+known source ids | Gửi yêu cầu ANALYZE kèm text/doc; xem kết quả | Kết luận dựa đúng evidence, không tự ghi GitHub ngoài action | NOT_RUN |
| TC05 STATUS | P1/happy | Ticket test known repo+group binding | Hỏi STATUS và Reply đúng notice | Đọc đúng ticket/nhóm; không tạo/sửa ticket | NOT_RUN |

## Edge / recovery (planned; tất cả NOT_RUN)

| Case | Priority/type | Preconditions | Steps | Expected | Result |
|---|---|---|---|---|---|
| TC06 restart/unknown write | P0/error | Offline snapshot/fake tools only; no live replay | Mô phỏng crash sau reservation/unknown send, reopen | HOLD/manual; không replay GH/TG; giữ ledger để đối soát | NOT_RUN |
| TC07 foreign/changed proof | P0/edge | Fake source/ops/member/repo fixtures | Reply foreign group hoặc đổi source/question/repo sau proof | Từ chối/HOLD; không ghi/không gửi sai kênh | NOT_RUN |

## Acceptance Criteria / data / security

- [ ] Owner cung cấp ba target fields sourcegroup/opsgroup/repo; repo hiện hữu không tự thành approved target.
- [ ] P1–P13 có bằng chứng và chữ ký đúng vai; chỉ waiver ở gate canonical cho phép; Owner GO cuối.
- [ ] Tất cả P0/P1 live cases có actual result, timestamp và ticket/notice binding; không dùng seed/injected state thay luồng thật.
- [ ] Config print chỉ presence/độ dài secrets; không lưu credential/password/session hoặc dữ liệu chat thật trong báo cáo.
- [ ] Kiểm group isolation, minimum repo scope, source reaction-only, ops notice, ambiguous/no-replay và synthetic attachments.

Test data: synthetic text/image/doc và expected rules example ids; không dùng PII thật. Test case ghi live chỉ chạy sau targets+authorization, recovery giữ fake/offline. Performance N/A hiện tại, chưa có SLO/throughput được duyệt; duration test không phải latency production. Browser/responsive/a11y N/A vì không đổi UI web; vẫn kiểm khả năng đọc notice Telegram.

## Environment setup / evidence

Offline source commands hiện hữu: trong listener/ với PYTHONPATH=src chạy `python -m unittest tests.test_rules_markdown`; full `python -m unittest discover -s tests -t .`; từ repo root chạy `python scripts/check-telegram-triage-policy.py` và `python scripts/test_telegram_triage_policy.py`. Exact machine test-config check-config command chờ root cung cấp actual staged invocation, không đoán CLI flags. Không start Telegram listener bằng config live/allgroups hiện hữu. Model-only guarded harness có thể đọc TOML provider settings với validate_only, không TG/GH transport; không đồng nghĩa listener smoke test.

Related Documents (đường dẫn tương đối khi root copy vào Docs/03-qa):

- [Master](../00-master/telegram-triage-noissue_overview_261027_v1.0.0.md)
- [Engineering](../02-engineering/telegram-triage-noissue_listener-audit_261029_v1.0.0.md)
- [Business](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md)
- [Canonical preflight](../../listener/rules/preflight.md)

## Changelog

| Version | Date/time | Change |
|---|---|---|
| v1.0.0 | 2026-11-03 17:28 | QA preflight from actual source/host evidence; full source PASS1655, configured model subsetFAIL2/8,no GO; future5functional+2edge all NOT_RUN; NO_GO |
