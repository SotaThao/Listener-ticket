# Listener-ticket — Audit goal Telegram → ticket và điều kiện vận hành

**Version**: v1.0.2 · **Cập nhật**: 2026-10-30 18:13 Asia/Saigon · **Owner**: AlexChu · **Người cập nhật**: Codex
**Kết luận**: Lõi đã triển khai và kiểm thử offline; sáu rules ACTIVE, tám mẫu tin được Owner duyệt. Chưa đủ bằng chứng chạy thật: **NO_GO**. Không bật/restart bot trong phiên.

> Bản này cập nhật audit v1.0.0 về bộ cài. Goal ưu tiên hiện tại là **Agent tự lắng nghe thông tin trong group Telegram và log ticket đúng nghiệp vụ**. Installer cho người không biết code là nhánh sau; không dùng điểm DX của bản cũ để kết luận chất lượng lõi hôm nay. Giữ tên file để các liên kết hiện hữu tiếp tục hoạt động.

## 1. Phạm vi, nguồn chính và bằng chứng

- Owner chốt **Listener-ticket phát triển độc lập**. `PersonalProjectJob/telegram-agent` / AgentBuilder chỉ còn là nguồn lịch sử, không phải nguồn chính để sửa song song.
- Audit ban đầu được cập nhật trên branch tài liệu `docs/noissue_devex-audit-installer`; bản này được tích hợp cùng mã lên `main` theo yêu cầu Owner. Snapshot đã kiểm được commit tại `095ca3f` trên branch `codex/noissue_listener-core-rules`, base `origin/main` `b2d051b`; Owner đã yêu cầu tích hợp mã và audit lên `main`. Worktree mã ban đầu là `Listener-ticket-core-rules`; kiểm lại HEAD của main để xác định bản publish. Chưa tạo release hoặc triển khai service.
- Owner đã duyệt: “Duyệt 8 mẫu tin và chuyển 6 rules sang ACTIVE, vẫn giữ bot tắt. Tiếp tục kiểm tra bước tiếp theo như đề xuất.” Sáu file quyết định hiện header version 3 / ACTIVE; `roles.md`, `preflight.md` vẫn DRAFT.
- Cấu hình ví dụ vẫn `mode="dry-run"`, `autonomous_enabled=false`, `decision_rules_enabled=false`. ACTIVE cho phép nạp rules khi opt-in; không tự bật tính năng hoặc service.
- Owner hoãn cấu hình model/P8: “sẽ hỏi khi setup máy”. Không yêu cầu lại model trước thời điểm setup.

Bằng chứng dưới đây ở `.context/` **của worktree mã**, là artifact local được gitignore, chưa phải bằng chứng release công khai:

| Kiểm tra đã chạy | Kết quả thật | Artifact |
|---|---|---|
| Full listener suite sau ACTIVE | `Ran 1393 tests in 169.375s; OK (skipped=2)` | `activation-full-tests.log` |
| Rules / engine / bootstrap | 31 tests OK; riêng markdown 13 tests OK; K7 đạt | `rules-progress.txt`, `activation-preflight.md` |
| Policy checker | PASS, 33 quy tắc / 56 file runtime-prompt-template | `activation-policy.log` |
| Policy regression | 18 tests OK, 7.499s | `activation-policy-tests.log` |
| Wheel trong venv tạm + CLI `check-config` nguồn/bản cài | PASS, RECORD đúng, import từ venv, hai fingerprint khớp | `activation-wheel-check.json`, `.log` |
| Independent review | Read-only Codex Terra/high APPROVED sau sửa findings; reviewer đọc source được cung cấp, không tự chạy test/hash | `codex-review-final-r10.jsonl`, `codex-review-activation-r2.jsonl` |
| Controller đối chiếu file | 34/34 hash khớp sau review; 22 file code/test/template/package không đổi ở lát ACTIVE | `activation-freeze-r3-hashes.json`, `activation-scope-proof.json` |
| Diff whitespace | `git diff --check` exit 0 | `activation-diff-check.log` |

Rules fingerprint: `d3f2f2c36255ef35728794f749980fd5c3e0845fe79050a9a85b1b4c553c81e6`.
Template fingerprint: `8bc960370883bc8855a0e3e204f22656f957e5f0ae1998cbd8343ef184d9ef2f`.

Kiểm wheel chỉ dùng config/credential tổng hợp; không chạy `run`, không gọi model/GitHub/Telegram, không tạo DB/session. **Offline PASS không chứng minh model trích đúng, config máy chạy đúng hoặc bot đã chạy thật.** Hai test skipped không được tính là PASS riêng lẻ.

## 2. Phần đã đáp ứng goal ở mức code/offline

| Nhu cầu | Đã có | Giới hạn bằng chứng |
|---|---|---|
| Tự nghe group, không cần tag để phát hiện công việc | Nền Telethon, lọc nhóm được cấu hình, lưu SQLite, gom batch và chống trùng | Chưa kiểm membership/kết nối máy triển khai |
| Quyết định tạo ticket nhất quán | Model chỉ trích nhóm/dữ kiện; code áp markdown và sáu trạng thái READY/APPEND/NEED_INFO/MEDIA_ONLY/CHAT/HOLD; confidence không quyết định | P8 model thật chưa kiểm; không có cam kết chính xác 100% từ unit test |
| Tạo/bổ sung ticket đúng nguồn | Renderer/reservation hiện hữu; APPEND cần Reply thật và bằng chứng target open cùng nguồn/repo tin cậy; source có định danh chat + message | Chưa kiểm token/quyền repo thật; ticket đóng còn HOLD |
| Hỏi bổ sung đúng kênh | Group nguồn chỉ reaction trong rules mode; mọi tin chữ sang ops; Reply vào câu hỏi đã gửi được nối đúng pending | Tám mẫu đã duyệt; P12 trên group thật còn thiếu |
| Giữ tin khi chưa đủ rõ | Pending bền vững, hỏi từng trường một lần, fallback một lần, retry có giới hạn, expiry bảy ngày | Một số tổ hợp lịch sử/budget thiếu bằng chứng giữ HOLD/manual |
| Ảnh và tài liệu có provenance | Định danh kép chat/message, giữ ảnh của pending, đọc text ngắn có giới hạn; tài liệu chưa đọc không cấp dữ kiện giả | PDF/Office chưa đọc có thể đính kèm nếu caption tự đủ; không hứa hiểu nội dung chưa đọc |
| Tránh ghi lặp khi crash | Checkpoint nhóm, ledger issue/comment/notice/upload, phân biệt claim nội bộ với thao tác ghi thật; unknown write dừng H4, không tự replay | H4 cần Owner kiểm tra repo; không tự sửa/reset DB để chạy tiếp |
| Một tiến trình mỗi DB trên Windows | `ProcessLock` dùng `msvcrt`, test contention/release/kill với process thật | Chưa thử cùng service và quy trình restart trên máy đích |

Nguồn mã để kiểm lại: `decision_rules.py`, `rules_extraction.py`, `rules_intake.py`, `rules_pending_store.py`, `runtime.py`, tích hợp `pipeline.py` / `telegram_io.py` trong worktree mã. Chế độ cũ khi flag OFF vẫn giữ; ANALYZE/STATUS không được coi là khả năng của đường intake rules mới.

## 3. Phần dư so với goal hiện tại

“Dư” ở đây nghĩa là chưa cần để chứng minh nghe group → log ticket an toàn; chưa có yêu cầu xóa các khả năng này.

| Nhánh | Cách xử lý phạm vi |
|---|---|
| `install.exe`, PyInstaller/Nuitka, Inno Setup, ký số | Để backlog installer riêng; không nằm trong gói 20h ở §7 |
| Wizard sáu bước, Telegram OTP GUI, GitHub device flow, DPAPI | Hữu ích cho người cài không biết code; chưa cần cho operator cấu hình bản Python |
| Mục tiêu cài ≤10 phút, SETUP có ảnh, UX lỗi/thống kê thời gian cài | Chưa đo lại; điểm DX 3/10 và TTHW >60 phút của v1.0.0 là nhận định lịch sử cho persona installer, không phải số đo runtime mới |
| Windows Service/Task Scheduler hoàn chỉnh | Không cần để chứng minh logic offline; cơ chế chạy nền/restart thực tế của máy đã chọn vẫn phải kiểm trước GO |
| Phân tích/status rộng, tối ưu chi phí, đa provider, routing nâng cao | Giữ ngoài gói kiểm chứng lõi; không mở rộng rules intake để đạt parity mọi tính năng cũ |
| Viết lại C++/Go hoặc đồng bộ sửa hai repo nguồn | Không làm: Python hiện hữu và một nguồn chính Listener-ticket đã chốt |

Không xếp policy, provenance, lock, reservation, rollback hoặc preflight vào “dư”: đây là điều kiện bảo vệ ticket và dữ liệu.

## 4. Bug, giới hạn và điều chỉnh audit cũ

### 4.1 Bug đã sửa và kiểm lại

| Nhóm lỗi đã phát hiện | Trạng thái hiện tại |
|---|---|
| ProcessLock không khóa trên Windows | Đã sửa, có kiểm tranh chấp process thật. Không còn giữ B4 như bug mở |
| Validation null/falsy, tính bất biến snapshot | Đã sửa và reviewer kiểm lại |
| Batch sai làm sửa một phần pending; mất câu trả lời đến đồng thời | Đã sửa guard/claim; regression fake xanh |
| Ảnh pending lịch sử, message ID trùng giữa source/ops, reaction sai đích | Đã sửa định danh kép và lưu provenance; regression xanh |
| Tài liệu chưa đọc bị dùng để hợp thức hóa dữ kiện model | Đã sửa grounding; không dùng boolean có tài liệu để tin mọi nội dung |
| H4 kiểm quá muộn, worker/sweep tiếp tục sau ghi không rõ kết quả | Đã sửa tại ranh giới tác vụ và trước Store recovery; giữ DB/queue |
| Recovery CHAT đã đóng hoặc APPEND có nhiều Reply cùng target bị chặn sai | Đã sửa, kiểm crash không repost |
| Nhầm timeout `gh release view` là ghi ambiguous | Đã tách read failure trước write; uncertainty của create/upload vẫn dừng |
| Tài liệu còn ghi mẫu tin chưa duyệt | Đã sửa sau Owner phê duyệt; activation review APPROVED |

### 4.2 Bug còn mở được xác nhận

**Chưa có defect code tái hiện còn mở trong snapshot và phạm vi reviewer vừa kiểm.** Đây không phải kết luận “không còn bug”: chưa có kiểm model cấu hình thật, bản release hay group thật. P6 vẫn UNCHECKED vì chưa có xác nhận Owner về danh sách HIGH.

| Mục còn thiếu | Phân loại đúng | Ảnh hưởng / cách giữ an toàn |
|---|---|---|
| Reply vào ticket đã đóng | Khoảng trống lifecycle của rules mode | HOLD `closed_target_not_supported`; không tự reopen hoặc tạo thay thế. Không nằm trong 20h hoàn thiện parity |
| Thiếu budget/proof, nhiều pending lịch sử hoặc tổ hợp document lịch sử không được hỗ trợ | Giới hạn xử lý bảo thủ | HOLD/manual; không suy diễn để ép READY |
| Câu trả lời ops đến khi một ghi đang in-flight | Giới hạn concurrency có chủ đích | Lưu bền vững, giữ cho Owner xem; không âm thầm bỏ tin |
| Model/provider, quyền GitHub, membership Telegram, service thật | Rủi ro chưa kiểm | Giữ UNCHECKED/NO_GO tới khi có output máy |
| `harness-verify` chỉ nhận project Node | Blocker công cụ verify, không phải bug listener | Exit 2 vì thiếu package.json; giữ Python checks riêng, không tạo package.json giả |

### 4.3 Đính chính các finding installer v1.0.0

- **B4**: `model_lock.py` đã có nhánh Windows từ trước. Bug thực tế là `runtime.ProcessLock`; đã sửa ở worktree mới. Không yêu cầu sửa lại ModelLock khi không có repro.
- **Lỗi HIGH giới hạn ảnh** ghi trong preflight cũ là nhận định lịch sử; test hồi quy hiện xanh. Không ghi lại thành bug mở chỉ từ tài liệu ngày 29/10.
- **B5**: fallback Ollama là cấu hình mặc định, không phải bắt buộc duy nhất; config hỗ trợ `fallback="none"`. Khi setup phải đối chiếu provider/fallback thật và consent.
- **B7**: tìm data-files đã được kiểm qua wheel Python trong venv tạm. Chưa kiểm executable frozen; PASS wheel không đóng finding đóng gói `.exe`.
- **D-8**: đã chốt Listener-ticket độc lập. **D-9**: đề xuất wizard chỉ có group nguồn đã lỗi thời cho rules mode: ops group riêng là điều kiện config bắt buộc khi opt-in. Các quyết định app Telegram/OAuth, installer, code-signing và ngoại lệ ticket setup thật chưa được duyệt chỉ vì rules ACTIVE.

## 5. Phiếu preflight hiện tại và việc chờ setup máy

| Mục | Trạng thái | Còn cần gì |
|---|---|---|
| P1 sáu rules ACTIVE | PASS | Owner duyệt, version3; không bao gồm roles/preflight |
| P2 markdown | PASS | 13 tests OK, có K7 |
| P3 nạp rules/template + fingerprint | PASS offline | Source/venv wheel tạm khớp; đối chiếu lại bản cài đích |
| P4 toàn bộ test đúng commit release | UNCHECKED | Hiện chỉ có worktree 1.393 tests OK; cần SHA/gói release bất biến và test đúng bản đó |
| P5 drift/policy | PASS | 33 rules/56 files, 18 tests |
| P6 không HIGH mở | UNCHECKED | Owner xem danh sách rủi ro/finding và ký; không tự suy từ review APPROVED |
| P7 smoke dùng fake external tools | UNCHECKED | Chạy script smoke quy định với đúng cấu hình/bản release; unit fake không thay P7 |
| P8 model trích đúng toàn bộ ví dụ | UNCHECKED, Owner hoãn | Hỏi model/provider/config khi setup; chạy extraction thật và so 100% ví dụ; fail giữ NO_GO |
| P9 config máy chạy | UNCHECKED | Repo, source chat, ops chat riêng, mode, autonomous/rules flags, media limit, consent, fallback |
| P10 rollback | UNCHECKED | Giữ wheel trước, thử cài lại và giữ nguyên DB/config; ghi output |
| P11 bản cài/service | UNCHECKED | Test bản cài, PID/service readback sau restart, fingerprint khớp P3; chỉ chạy khi được cấp quyền |
| P12 group xử lý | UNCHECKED | Bot membership, tin chữ chỉ ops, reaction nguồn; kiểm nguồn/Reply/link đúng; chỉ gửi khi được Owner cho phép |
| P13 GO | UNCHECKED | Owner ký sau mọi điều kiện trước; không tự bật |

Thứ tự kiểm theo `rules/preflight.md`; không vượt một mục FAIL bằng cách tick PASS mục sau. Việc độc lập đã kiểm offline được ghi riêng, không thay phiếu release. Owner phải chốt bảng quyền/phiếu trước vận hành; agent không tự nhận OPERATOR. Không replay tin thật, không reset reservation, không tạo ticket thử thật theo đề xuất installer khi ngoại lệ đó chưa được duyệt.

## 6. Hướng triển khai tiếp và ranh giới 20 giờ

Gói tiếp theo là **kiểm thử và hoàn thiện khả năng vận hành lõi còn lại**, không tính lại implementation rules/lock đã hoàn tất và không bao gồm installer. Bắt đầu các bước phụ thuộc máy khi Owner cung cấp máy/cấu hình và quyền vận hành. Thời gian chờ credentials, quyền, Owner hoặc mua dịch vụ không phải giờ thực hiện.

20h là **tổng estimate**, không phải timesheet, cam kết đủ cho mọi bug chưa biết hay bảo đảm GO. Nếu xuất hiện lỗi mới vượt khả năng sửa trong lát đã dự toán, ghi issue/repro và giữ NO_GO; không đổi số test, nén phạm vi hoặc tick khống để đủ giờ.

## 7. Task Dev đề xuất — tổng đúng 20h

Nơi tracking do Owner chỉ định: `vlink-group/vlink-nexora`, Backlog **#2178**, assignee **SotaThao**. Board chỉ dùng quản lý công việc; source code vẫn là Listener-ticket độc lập. Không sửa body Backlog lịch sử, không chuyển nguồn code sang Nexora.

**Trạng thái ticket: đã tạo Task [#2488](https://github.com/vlink-group/vlink-nexora/issues/2488), giữ Todo theo chỉ định mới của Owner: “tạo ticket và giữ ticket todo cho phần setup máy”.** Năm Dev Task #2489–#2493 có estimate 4h mỗi việc. Các bước setup chưa thực hiện; tạo ticket không cấp quyền bật bot hoặc vận hành thật.

| Lát công việc còn lại | Estimate | Kết quả phải có / phụ thuộc |
|---|---:|---|
| Chuẩn bị bản release, đối chiếu config và diễn tập rollback | 4h | SHA/gói được Owner cho phép tạo, config đã che bí mật, rollback có output; P4/P9/P10 |
| Kiểm model cấu hình thật với ví dụ và smoke | 4h | Báo cáo từng ví dụ và kết quả smoke, sai ghi rõ; P7/P8; chờ setup máy |
| Kiểm bản cài, service, khóa một bản và phục hồi | 4h | Test đúng bản cài, readback PID/fingerprint, contention/restart có bằng chứng; P11; cần OPERATOR được chỉ định |
| Kiểm luồng Telegram → GitHub và hai group | 4h | Tin mới/bộ dữ liệu kiểm được duyệt, quyền issue/asset, Reply/source/reaction và ops đúng; P12; không tự gửi live |
| Triage lỗi phát sinh, hồi quy có giới hạn và bàn giao preflight | 4h | Repro/fix trong phạm vi, regression, danh sách còn mở và phiếu GO/NO_GO để Owner quyết; P6/P13 |
| **Tổng estimate Dev Task** | **20h** | **5 × 4h; không cộng Effort lần nữa ở Task/Backlog** |

Tất cả là việc **chưa hoàn tất**, status Todo trên board; năm Dev Task nằm dưới Task #2488, Task nằm dưới Backlog #2178. Task cha dùng AC2, AC4, AC6 của Backlog #2178; không thêm AC hoặc sửa kết quả lịch sử khi chưa có yêu cầu. Parent/Backlog không set field Effort; kiểm tổng qua năm Dev Task trên board. Week hiện tại chỉ là nhãn tracking, không phải hứa setup xong trong tuần; không đặt due date hôm nay cho công việc còn chờ setup.

## 8. Related Documents

- [Master overview](../00-master/telegram-triage-noissue_overview_261027_v1.0.0.md)
- [Business DOCS](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md)
- [Luồng nghiệp vụ](../01-product/telegram-triage-noissue_business-flow_261028_v1.0.0.md)
- [Rules spec](../04-tasks/telegram-triage-noissue_rules-spec_261029_v1.0.0.md)
- [QA](../03-qa/telegram-triage-noissue_qa_261015_v1.0.0.md)
- [Rules loader / approval](../../listener/rules/README.md), [Preflight](../../listener/rules/preflight.md), [Agent guide](../../listener/AGENTS.md)
- [Backlog tracking do Owner chỉ định](https://github.com/vlink-group/vlink-nexora/issues/2178)

Các link tương đối trên branch tài liệu có thể còn nội dung cũ; trạng thái mới phải đối chiếu worktree mã và manifest nêu §1. Không dùng tài liệu cũ để phủ nhận bằng chứng snapshot hoặc suy thêm quyền vận hành.

## 9. Changelog

| Version | Ngày / giờ Asia/Saigon | Người cập nhật | Nội dung |
|---|---|---|---|
| v1.0.2 | 2026-10-30 18:20 | Codex | Owner yêu cầu commit/push main; ghi commit mã 095ca3f, tạo Task setup #2488 cùng năm Dev Task Todo, giữ 20h estimate và NO_GO. |
| v1.0.1 | 2026-10-30 18:13 | Codex | Đổi trọng tâm về goal nghe group → ticket; phân loại đáp ứng/dư/bug/giới hạn/setup; sửa B4/B5/B7, D8/D9; ghi ACTIVE/approval, bằng chứng 1.393 tests, NO_GO và estimate 20h. |
| v1.0.0 | 2026-10-30, giờ chưa ghi | Claude | Audit DX bộ cài trước implementation, base b2d051b. Tám trục và điểm tổng 3/10 là lịch sử, chưa đo lại bằng installer thật. |
