# P8 và group ops — kết quả kiểm tra #2178

**Version:** v1.0.0 · **Cập nhật:** 03/11/2026 18:43 Asia/Saigon · **Owner/state:** AlexChu. Executor Codex Sol Medium; reviewer độc lập Sol High.

**Kết luận: source/model đã đạt các kiểm tra bên dưới; vận hành vẫn NO_GO.** Chưa commit/push bản sửa, cài bản mới, đổi config live, restart service, gửi Telegram hoặc tạo ticket thật trong phiên này.

## 1. Phạm vi và kết quả chính

Yêu cầu Owner: “tiếp tục xử lý lỗi provider/timeout ở P8, xác minh ID và quyền truy cập group ops để sẵn sàng test thực tế trên Telegram cho tôi.” Các đích Owner đã xác nhận: gw-01/THINKPAD; nguồn là mọi group listener tham gia, tách ops khỏi intake thường; repo test `PersonalProjectJob/telegram-agent`; ops từ đường dẫn Telegram Owner cung cấp.

| Hạng mục | Kết quả | Bằng chứng |
|---|---|---|
| Lỗi provider/timeout cũ | Không tái hiện trong lượt kiểm lại; chưa xác lập nguyên nhân lịch sử | Text subset 8/8 trong 7,937 giây; fallback health tối thiểu 2,774 giây; không sửa transport hoặc tăng timeout |
| P8 — kết quả theo corpus chuẩn | **24/24 đúng state, rule và questions** sau sửa v2 | 20 lần gọi DeepSeek thành công; ba ca được production xử lý sớm; X14 là fault fixture; reviewer High đối chiếu JSON gốc |
| Trích riêng ba ca xử lý sớm | Kết quả cuối **3/3**; diagnostic dữ kiện thô **FAIL** | X10 thêm object; X12 intent task thay unknown; X8 thiếu intent được chuẩn hóa unknown. Provenance cả ba hợp lệ; không giấu exit 1 của diagnostic |
| Full listener | **1.658 test; 1.656 PASS, 2 skip; exit 0** | Runner 734,484 giây; wrapper 735,516 giây; 152 file nguồn/test/rules/template giữ nguyên trước/sau |
| Regression và review | High **APPROVED** | Ba regression có red/green: hai fail trên baseline, contrast mới fail trên v1; executor suite 57 PASS; High kiểm độc lập |
| Rules markdown | **13/13 PASS** | 0,030 giây |
| Drift / policy | **PASS: 33 rules / 61 runtime** | Policy suite 18 PASS; Business trace v1.16.8 đồng bộ đúng bytes đã review |
| Fake tools đúng rules mode | **6 core + 3 extension PASS** | Core 1,553 giây; extension 3,435 giây. Pipeline/worker, ops Reply, reservation, ANALYZE/STATUS, append-once và reaction được kiểm bằng adapter giả |
| Group ops | **ID `-5536943529`, loại Chat; member và quyền gửi text hợp lệ** | Telegram authenticated read-only; không migrated; không phải admin; **0 tin đã gửi** |

Smoke autonomous cũ với rules tắt đạt 2/2 ca grouping. Smoke THINKING phụ chạy trên cấu hình autonomous hiện hữu có ba assertion grouping không đạt và một ca ancillary đạt; giữ nguyên kết quả FAIL này, không dùng để ký thay smoke rules bật.

## 2. Lỗi đã sửa và giới hạn bằng chứng

- X20: batch bảy nguồn, năm ảnh được chọn và hai ảnh chưa xem cần **8.188 byte** ngay cả khi bỏ hết ví dụ tùy chọn; bị từ chối trước provider. Bỏ cột ví dụ trường tùy chọn giúp request còn **7.852 byte**; giữ nguyên định nghĩa, chỉ dẫn, schema, text, source ID, Reply, forward flag và pixel.
- X4/X7/X23: bộ mẫu trích thiếu cân đối làm lệch loại yêu cầu/câu hỏi; X23 từng sai NEED_INFO → READY. Bổ sung mẫu trích chuẩn, không phân nhánh theo nội dung tin đầu vào.
- V1 còn làm X17 sai CHAT → READY. V2 thêm đối chiếu câu hỏi thông tin X17 với câu hỏi có yêu cầu làm việc X18. Lượt toàn bộ v2 đạt 24/24; lượt full suite v1 bị dừng chủ động, **không tính PASS**.

Chỉ thay `rules_extraction.py`, thêm ba regression trong file test hiện hữu và đồng bộ Business trace. Không đổi bảng rules, model, consent, transport, giới hạn **8.000 byte / 2 call / 40 giây** hoặc cấu hình provider.

Theo README rules §7, tiêu chí P8 là áp dữ kiện vào rules và khớp cột **Kết quả**; không yêu cầu mọi raw fact giống từng chữ. High xác nhận tiêu chí này đạt trên corpus giả có ghi rõ điều kiện. Các artifact giữ `full_P8=false` vì không tự ký toàn bộ preflight; báo cáo này ghi nhận đánh giá criterion riêng, không sửa output gốc.

Ảnh là fixture giả có SHA, không phải ảnh lịch sử. X14 mô phỏng model unavailable; X24 có context khác chủ đề ở chế độ chỉ đọc, không cấp quyền target. Không suy thành chất lượng ảnh thật, tài liệu/lifecycle do model thật, khả năng gửi Telegram hay release readiness. Session/auth chỉ được dùng trên gw-01 trong MemorySession cho truy vấn entity/quyền; không xuất session hoặc credential.

## 3. Đích, cấu hình và các gate còn lại

Candidate riêng trên gw-01: `/home/startechai/listener-test-2178-20261103/listener-test.candidate.toml`, quyền `0600`. Đã validate source rules/templates và target, chưa thay file `/etc/tg-triage/listener.toml`.

| Trường | Candidate |
|---|---|
| Nguồn | `listen_all_groups=true`, `chat_ids=[]`; ops đi nhánh riêng |
| Ops | `ops_chat_id=-5536943529` |
| Repo | `PersonalProjectJob/telegram-agent` |
| Rules | `decision_rules_enabled=true`; autonomous/thinking giữ bật |
| Model | DeepSeek `deepseek-flash`; fallback `qwen3.5:9b`; settings giữ nguyên |
| State | Giữ đường dẫn ledger hiện có; **không dùng DB trắng** |

Candidate ban đầu dự kiến DB riêng đã được sửa sau review: `catch_up=True` cùng all-groups có thể xử lý tin tồn mà thiếu lịch sử chống trùng. Trước cutover phải dừng listener cũ đúng quy trình, giữ backup/session/ledger, kiểm queue và thao tác chưa xác nhận, dùng preflight/transition legacy được hỗ trợ, xác định ranh giới catch-up và kiểm readback. Không sửa tay/reset DB hoặc replay tin cũ.

| Gate | Trạng thái hiện tại |
|---|---|
| P1 | Rules quyết định ACTIVE theo duyệt trước của Owner; roles/preflight vẫn DRAFT |
| P2 | PASS 13 markdown tests |
| P3 | PASS loader/validation trong staged preview; cần đối chiếu lại bản cài |
| P4 | Frozen worktree full suite PASS; **release gate UNCHECKED**, chưa bind commit mới |
| P5 | PASS checker và 18 policy tests |
| P6 | Finding source P8 đã sửa và High duyệt; Owner ký cuối còn chờ |
| P7 | PASS fake-tool smoke cho core + extension; không thay P12 |
| P8 | PASS tiêu chí kết quả corpus giả đã review; giữ các giới hạn ở §2 |
| P9 | Candidate đã đối chiếu target; **UNCHECKED trên bản cài** |
| P10 | **UNCHECKED:** chưa thử rollback; chưa xác định wheel cũ được giữ ở đâu |
| P11 | **UNCHECKED:** chưa cài/test bản cài/restart/readback |
| P12 | Quyền ops đã xác minh; **UNCHECKED gửi thực tế và source chỉ reaction** |
| P13 | **UNCHECKED:** Owner chưa ký GO |

Service live giữ `active`, PID `577623`, `NRestarts=0`; bản cài vẫn ở commit `a81355f9498466939db37b7f7fa2c7b94f06888b`. Preview đã đọc lại đúng runtime SHA v2, không phải bản cài vận hành.

## 4. Source và bằng chứng

Worktree: `Listener-ticket-p8-2178`, branch `bugfix/2178_provider-ops-preflight`, base `892d5eaa1911c9667dda8f84b192708206c5dbc3`. Bản sửa chưa commit/push.

- Runtime raw SHA256: `e5c24f790dfc04f873942b3c4b12e27d1861c9c266544b8be7b69967ea40c49d`.
- Test raw SHA256: `99dc271f74f9e7b4283259fb96b2167114dea5822ebf2e7901d29c69652b1e4a`.
- Rules fingerprint: `3942f488a1d1ba0f48d8ca05c7eafdf28da1d9ddfa5b128e3e11ac274a5016c3`.
- Template fingerprint: `8bc960370883bc8855a0e3e204f22656f957e5f0ae1998cbd8343ef184d9ef2f`.

Bằng chứng local riêng, không commit: `.context/P8-v2-24.jsonl`, `P8-v2-early3.jsonl`, `P8-full-v2-result.json`, `P8-full-v2-unittest.log`, `P7-rules-smoke.log`, `P7-extensions-smoke.log`, `P8-high-review.md` và manifest/checkpoint của từng lượt. Model artifacts gốc và candidate còn ở staging riêng trên gw-01; không đưa token, session, DB hay chat thật vào báo cáo.

Các kiểm tra source chạy bằng `python -m unittest discover -s tests -t .` trong `listener/` với `PYTHONPATH=src`; checker/policy chạy từ repo root. Model probes dùng Python venv trên gw-01, source preview đã bind raw SHA, chỉ mở SQLite memory và provider đã cấu hình.

Related Documents: [Master](../00-master/telegram-triage-noissue_overview_261027_v1.0.0.md) · [Engineering](../02-engineering/telegram-triage-noissue_listener-audit_261029_v1.0.0.md) · [Business](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md) · [Test cases cho phiên Telegram](telegram-testing-2178_qa_261103_v1.0.0.md) · [Preflight P1–P13](../../listener/rules/preflight.md).

## Changelog

| Version | Ngày giờ (Asia/Saigon) | Nội dung |
|---|---|---|
| v1.0.0 | 03/11/2026 18:43 | Ghi kết quả P8 v2/full suite/review, xác minh ops và candidate; phân biệt source PASS với installed/delivery/GO chưa hoàn tất. |
