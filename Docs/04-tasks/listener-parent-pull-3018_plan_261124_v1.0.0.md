# Kéo chuỗi cha F2 vào ticket (`parent_pull`) — Plan

**Phiên bản:** v1.0.0 · **Ngày:** 2026-11-24 · **Owner:** AlexChu · **Trạng thái:** Owner đã duyệt 2026-11-24 (phạm vi lựa chọn 1, gom lô 60 s / 120 s, executor Codex `gpt-6.2-sol` · `high`, verify `nx-verifier` + `nx-reviewer`)
**Task harness:** `T-261124-3018-f2-keo-chuoi-cha-vao-ticket-ready-l` · Route C · GATE 0b (tạo issue GitHub thật, đổi trạng thái `messages`)
**Quyết định Owner 2026-11-24:** "Hướng B: kéo chuỗi cha".

## 0. Vì sao

Ca live ngày 24/11, group DEV - NEXORA (chat `-1004223009635`), đọc từ DB live (bản đọc `mode=ro`):

| Tin | Nội dung | Run | Kết quả |
|---|---|---|---|
| 7131 | Mai: link PR + "review giúp em pr nha anh @PmqQuan" | 177 (`early`) | CHAT · F1 (đúng) |
| 7132 | Leo, Reply 7131: "Mai xem chỉnh lại icon … @vomaingoc nó đang dùng unicode icon nữa r" | 178 | CHAT · F2 |
| 7133 | Leo, ảnh trần thanh menu | 178 | CHAT · F2 |
| 7134 | Mai, Reply 7133: "à này là phía người chưa đăng nhập …" | 179 | READY · R10 |
| 7135 | Leo: "sửa cho đồng bộ đi Mai cùng ý nghĩa" | 179 | READY · R10 |

Ticket `PersonalProjectJob/telegram-agent#206` chỉ có 7134 + 7135, tiêu đề "sửa cho đồng bộ — trang chưa đăng nhập", thiếu yêu cầu gốc và ảnh. Mong muốn: một ticket task gồm 7132, 7133 (ảnh evidence), 7134, 7135; 7131 (nhờ review PR) không vào.

## 1. Luật mới: bảng `parent_pull` (readiness.md v17 → v18)

Đặt sau `reply_chat`, trước `image_split`. Khóa hash bằng `PARENT_PULL_SHA256`. `team_request` và `reply_chat` không đổi.

| # | Bước |
|---|---|
| J1 | Chỉ xét lô mà chính các tin của lô không khớp lọc local nào (F7, F1, F3, F4, F6, F5). Tin xét là tin Reply trong lô, không forward, không command |
| J2 | Tin cha: cùng group, đã lưu, không forward; đang lưu `chat` với lý do `F2`; chưa gắn ticket/case; không nằm trong chủ đề đang `OPEN`; gửi không quá 24 giờ trước tin Reply |
| J3 | "Chủ đề F2" là đúng một chủ đề đã đóng có quyết định cuối `CHAT/F2` và chứa tin cha. Kéo các tin trong chủ đề đó còn `CHAT/F2`, không forward, cùng người gửi với tin cha. Không đi tiếp chuỗi Reply của tin bị kéo (7131 không bao giờ vào). Không có đúng một chủ đề ⇒ chỉ kéo tin cha. Vượt `max_messages_per_window` ⇒ không kéo |
| J4 | Tin kéo vào lô **trước** khi gọi trợ lý/model, như tin nguồn (ảnh được xem). Nhóm chứa tin kéo không xét `team_request` |
| J5 | Nhóm của tin Reply ra `READY` ⇒ ticket gồm cả tin kéo. Kết quả khác, hoặc trợ lý tách tin kéo sang nhóm khác ⇒ tin kéo giữ nguyên `CHAT/F2`, không ghi gì: hành vi như hôm nay |
| J6 | Giao việc thuần (không ai Reply rồi chốt thành việc) vẫn là `CHAT` theo F2 |

Kéo trước khi gọi model vì: tiêu đề và trường ticket do trợ lý trích từ tin nguồn; ảnh chỉ được chuẩn bị cho tin nguồn; `guard_image_split` tự giữ ảnh và tin Reply vào ảnh cùng nhóm.

## 2. Điểm sửa code

- `decision_rules.py`: schema bảng `parent_pull` + `parent_pull_cases`; hằng `PARENT_PULL_SHA256`, `PARENT_PULL_WINDOW_SECONDS`; hàm thuần tự kiểm ca mẫu lúc nạp.
- `rules_intake.py`:
  - `_pull_parents(pipeline, sources)`: truy vấn `messages`, `rules_sources`, `rules_topics`, `rules_decisions`.
  - `_process_batch`: chạy `early_decision` trên tin gốc; chỉ khi ra None mới mở rộng lô, trước `prepare_images`; sweep sau restart nhận lại tin kéo.
  - `_process_batch_inner`: `batch_id` và trace `early` theo tin gốc; không ghi `pending_review` cho tin kéo; bỏ tin kéo khỏi `external`; truyền `parent_pull` qua `batch_context` (contract `decide/v1` không đổi); đường propose dùng `team=False` khi nhóm có tin kéo.
  - Thực thi: topic theo tin không kéo nhỏ nhất (vd `…:7134`, tránh đụng topic F2 đã đóng); claim tin kéo (`chat/F2 → classifying`, rowcount=1) trước PLANNED→RESERVED, thất bại ⇒ `rules_parent_pull_changed`; nhóm chỉ có tin kéo ⇒ DONE không ghi gì; trạng thái khác `READY` ⇒ không `finish` tin kéo.
- Không đổi `issue_format.py`, template, `classification_log`, schema DB.
- Dựng lại pack: `assistant-triage.md`, `assistant-core.json`, bản `drafts/decide-v1`.

## 3. Ca mẫu (`examples.md` v22 → v23, bảng `parent_pull_cases`)

PP1 ca live (ẩn danh) ⇒ kéo · PP2 Reply vào tin F1 nhờ review PR ⇒ không · PP3 nhóm ra CHAT ⇒ không · PP4 NEED_INFO ⇒ không · PP5–PP8 cha forward / cha quá 24 giờ / cha đã có ticket / cha trong chủ đề OPEN ⇒ không. Không thêm dòng vào bảng `examples` gửi model (ngân sách 8000 byte).

## 4. Docs, test, verify

- Docs: `rules/README.md` §1/§3; Business DOCS TG-05, TG-09, changelog, trace JSON (`runtime_inventory`, `runtime_fingerprint`); business-flow bước 5.
- Test: `test_rules_markdown`, `test_decision_rules`, `test_rules_intake_decide` (ca live ra 1 ticket 4 tin, ảnh 7133 được đính, 7131 không đổi; ca âm CHAT/NEED_INFO/F1; crash sau `save_batch` rồi replay; hai lô cùng kéo một cha), `test_rules_intake` (đường propose).
- Lệnh (`agentfleet_common` ở `C:\Users\AD\Documents\GitHub\AgentBuilder-agentfleet-common\common\src`):

```
cd listener && PYTHONPATH="src;C:/Users/AD/Documents/GitHub/AgentBuilder-agentfleet-common/common/src" python -m unittest discover -s tests -t .
python scripts/check-telegram-triage-policy.py
python scripts/test_telegram_triage_policy.py
python scripts/build-assistant-pack.py --check
python scripts/build-assistant-pack.py --check --principles listener/assistant/drafts/decide-v1/principles.md --out listener/assistant/drafts/decide-v1/assistant-triage.md
```

## 5. Rủi ro

- Ở chế độ decide, trợ lý vẫn có thể áp F2 hoặc tách tin kéo; J5 trả về hành vi hôm nay, không bảo đảm 100%.
- Request lớn hơn (thêm ảnh) có thể rơi vào nhánh `too_large`.
- Fingerprint rules và pack đổi ⇒ mất cache prompt; cài bản mới cần phiếu preflight GO mới và precheck DB live (đọc bản sao `mode=ro`).

## 6. Câu hỏi Owner — ĐÃ CHỐT 2026-11-24: "thực hiện lựa chọn 1" (đồng ý cả 5 đề xuất)

1. `NEED_INFO`: tin kéo có theo chủ đề chờ trả lời không? Đề xuất: không (v1).
2. `APPEND`: có kéo vào comment ticket khác không? Đề xuất: không.
3. Cửa sổ 24 giờ? Đề xuất: đồng ý.
4. Chỉ kéo tin cùng người gửi với tin cha? Đề xuất: đồng ý.
5. Marker `<!-- tg: -->` và reaction gắn tin Reply đầu tiên (7134)? Đề xuất: 7134.

## 6b. Nới thời gian gom lô (Owner 2026-11-24: "60s / 120s")

Hiện lô đóng lúc `min(tin_cuối + quiet, tin_đầu + max_wait)` với quiet 30 s, max 60 s, giới hạn cứng `30 ≤ quiet ≤ max ≤ 60`. Ca live bị tách vì 7133 → 7134 cách 46 s; cả chuỗi 7132 → 7135 dài 97 s.

- Mặc định mới: `context_quiet_seconds = 60`, `context_max_wait_seconds = 120`; giới hạn cứng mới `30 ≤ quiet ≤ max ≤ 120`.
- File: `listener/src/tg_triage/pacing.py` (dòng 21–22), `listener/src/tg_triage/config.py` (190–191, 509–512), `config/listener.example.toml` (109–110), `listener/README.md` (415), `listener/tests/test_chat_pacing.py`, Business DOCS (đoạn "Sau 60 giây" dòng 47–51 + `runtime_inventory`).
- Test: giá trị 60/120 hợp lệ; 121 và quiet > max bị từ chối; ca 4 tin cách 27/46/24 s vào cùng một lô.
- Config live `/etc/tg-triage/listener.toml` trên gw-01 đổi lúc deploy (Owner, sudo), ghi trong phiếu preflight mới.

## 6c. Vòng 2 — sau review GATE 0b (nx-reviewer CHANGES_NEEDED, 2026-11-24)

Quyết định Owner 2026-11-24:
- Cùng một lô (60/120 s gom 7132–7135 vào một lô): "trong 1 batch vẫn có thể có 2 nội dung khác nhau, cần Trợ lý phân tích và đưa ra quyết định" ⇒ không thêm luật code ép bỏ F2 trong lô; trợ lý quyết, chỉ thêm test.
- "Cho kéo trước F6": tin Reply vào cha `CHAT/F2` được kéo kể cả khi tự nó khớp F6; vẫn chỉ tạo ticket khi nhóm ra `READY` (J5).
- "Chạy lại lô không kéo": lô có tin kéo bị lỗi riêng (request quá lớn, trợ lý không phủ ID tin kéo, tách tin kéo sang nhóm khác) ⇒ bỏ tin kéo, xử lý lại lô như hôm nay.

Sửa theo review:
1. Lọc nội dung từng tin ứng viên (F1/F3/F4/F7) trước khi kéo, kể cả khi tin đang lưu `F2`.
2. Claim tin kéo cùng transaction với tạo topic/`add_source`; dấu claim riêng (không dùng `classifying`); gate khởi động và recovery của `Store` nhận ra tin kéo, trả về `chat/F2` khi chưa gọi GitHub; không bao giờ đưa tin kéo về `queued`.
3. `rate_limited` / lỗi GitHub ⇒ trả tin kéo về `chat/F2`, không để trong topic OPEN.
4. Claim thất bại ⇒ chỉ bỏ phần kéo, không escalate cả lô, không làm mất kết quả các nhóm khác.
5. Read context của lô có tin kéo không vượt cửa sổ 24 giờ.
6. Cập nhật 30/60 cũ ở Docs overview/engineering/QA và đoạn F3 "30 giây" trong Business DOCS.
7. Test: crash sau claim + chạy gate; `rate_limited`/lỗi GitHub; tin nội dung F1 lưu `F2`; ca live một lô; F6 bị vượt; fallback chạy lại không kéo; worker ở mặc định 60/120.

## 6d. Vòng 3 — sau review GATE 0b vòng 2 (nx-verifier PASS 2647 OK; nx-reviewer CHANGES_NEEDED)

Quyết định Owner 2026-11-25: "Chỉ áp dụng ở chế độ decide" — `parent_pull` đứng trước F6 chỉ khi `authority = "decide"`; ở `propose`, Reply khớp F6 vẫn là `CHAT/F6` local như trước, không kéo (tránh tái lỗi live #121).

Sửa theo review vòng 2:
1. Ứng viên kéo (kể cả tin cha) luôn qua bộ lọc nội dung F1/F7 (và F3/F4) bất kể là Reply hay có đính kèm; tin cha là Reply nhờ review PR không bao giờ bị kéo.
2. Tin được Reply bị bộ lọc loại ⇒ bỏ kéo cho Reply đó (không kéo anh em cùng chủ đề).
3. POST GitHub không rõ kết quả (đã `reserve_github_call`) ⇒ giữ tin kéo bằng dấu riêng mà J2 loại ra; chỉ thả về `chat/F2` khi chắc chắn chưa gọi GitHub.
4. Lưu quyết định "chạy lại không kéo" cho lô/topic để sweep không kéo lặp lại.
5. Dòng `classification_runs` của lượt có tin kéo bị thay thế ⇒ đánh dấu đã thay thế.
6. Docs: overview dòng 66, Rulebook TG-05.1/TG-05.4 (60 s / 120 s), Business DOCS ghi đánh đổi (#1, #2), `listener/README.md:399`.
7. Test chốt hash `test_assistant_contract.py`: nêu lý do hoặc chốt hash trực tiếp.

## 6e. Vòng 4 — sau review GATE 0b vòng 3 (CHANGES_NEEDED; mọi điểm vòng 2 đã đạt)

Không cần quyết định mới; sửa theo quyết định đã có (§6c, §6d, J6, luật PR review 2026-11-05):
1. Ở propose, `team_request` xét trên các tin không kéo; tin kéo không được tắt F2 của chính Reply giao việc có @tên.
2. Reply kích hoạt kéo mà tự nó khớp F1/F7/F3 (nhờ review PR, xác nhận merge, chỉ link) ⇒ không kéo, đi đường hôm nay.
3. Runbook Owner cho dấu `rules_parent_pull_ambiguous` (issue đã tạo / chưa tạo).
4. Decide: Reply F6 khi trợ lý chưa nhận (outage) ⇒ chạy lại không kéo, không giữ.
5. Admin badge + script replay cho `SUPERSEDED`.

## 6f. Vòng 5 — sau review GATE 0b vòng 4 (mọi điểm vòng 3 đạt, 0 lỗi mới; 1 blocker có từ vòng 1 ở propose)

Quyết định Owner 2026-11-25:
- "Chỉ bật parent_pull ở decide" — `authority = "propose"` không kéo gì, hành vi y hệt `main` (gỡ mọi nhánh propose của parent_pull). Lý do: ở propose code tính READY từ dữ kiện, nên nội dung kéo vào có thể tự sinh việc (3 ca sai: #121, tự Reply giao việc, câu xác nhận "dạ em sửa liền anh").
- "Thêm hướng dẫn J4" — luật gửi trợ lý ghi: tin kéo là yêu cầu gốc; xét F2 (giao việc thuần) theo các tin Reply mới; khi người giao chốt cách làm hoặc việc cần làm thì không coi là giao việc thuần; trợ lý vẫn quyết cuối.

Sửa thêm: runbook ghi đúng anchor marker = ID nhỏ nhất trong `source_keys` (không phải tin Reply).

## 6g. Vòng 6 — sau review GATE 0b vòng 5 (propose = main đã xác nhận; decide không đổi)

Owner duyệt 2026-11-25 câu J4 (nguyên văn, đưa vào bảng `parent_pull` phần gửi model):
«Tin kéo là yêu cầu gốc, chỉ để hiểu ngữ cảnh. Chỉ ra READY khi các tin Reply mới chốt việc cần làm hoặc cách làm (vd người giao: "sửa cho đồng bộ đi Mai cùng ý nghĩa"). Lời nhận việc, hứa làm, báo đã xong, hỏi lại hoặc nhờ review PR của người được giao KHÔNG biến tin kéo thành việc mới — khi đó giữ CHAT/F2. Trợ lý quyết cuối.»

Sửa:
1. Checkpoint có tin kéo tạo ở decide rồi chuyển sang propose ⇒ chạy lại tin gốc không kéo (như quyết định C), không kẹt im lặng.
2. Câu J4 trên + khôi phục định nghĩa J6 "(không ai Reply rồi chốt thành việc)"; ca mẫu nếu còn ngân sách.
3. Reply có link PR/issue GitHub (P1) ⇒ không kéo, bảo đảm bằng code.
4. Ngữ cảnh đọc của lô có tin kéo gồm cả các tin nằm giữa tin cha và Reply (trong 24 giờ).

## 6h. Vòng 8 — sau vòng 7 (nx-verifier PASS 2667 OK; nx-reviewer APPROVED)

Owner chốt 2026-11-25 ba điểm không chặn:
1. "Sửa trong PR này": lô có tin kéo mà phải bỏ `group_mood` (hoặc tin đích của Reply gốc) để vừa ngân sách ⇒ chạy lại không kéo.
2. "Miễn 24h như HEAD": tin đích Reply của các tin gốc không kéo giữ như HEAD (không giới hạn 24 giờ); chỉ các tin ở giữa tin cha và Reply kéo mới giới hạn 24 giờ.
3. "Bỏ để khớp HEAD": lô có tin kéo không gửi thêm `mood_examples` (X26/X27); payload = HEAD + tin kéo + `parent_pull`.

## 7. File (≈14, không có file mới ngoài plan này)

`listener/rules/{readiness,examples,README}.md` · `listener/src/tg_triage/{decision_rules,rules_intake}.py` · `listener/assistant/{assistant-triage.md,assistant-core.json}` · `listener/assistant/drafts/decide-v1/assistant-triage.md` · `listener/tests/{test_rules_markdown,test_decision_rules,test_rules_intake_decide,test_rules_intake}.py` · `Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md` · `Docs/01-product/telegram-triage-noissue_business-flow_261028_v1.0.0.md`.

Chạy một stream (rules + `decision_rules` trước, `rules_intake` + test sau, docs và hash cuối cùng): hai phần phụ thuộc nhau nên không tách song song.
