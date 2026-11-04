# Listener-ticket — Báo cáo tiến độ và kế hoạch triển khai

**Ngày báo cáo:** 03/11/2026, cập nhật 04/11/2026 (Asia/Saigon)

**Phiên bản:** v1.0.11

**Owner:** AlexChu

**Trạng thái:** **Đang vận hành thật theo rules mode từ 04/11/2026** — Owner ký GO (preflight P13) cho release `7ba210b`. Mục 1.1 tóm tắt đợt 04/11; các mục từ 2 trở đi là báo cáo đợt 03/11, giữ làm lịch sử.

**Phạm vi đợt này:** Hoàn thiện ba luồng chức năng để chạy và kiểm thử từ source trước khi đóng gói: schema/lifecycle, ANALYZE/STATUS và SQLite/legacy.

## 1. Trạng thái tổng thể

**Cả ba task đã qua review High trong phạm vi source đã kiểm; full regression cuối đạt.** Bản mới nằm trên worktree nhánh `bugfix/2178_rules-functional-completion`, tiếp tục từ baseline `e2833ec`; code ba task đã commit và push lên `main` tại `c749d31` theo yêu cầu Owner. Tại thời điểm 03/11 bot chưa chạy thật (NO_GO); xem mục 1.1 cho trạng thái hiện tại.

### 1.1. Cập nhật 04/11/2026 — từ kiểm offline tới vận hành thật

| Bước | Kết quả |
|---|---|
| Preflight trên máy chạy (P9–P11) | Kiểm config, thử quay lại bản cũ, cài bản mới và đọc lại log sau restart: đạt |
| Bật rules mode lần đầu | Tạo ticket đúng, nhưng phát hiện bot trả lời phân tích (ANALYZE) bằng chữ trong group nguồn — vi phạm quy tắc "group nguồn chỉ có reaction" (điều kiện dừng H7). Bot được dừng ngay |
| Sửa lỗi | Mọi tin chữ ở rules mode chuyển vào group ops (ANALYZE, STATUS, câu hỏi lifecycle, thông báo tái diễn); trả lời bằng Reply trong ops; thêm chốt chặn trong code từ chối gửi chữ ra ngoài group ops. Owner duyệt 4 quyết định và điều chỉnh rule L1 |
| Kiểm chứng | 1.670 test đạt (2 skip); reviewer độc lập chỉ đọc: vòng 1 yêu cầu sửa 3 lỗi (2 HIGH, 1 MED), vòng 2 **APPROVED** |
| Vận hành | Group nguồn và group ops được nâng cấp lên supergroup; dùng sổ theo dõi mới vì dữ liệu cũ là dữ liệu test (theo quyết định Owner) |
| Kiểm thật P12 | READY tạo ticket và báo link vào ops; NEED_INFO hỏi trong ops; ANALYZE và STATUS trả lời trong ops; group nguồn chỉ nhận reaction; không ticket trùng |
| GO | Owner ký "Owner: GO · 2026-11-04" |

Việc còn lại ngoài đợt này: hướng dẫn cài từ đầu cho máy mới (chưa có); mỗi lần cài bản mới hoặc đổi máy cần lập phiếu preflight mới. Không chạy hai bot cùng lúc trên cùng các group vì sẽ tạo ticket trùng.

## 2. Tiến độ thực tế khi áp dụng Agent

| Công việc | Agent thực hiện | Kết quả thực tế |
|---|---|---|
| Xây lõi, rules và phần cleanup | Codex + Antigravity/Gemini qua các lượt trước | Có code; lifecycle, ANALYZE và SQLite mới hoàn thành một phần |
| Kiểm lại báo cáo và mã Gemini | Codex | Xác nhận test offline đạt; phát hiện schema lệch và các khoảng trống nghiệp vụ |
| Kiểm thử, review và publish | Codex + phiên reviewer độc lập | Đã push main; có wheel build và tài liệu PM. Review chỉ chấp thuận phạm vi publish |
| Schema/lifecycle | Codex Medium + reviewer High độc lập | Source APPROVED R5; nhiều nguồn/ảnh/tài liệu, recovery và guard trước POST; elapsed khoảng 114 phút |
| ANALYZE/STATUS | Codex Medium + reviewer High độc lập | Source APPROVED R2; critic/receipt, budget và ledger; elapsed khoảng 74 phút |
| SQLite/legacy | Codex Medium + reviewer High độc lập | Source APPROVED R3; snapshot, inventory, archive và recovery/identity guards; khoảng 121 phút tới kiểm tổng cuối |

Ba task đã qua source review; chưa có PM ký Accepted. Elapsed là thời gian thực từ bắt đầu đến verdict, gồm kiểm/review/chờ công cụ, không phải tổng giờ công các Agent chạy song song hoặc tỷ lệ hoàn thành toàn dự án.

## 3. Bằng chứng đã có

- **Bản source cuối R3:** 1.650 test chạy, **1.648 pass, 2 skip**, exit 0; 537,600 giây. Hai skip do quyền tạo symlink Windows và kiểm mode POSIX. Cả 130 file được theo dõi không đổi khi chạy.
- **Review độc lập:** PM-01 R5, PM-02 R2 và PM-03 R3 source APPROVED; không còn finding cụ thể. PM-03 High kiểm riêng 13 ca và 3 probe; số kiểm các vòng có trùng, không cộng thành tổng mới.
- **Trace policy cuối:** PASS **33 rules / 61 runtime files**; 18 test policy và 8 test reformat đạt. Chỉ cập nhật metadata trace sau source review; mọi file Python giữ nguyên.
- Kiểm từ source dùng SQLite tạm và fake transport; chưa xác minh chất lượng model thật hoặc luồng Telegram → model → GitHub thật.

## 4. Ba nhóm việc và thời gian với Agent

| Việc | Output PM cần nhận | Dự báo ban đầu | Thực tế / trạng thái |
|---|---|---:|---|
| Schema và lifecycle | Contract thống nhất; ticket đóng/tái diễn đúng, không tạo trùng | 1–2h | ~114 phút; High source APPROVED |
| ANALYZE / STATUS | Phân tích được text/ảnh/tài liệu theo nghiệp vụ; tra đúng ticket | 1,5–3h | ~74 phút; High source APPROVED |
| SQLite và legacy | Ma trận chuyển tiếp dữ liệu, kết quả thử trên bản sao và điều kiện giữ/gỡ legacy | 1,5–3h | ~121 phút; High source APPROVED R3, giữ legacy cho lịch sử chưa hỗ trợ |
| **Tổng đợt này** | **Ba luồng chạy từ source, có kết quả kiểm chức năng** | **4–8h** | **~5 giờ 14 phút elapsed, 11:36–16:50** |

### Định nghĩa ba nhóm việc

**1. Schema và lifecycle — Chuẩn dữ liệu và vòng đời ticket**

- **Schema:** khuôn dữ liệu trao đổi giữa model và bot: có những trường nào, kiểu dữ liệu, giá trị được phép và nguồn chứng cứ. Ví dụ: model trả một loại bằng chứng mà bot chưa khai báo thì kết quả có thể bị từ chối.
- **Lifecycle:** quy tắc xử lý ticket theo tình trạng và diễn biến sự cố: bổ sung ticket đang mở/đã đóng, hỏi làm rõ hoặc tạo lần tái diễn có liên kết. Không tự mở lại ticket hoặc tạo trùng.
- **Đã triển khai/kiểm source:** thống nhất contract, host xác định success đã dùng; xử lý nhiều nguồn/media, hỏi làm rõ, episode kế tiếp và chống ghi lặp.

**2. ANALYZE / STATUS — Phân tích yêu cầu và tra trạng thái xử lý**

- **ANALYZE:** đọc hội thoại, ảnh hoặc tài liệu để đề xuất ngắn gọn **việc cần làm, phạm vi thực hiện và tiêu chí hoàn tất**; riêng thao tác phân tích không tự tạo/sửa ticket.
- **STATUS:** trả lời bot đã ghi nhận/xử lý yêu cầu tới đâu dựa trên tin nguồn và sổ theo dõi của bot: đã tạo ticket, đang xử lý hoặc chưa xác nhận được kết quả. Không suy ra trạng thái hoàn thành công việc của team chỉ từ sổ bot.
- **Đã triển khai/kiểm source:** đề xuất theo nghĩa với critic và host receipt; hỗ trợ nhóm nhiều nguồn, giữ budget/provider privacy và receipt cũ. STATUS kiểm identity/repo/partition và đọc ledger hiện tại.

**3. SQLite và legacy — Dữ liệu lưu cục bộ và luồng xử lý cũ**

- **SQLite:** cơ sở dữ liệu cục bộ lưu tin đã nhận, liên kết tin–ticket, công việc đang chờ và lịch sử thao tác để chống xử lý trùng.
- **Legacy:** code/luồng xử lý và định dạng trạng thái cũ còn tồn tại bên cạnh luồng rules Markdown mới.
- **Đã triển khai/kiểm source:** snapshot read-only, kiểm kê và đánh giá riêng recovery/chuyển đổi; lưu trạng thái gốc với receipt; xác nhận binding và rollback. Converter giữ phạm vi hẹp; lịch sử chưa đủ chứng cứ cần đối soát và tiếp tục giữ legacy, không replay tác động chưa rõ.

[Nơi lưu trữ, cách hoạt động và cách chỉnh thủ công của ba phần](#8-tra-cứu-lưu-trữ-hoạt-động-và-chỉnh-thủ-công).

## 5. Thời gian và điều kiện

**Dự báo ban đầu 4–8 giờ; thực tế lượt triển khai 03/11 khoảng 5 giờ 14 phút elapsed** từ 11:36 đến kiểm tổng cuối lúc 16:50. PM-01 khoảng 114 phút, PM-02 khoảng 74 phút, PM-03 khoảng 121 phút; thời gian gồm sửa/kiểm/review, không phải tổng giờ công các Agent chạy song song. Không dùng số này làm tỷ lệ hoàn thành toàn dự án hoặc benchmark cho dự án khác.

CI, đóng gói/review bản cài và setup vận hành được để ngoài plan đợt này. Các ticket setup hiện hữu giữ nguyên.

## 6. Phụ thuộc và rủi ro

| Phụ thuộc / rủi ro | Ảnh hưởng | Cách xử lý |
|---|---|---|
| Rules mở rộng còn DRAFT | ~~Rules mode chưa khởi động được~~ — **đã xử lý 03–04/11**: lifecycle/analysis-status ACTIVE, rules mode chạy thật | Owner duyệt từng thay đổi rule |
| Test giả bỏ qua contract thực tế | Test xanh nhưng tích hợp model vẫn sai | Kiểm schema và provider boundary thực tế; tách kiểm transport khỏi đánh giá chất lượng model |
| Dữ liệu lịch sử có tác động bên ngoài chưa rõ | Nguy cơ ticket/comment/attachment trùng nếu replay | Giữ reservation, kiểm trên bản sao, đối soát trước khi cho chuyển tiếp |
| Chưa có môi trường kiểm Telegram/model/GitHub thật | ~~Chưa kiểm được toàn bộ luồng thật~~ — **đã kiểm thật 04/11** (P12: READY, NEED_INFO, ANALYZE, STATUS) | Theo dõi điều kiện dừng H1–H7 khi vận hành |
| Thiếu hướng dẫn cài từ đầu cho máy mới | Khó chuyển bot sang máy khác | Viết runbook cài mới; không chạy song song hai bot trên cùng group |
| Công cụ harness-verify hiện chỉ hỗ trợ Node | Không có verdict harness hợp lệ cho repository Python | Ghi đúng giới hạn, dùng output Python checks riêng; không tạo package.json giả để vượt gate |

## 7. Điều kiện hoàn thành đợt này

- Ba task chức năng đáp ứng Business DOCS, có output và reviewer verdict độc lập.
- Chạy kiểm từ source: schema/provider khớp, lifecycle không tạo trùng, ANALYZE/STATUS đúng nguồn, SQLite không mất dữ liệu hoặc replay ghi chưa rõ kết quả.
- Báo cáo rõ ca đã chạy, PASS/FAIL/SKIP và phần chưa xác minh; chỉ kết luận đạt trên đúng phạm vi có bằng chứng.

## 8. Tra cứu lưu trữ, hoạt động và chỉnh thủ công

Các đường dẫn dưới đây tính từ root repository; nhánh triển khai `bugfix/2178_rules-functional-completion` đã được gộp vào `main` (hiện đã bao gồm cả bản sửa 04/11). Cả ba task đã qua source review và kiểm tổng; chưa có PM ký Accepted. Không có thao tác đổi config hoặc dữ liệu live khi viết tài liệu.

### 8.1. Schema và lifecycle

**Nơi lưu trữ**

| Thành phần | Vị trí | Vai trò |
|---|---|---|
| Danh sách dữ kiện và ví dụ | [facts.md](../../listener/rules/facts.md), [examples.md](../../listener/rules/examples.md) | Khai báo trường, nguồn chứng cứ và ví dụ đối chiếu |
| Schema model nhận | [decision_rules.py](../../listener/src/tg_triage/decision_rules.py), [followup_model.py](../../listener/src/tg_triage/followup_model.py) | Sinh schema từ rules và bổ sung contract lifecycle bằng Python |
| Bảng quyết định lifecycle | [lifecycle.md](../../listener/rules/lifecycle.md) | Bằng chứng cũ/tái diễn/chưa rõ → bổ sung/tạo episode/hỏi/giữ lại; hiện DRAFT |
| Kiểm và thực thi | [rules_extensions.py](../../listener/src/tg_triage/rules_extensions.py), [lifecycle.py](../../listener/src/tg_triage/lifecycle.py), [rules_intake.py](../../listener/src/tg_triage/rules_intake.py) | Nạp bảng, kiểm nguồn/chronology/target rồi thực thi |
| Lịch sử xử lý | Trong SQLite tại `storage.db_path`; `rules_lifecycle_questions`, `rules_lifecycle_answers`, `rules_related_episodes`, `rules_related_receipts`, `rules_comments`, `rules_batch_groups` | Lưu câu hỏi, xác nhận, parent/episode, proof nhiều nguồn/media và reservation để chống trùng |

**Cách hoạt động:** nạp rules → sinh schema → model trích dữ kiện → host kiểm nguồn, episode và chronology → áp bảng lifecycle → reserve trước upload/POST → kiểm lại bằng chứng và topic còn OPEN ngay trước POST → xác minh và lưu kết quả. Success đã dùng do ledger xác định; thời điểm nhận tin không tự chứng minh thời điểm đã chạy tốt. Khi chưa rõ freshness hoặc kết quả ghi, bot hỏi/giữ lại để đối soát.

**Cách thay đổi thủ công khi cần:**

- Thay điều kiện/bảng hoặc câu xác nhận lifecycle: sửa bản source `listener/rules/lifecycle.md`; tăng version, bổ sung ví dụ/test và cập nhật Business DOCS. Cấu trúc bảng và các kết quả được phép phải được loader hỗ trợ.
- Thêm trường, enum hoặc trạng thái mới: Dev phải đồng bộ schema, parser, validation và code thực thi; sửa Markdown hoặc prompt đơn lẻ không đủ. `reused_success` được host xác định từ ledger, không phải giá trị model được phép tự gắn trong fresh extraction.
- Không chỉnh bảng lịch sử SQLite để ép ticket chuyển trạng thái hoặc cho POST chạy lại. Thay đổi rules không tự sửa trạng thái ticket GitHub và không tự chuyển dữ liệu cũ.

### 8.2. ANALYZE / STATUS

**Nơi lưu trữ**

| Thành phần | Vị trí | Vai trò |
|---|---|---|
| Bảng định tuyến ý định | [analysis-status.md](../../listener/rules/analysis-status.md) | Phân tích yêu cầu hoặc hỏi trạng thái; hiện DRAFT |
| Contract và câu trả lời ANALYZE | [analysis_task.py](../../listener/src/tg_triage/analysis_task.py) | Kiểm `title`, `work`, `acceptance`, giới hạn độ dài và dựng nội dung |
| Trích dữ kiện/context và xử lý | [decision_rules.py](../../listener/src/tg_triage/decision_rules.py), [rules_extraction.py](../../listener/src/tg_triage/rules_extraction.py), [rules_intake.py](../../listener/src/tg_triage/rules_intake.py) | Tạo schema/hướng dẫn cho rules mode, gửi nguồn/context, kiểm và xử lý kết quả |
| Semantic critic và host proof | `listener/src/tg_triage/rules_analysis.py`; `provider.py`, `thinking.py` | Một critic cho batch, giữ đúng evidence view và ngân sách attempt/deadline |
| Trả lời STATUS | [ticket_status.py](../../listener/src/tg_triage/ticket_status.py) | Đọc sổ bot theo Reply và repository, dựng trạng thái đã ghi nhận/chưa xác nhận |
| Dữ liệu đã xử lý | SQLite: `messages`, `group_notices`, `rules_batch_groups`, `rules_readonly_notices`, `rules_analysis_reviews`, `rules_analysis_review_members` | Lưu nguồn/plan, review độc lập theo group và từng member, receipt gửi để tránh lặp khi restart |

**Cách hoạt động:** model đề xuất task ngắn; một critic kiểm đủ claim của mọi group ANALYZE trên đúng text/document span hoặc pixel thật đã cấp. Host lưu review/partition/member proof rồi reserve notice; chỉ xác nhận sent mới hoàn tất các member. Task không phải chép nguyên văn nguồn. Cache kiểm lại proof, không tự gọi model hoặc gửi lại khi kết quả send chưa rõ. STATUS đọc ledger hiện tại, đúng Reply/partition/repo và notice identity duy nhất; không GET/ghi issue GitHub. Critic là suy luận, chưa phải bằng chứng chất lượng model thật.

**Cách thay đổi thủ công khi cần:**

- Đổi bảng định tuyến: sửa `listener/rules/analysis-status.md` trong source; enum mới cần Dev cập nhật loader/implementation tương ứng.
- Đổi cấu trúc hoặc giới hạn task ANALYZE: Dev sửa `TASK_SCHEMA`, validation và renderer trong `analysis_task.py`, đồng thời cập nhật rules/tài liệu và test. Câu chữ ANALYZE/STATUS ở các renderer này; không phải mọi câu đều nằm trong template Markdown.
- Rules mode tạo hướng dẫn trích dữ kiện tại `decision_rules.py` / `rules_extraction.py`. Không mặc định sửa `prompts/autonomous.md` sẽ đổi được đường rules mới; cần xác định mode đang dùng. Các mẫu issue/notice chung nằm ở `listener/templates/github-ticket/`.
- Đổi critic/proof hoặc budget: Dev kiểm `rules_analysis.py`, provider/Turn và recovery tương ứng; giữ normal 2 attempts/40s và ngoại lệ tài liệu dài TG-14 hiện có. Không sửa tay bảng review/member/notice để giả lập approved hoặc ép gửi lại.
- Chọn provider/model và mode tại file config được truyền qua `--config`; mẫu tham khảo là [config/listener.example.toml](../../config/listener.example.toml). Sửa file mẫu không tự đổi cấu hình máy chạy. Đổi model không thay quy tắc nghiệp vụ.
- Muốn câu trả lời STATUS khác đi phải đối soát bằng chứng nguồn/issue; không sửa tay sổ bot để hiển thị kết quả mong muốn.

### 8.3. SQLite và legacy

**Nơi lưu trữ**

| Thành phần | Vị trí | Vai trò |
|---|---|---|
| File SQLite thực tế | Trường `[storage] db_path` trong config đang sử dụng | Một file chứa dữ liệu runtime; không nằm trong Git |
| Đường dẫn mẫu | `/var/lib/agentfleet/tg/triage.sqlite3` trong config mẫu | Chỉ là ví dụ triển khai Linux; chưa xác nhận DB thật trên máy người dùng |
| Schema và pending | [store.py](../../listener/src/tg_triage/store.py), [rules_pending_store.py](../../listener/src/tg_triage/rules_pending_store.py) | Lưu/đọc tin, pending, plan, notice và reservation |
| Luồng xử lý cũ | [pipeline.py](../../listener/src/tg_triage/pipeline.py), [autonomous.py](../../listener/src/tg_triage/autonomous.py), [followups.py](../../listener/src/tg_triage/followups.py) và các module liên quan | Hành vi/khôi phục cũ còn tồn tại khi chuyển đổi chưa hoàn tất |
| Kiểm kê/chuyển tiếp offline | [legacy_preflight.py](../../listener/src/tg_triage/legacy_preflight.py), `offline_snapshot.py`, [legacy_reconciliation.py](../../listener/src/tg_triage/legacy_reconciliation.py) | Kiểm một snapshot riêng, staging và release có digest/receipt |
| Lưu trạng thái gốc trước staging | SQLite: cột `original_state` trong `rules_legacy_transitions` | Giữ nguyên `decision`/`reason`, gồm Unicode và phân biệt NULL với chuỗi rỗng; receipt `legacy.stage/v2` ràng buộc bản lưu này |

**Cách hoạt động:** nhận tin và lưu SQLite → xử lý theo mode → lưu tiến độ/kết quả từng thao tác. Khi restart, bot kiểm lịch sử trước recovery. Preflight đọc một snapshot riêng nhất quán cho inventory, attachment evidence và đánh giá recovery; không khởi tạo Store hoặc nâng schema của bản sao đầu vào. Báo cáo tách kết quả kiểm tra (`inspection`), khả năng recovery (`recovery_gate`) và điều kiện chuyển đổi (`conversion`). Exit 0 hoặc CLEAR không có nghĩa đã chuyển dữ liệu hoặc được chạy thật.

Chuyển tiếp trên bản sao offline: kiểm kê → staging có attestation và lưu nguyên trạng thái gốc → lấy digest mới và giữ receipt bên ngoài DB → release. Release chỉ đưa nguồn vào HOLD chờ luồng rules; phải kiểm chức năng riêng. Converter giữ phạm vi hẹp: một pending pristine chưa liên kết; có thể kèm tối đa 1.000 tin text CHAT/noise trơ đã đối soát. Lịch sử linked/đã ghi/không rõ kết quả nằm ngoài converter; tiếp tục giữ legacy.

**Cách thay đổi thủ công khi cần:**

1. Muốn đổi vị trí lưu: sửa `[storage] db_path` trong đúng config. Việc đổi đường dẫn không tự chuyển dữ liệu sang file mới. Khi cần di chuyển phải dừng tiến trình dùng DB, tạo backup nhất quán, giữ nguyên dữ liệu và kiểm bản sao trước khi dùng; không copy riêng file `.sqlite3` từ một DB đang có giao dịch/WAL rồi coi đó là backup đầy đủ.
2. Muốn kiểm kê: Operator dùng `legacy-preflight` trên bản sao offline; đọc riêng ba kết quả ở trên. Ledger vắng/khác schema hoặc dữ liệu vượt giới hạn là UNKNOWN, không coi như rỗng. Giữ bản gốc đầy đủ; không xóa lịch sử để vượt kiểm tra.
3. Muốn chuyển tiếp trường hợp được hỗ trợ: dùng `legacy-transition` rồi `legacy-release` với manifest đã đối soát. Digest PowerShell dùng `(Get-FileHash -LiteralPath "<offline-copy.sqlite3>" -Algorithm SHA256).Hash.ToLowerInvariant()`; tính lại sau staging, không dùng lại digest trước staging. Manifest staging cần `offline_copy`, `sha256`, `chat_id`, `message_id`, `decision`, `operator`, `reason`; có lịch sử giữ lại thì cần xác nhận `reviewed_context`. Giữ `stage_receipt_sha256` trả về ngoài DB. Manifest release cần digest mới, receipt gốc, định danh, operator/reason và `release_for_rules: true`. Không tự tạo bằng chứng để vượt kiểm tra.
4. Pending nhiều liên kết, hydration đã tiến triển, media thiếu bằng chứng và ghi bên ngoài chưa rõ kết quả cần đối soát riêng. Một attachment có durable `prewrite_error` đúng định danh có thể staging theo attestation; release vẫn HOLD khi chưa chứng minh được bytes. Không reset reservation hoặc sửa `reserved`/`ambiguous` thành thành công để tiếp tục. Stage cũ thiếu `original_state` phải tìm bản sao trước staging/backup đáng tin; không tự đoán lý do cũ hoặc tạo receipt thay thế.

Ví dụ cú pháp — thay đường dẫn giữ chỗ bằng **bản sao offline và manifest đã được kiểm**, không trỏ vào DB đang chạy:

```powershell
# Chạy từ listener/, dùng PYTHONPATH=src nếu chưa cài package.
$env:PYTHONPATH = "src"
python -m tg_triage legacy-preflight --db "<offline-copy.sqlite3>" --attachment-evidence
python -m tg_triage legacy-transition --db "<offline-copy.sqlite3>" --manifest "<stage-manifest.json>"
python -m tg_triage legacy-release --db "<offline-copy.sqlite3>" --manifest "<release-manifest.json>"
```

### 8.4. Quy trình chung khi đổi rules/config

Sửa bản source → tăng version và cập nhật ví dụ/Business DOCS → chạy kiểm tra → reviewer kiểm → Owner duyệt rules cần ACTIVE → `check-config` đối chiếu fingerprint → khởi động lại trong môi trường được phép. Rules được nạp lúc startup, không có hot reload hoặc thư mục override cho người dùng.

Bản cài tìm rules tại `<sys.prefix>/share/tg-triage/rules`, còn chạy source ưu tiên `listener/rules/`. Không sửa trực tiếp file trong bản cài để tạo một phiên bản khác source. Hiện hai rules mở rộng DRAFT làm loader rules mode dừng; không chỉ sửa header ACTIVE để bỏ qua cổng duyệt. Một số bảng/enum còn bị Python giới hạn, nên thay đổi vượt danh sách hỗ trợ cần Dev sửa và kiểm cả contract.

Các lệnh kiểm quy tắc khi Dev đã sửa source:

```powershell
# Tại listener/
$env:PYTHONPATH = "src"
python -m unittest tests.test_rules_markdown tests.test_rules_extensions

# Tại repository root
python scripts/check-telegram-triage-policy.py
```

Các lệnh này kiểm hợp lệ và regression trong phạm vi nêu trên; không thay cho bộ kiểm đầy đủ của thay đổi hoặc kiểm chất lượng model thật.
