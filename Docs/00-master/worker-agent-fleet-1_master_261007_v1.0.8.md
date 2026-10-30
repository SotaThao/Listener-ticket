# Master Document: Worker Agent Fleet — mỗi máy Ubuntu là một worker, cả đội làm việc qua Telegram

<!-- telegram-triage-historical -->
> **Telegram triage — phần lịch sử/đề xuất:** mô tả nghiệp vụ triage trong tài liệu này không còn là nguồn hiện hành. Xem [TG-01..TG-32](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md) cho bản đã triển khai baseline `9746ef2`. Các kế hoạch Fleet/worker ngoài triage, sign-off, incident và số đo cũ được giữ nguyên; không coi đề xuất là chức năng đã chạy.


## Metadata
- **Feature ID**: 1 (ticket nội bộ — repo chỉ local, chưa có GitHub issue)
- **Branch Name**: feat/1_worker-agent-fleet
- **File Naming Convention**: worker-agent-fleet-1_<type>_<yymmdd>_v<major>.<month>.<day>.md
- **Created Date**: 2026-10-07 15:54
- **Last Updated**: 2026-10-15 21:12 (Asia/Saigon)
- **Current Version**: v1.0.9
- **Author**: AlexChu (Main Agent harness, phiên Claude Code)
- **Status**: Draft
- **Approved By**: — (chờ duyệt)

## Bổ sung phạm vi 2026-10-15 — chuẩn hóa/nghiên cứu trước triển khai

User đã yêu cầu chuẩn hóa nghiệp vụ và nghiên cứu Harness THINKING trong phiên này. Đây là quyền thực hiện phần chuẩn hóa/nghiên cứu; **không phải phê duyệt toàn bộ Fleet Master**, các sign-off Draft phía dưới được giữ nguyên.

- [Nguồn nghiệp vụ duy nhất TG-01..TG-32](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md): mô tả bản triển khai tại baseline `9746ef2`, truy vết code/test và giới hạn.
- [Nghiên cứu THINKING](../02-engineering/telegram-triage-noissue_thinking-research_261015_v1.0.0.md): đề xuất kiến trúc, chưa triển khai hay đổi cấu hình production.
- Các mục triage trước đây phía dưới là bối cảnh/đề xuất lịch sử; quy tắc hiện hành nằm ở nguồn trên. Lộ trình bootstrap, worker và Fleet khác vẫn giữ giá trị phạm vi riêng.
- Chỉ thay tài liệu và công cụ kiểm tra drift tài liệu; không đổi runtime, prompt, DB hay cấu hình đang chạy. Không có ticket mới cho việc này; `noissue` trong tên tài liệu là chủ đích. [PR #43](https://github.com/PersonalProjectJob/telegram-agent/pull/43) là liên kết thay đổi, không phải issue yêu cầu.

## 1. Feature Overview

### Image-caption context correction — 2026-10-15

Post-deploy verification found hosted malformed JSON followed by a slow local fallback despite the correct image bundle. Authorized correction: THINKING uses its remaining second attempt for same-host format validation retry, with identical context/schema; transport failures retain configured fallback. Two actual calls and20/40second limits remain unchanged. No parser relaxation or private-output repair prompt.

User bổ sung: ảnh, caption và hội thoại/reply liên quan phải nằm trong cùng một gói context USER gửi model. Mapping thứ tự ảnh → source ID phải rõ khi nhiều ảnh; retry/fallback gửi lại nguyên gói, không đọc ảnh riêng rồi ghép suy luận.

User yêu cầu xử lý caption thiết kế kèm screenshot và không trộn trao đổi backend/ack không liên quan vào ticket. Audit issue46 cho thấy zero model calls vì envelope vượt budget, rồi fallback gộp toàn case. Phạm vi sửa: renderer THINKING gọn có ID/reply/media provenance và truncation rõ; fallback chỉ theo scope có bằng chứng; image/model smoke dùng asset gốc với fake tools, không replay live DB/issue/notices. Giữ8000bytes,20/40seconds,hai attempts, provider/consent và shared service modules.

### Phạm vi THINKING được user yêu cầu triển khai — 2026-10-15

User đã yêu cầu triển khai thiết kế THINKING, kiểm thử các hội thoại đã gửi, cập nhật PR #43 và deploy THINKPAD. Phạm vi gồm controller có ngân sách tổng, quyết định có version/bằng chứng, bảo toàn xác nhận và thao tác đã reserve, switch rollback và kiểm thử offline bằng tool giả. Tham khảo Harness AlexChu chỉ ở mô hình state/validate/verify; không import executor CLI hay sửa Harness canonical. Đây là quyền triển khai riêng cho triage, không thay sign-off Fleet Draft. Kết quả deploy/verification được báo bằng bằng chứng thực tế sau triển khai.

Acceptance: [QA THINKING](../03-qa/telegram-triage-noissue_qa_261015_v1.0.0.md). Phần chuẩn hóa/nghiên cứu trước triển khai ở trên giữ lịch sử; phạm vi mới thay giới hạn docs-only cho tính năng này.

### Problem Statement

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.
Các agent AI (Claude Code, Codex, Antigravity) hiện chạy rải rác trên máy Windows dev. Chúng không có danh tính worker cố định và không có kênh chung để nhận việc hay báo cáo. Telegram mới chỉ được dùng một chiều (bot gửi thông báo vào thread 718/727/735). Chưa có gì lắng nghe Telegram rồi tạo ticket GitHub.

### Business Context
- Mục tiêu là một **đội agent vận hành như team thật**: mỗi máy là một worker có vai trò cố định, nhận việc, báo tiến độ và trao đổi qua Telegram.
- Yêu cầu từ Telegram (bug, feature, câu hỏi) được **tự động phân loại thành ticket GitHub**, không phải chép tay.
- Ưu tiên dùng **tài khoản subscription** (Claude Pro/Max, ChatGPT Plus/Pro, Google) thay vì API key trả theo token.
- Kết nối Telegram tại Việt Nam có thể bị chặn, nên cần **MTProxy** làm đường vào ổn định.

### Goals & Objectives
- G1: Dựng một máy Ubuntu thành worker hoàn chỉnh bằng **một script bootstrap** chạy lại được (idempotent).
- G2: Mỗi worker có **danh tính cố định** (`worker_id`, vai trò, danh sách agent) đọc từ file cấu hình. Đổi máy hay cài lại thì danh tính không đổi.
- G3: Một **listener Telegram dùng tài khoản cá nhân (MTProto)**, đi qua MTProxy, phân loại tin nhắn và tạo issue GitHub có nhãn.
- G4: Một **giao thức team** (định dạng tin nhắn, thread, ai nhận việc gì) để các worker giao tiếp qua Telegram như một team.

## 2. Scope

### In-Scope

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.
1. **Bootstrap worker Ubuntu**: cài git, gh, Node LTS, Python 3, Claude Code CLI, Codex CLI, Antigravity, tuỳ chọn Docker CE; tạo user và thư mục làm việc; ghi file danh tính worker.
2. **Danh tính worker**: `worker.yaml` (ví dụ `worker_id`, `role`, `agents`, `telegram_thread`), bản mẫu `worker.example.yaml` trong repo.
3. **Kết nối Telegram**: giai đoạn 1 kết nối **thẳng** (kiểm tra 2026-10-08: mạng không chặn), code giữ sẵn ô cấu hình proxy. MTProxy (`mtg` trên VPS nước ngoài) là bước tối ưu sau, chỉ làm khi bị chặn hoặc khi chuyển lên VPS (D7).
4. **Telegram → GitHub listener** (chạy duy nhất trên một máy "gateway"):
   - Đăng nhập tài khoản cá nhân bằng MTProto (Telethon), lưu session file trên máy, không commit.
   - Nghe những group/chat được cấu hình sẵn.
   - Phân loại tin nhắn (bug / feature / question / task / noise) rồi tạo issue GitHub có nhãn và link về tin nhắn gốc; chống tạo trùng.
   - Bộ phân loại chạy **model local Qwen 3.5 9B qua Ollama** trên máy gateway (D5), không tốn quota subscription.
5. **Giao thức team qua Telegram**: định dạng tin nhắn giao việc và báo cáo, thread theo worker hoặc vai trò, quy ước gắn issue.
6. **Service hoá**: chạy listener và agent runner bằng `systemd`, tự khởi động lại, có log.

### Out-of-Scope
- **Đăng nhập tài khoản subscription và Telegram thay người dùng.** Mọi bước OAuth, mã OTP Telegram, 2FA đều do người dùng tự làm trên từng máy. Script chỉ dừng lại và hướng dẫn.
- Worker chạy Windows (đã có `alexchu-bootstrap` riêng).
- Triển khai lên cloud hoặc VPS công cộng.
- Sửa lỗi Docker trên máy chủ Docker hiện tại: là **track riêng**, chờ log lỗi từ máy đó (xem §10).
- Antigravity chạy headless: Antigravity là app GUI, nhận việc bằng prompt dán tay, không tự động hoá như hai CLI kia.

## 3. Key Personas (Summary)

### Primary Users
- **Owner / Lead (người dùng)**: giao việc và đặt câu hỏi qua Telegram, duyệt ticket, nhận báo cáo từ các worker.
- **Worker agent**: Claude Code, Codex hoặc Antigravity trên một máy cụ thể; nhận việc từ ticket hoặc tin nhắn rồi báo cáo lại qua Telegram.

### Secondary Users
- **Thành viên team khác trong group Telegram**: gửi bug/yêu cầu, tin nhắn của họ được chuyển thành ticket.
- **AlexChu harness**: chủ sở hữu dữ liệu, task history và handoff. Worker ghi state theo chuẩn harness (`.agent-tasks/`).

## 4. Success Metrics (KPIs)

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.
- **Bootstrap**: một máy Ubuntu 26.04 sạch thành worker sẵn sàng trong **≤ 30 phút**, không tính thời gian người dùng đăng nhập thủ công. Chạy lại script lần 2 không đổi gì (idempotent).
- **Listener**: **≥ 95%** tin nhắn hợp lệ trong group được theo dõi tạo đúng 1 issue (không trùng), độ trễ **≤ 60 giây**.
- **Phân loại**: **≥ 85%** nhãn đúng trên tập 50 tin nhắn mẫu do Owner chấm.
- **Ổn định**: listener chạy liên tục **7 ngày** không cần can thiệp tay (systemd tự khởi động lại khi rớt proxy).
- **Bảo mật**: 0 secret (bot token, `api_hash`, session file, PAT) trong git history.

## 5. High-Level Timeline
- **Phase 0 — Chốt thiết kế + gỡ Docker**: duyệt Master Doc, trả lời câu hỏi mở (§10), lấy log Docker. **07–09/10/2026**
- **Giai đoạn 1 (MVP, theo D6)** — Telegram Triage Agent local: xem Setup Plan v1.0.2. Các phase dưới đây là lộ trình đầy đủ, thực hiện sau MVP.
- **Phase 1 — Bootstrap worker Ubuntu + danh tính**: script cài đặt, `worker.yaml`, kiểm thử trên máy Ubuntu đã setup. **09–14/10/2026**
- **Phase 2 — MTProxy + Telegram listener → GitHub**: proxy, Telethon client, bộ phân loại, tạo issue, systemd. **14–21/10/2026**
- **Phase 3 — Giao thức team**: định dạng tin nhắn, thread theo worker, agent runner nhận việc và báo cáo. **21–28/10/2026**
- **Phase 4 — Nhân rộng**: thêm máy thứ 2 trở đi bằng bootstrap, chạy thử 7 ngày. **28/10–04/11/2026**

## 6. Stakeholders & Responsibilities
- **Product / Owner**: @dev (người dùng). Duyệt scope, nhãn ticket, giao thức team.
- **Design**: không áp dụng (không có UI; "UX" là định dạng tin nhắn Telegram, nằm trong Product Doc).
- **Development**: executor do `/dispatch` chọn ở cổng duyệt plan (Codex / Gemini / Sonnet subagent).
- **QA**: verifier chéo theo `/dispatch` (khác executor) cùng Owner chấm tập 50 tin nhắn mẫu.
- **Project Manager**: AlexChu (harness): điều phối, theo dõi tiến độ, cập nhật tài liệu.

## 7. Related Documents
- **Product Doc**: `01-product/worker-agent-fleet-1_product_<yymmdd>_v1.x.x.md` — *chưa tạo, chờ duyệt Master Doc*
- **Engineering Doc**: `02-engineering/worker-agent-fleet-1_engineering_<yymmdd>_v1.x.x.md` — *chưa tạo*
- **QA Doc**: `03-qa/worker-agent-fleet-1_qa_<yymmdd>_v1.x.x.md` — *chưa tạo*
- **Solution Brief (trình bày CTO)**: [`00-master/worker-agent-fleet-1_brief_261007_v1.0.7.md`](./worker-agent-fleet-1_brief_261007_v1.0.7.md)
- **Setup Plan giai đoạn 1 — Telegram Triage Agent (trình bày CTO)**: [`00-master/worker-agent-fleet-1_setup-plan_261008_v1.0.6.md`](./worker-agent-fleet-1_setup-plan_261008_v1.0.6.md)
- **Tasks Doc**: `04-tasks/worker-agent-fleet-1_tasks_<yymmdd>_v1.x.x.md` — *chưa tạo*
- **GitHub Issue**: không có (repo chỉ local)
- **Pull Request**: không có

## 8. Changelog
| Version | Date | Time (HH:mm) | Author | Description | Reviewed By | Status |
|---------|------|--------------|--------|-------------|-------------|--------|
| v1.0.0 | 2026-10-07 | 15:54 | AlexChu (Claude Code) | Bản nháp đầu, từ yêu cầu `/dispatch` + 4 quyết định đã chốt | — | Draft |
| v1.0.1 | 2026-10-07 | 16:25 | AlexChu (Claude Code) | Thêm D5 (Qwen 3.5 9B local cho bộ phân loại), cập nhật Q4, A4, thêm link Solution Brief | — | Draft |
| v1.0.2 | 2026-10-08 | 08:27 | AlexChu (Claude Code) | Sửa A3: MTProxy phải ở VPS nước ngoài (máy Docker nội bộ không còn trên đường găng); cập nhật Q6; thêm link Setup Plan, Brief v1.0.1 | — | Draft |
| v1.0.3 | 2026-10-08 | 08:38 | AlexChu (Claude Code) | Thêm D6: MVP chỉ gồm Telegram Triage Agent chạy local; Setup Plan v1.0.1; Brief v1.0.2 | — | Draft |
| v1.0.4 | 2026-10-08 | 08:50 | AlexChu (Claude Code) | Setup Plan lên v1.0.2 (thêm phụ lục thuật ngữ); cập nhật link | — | Draft |
| v1.0.5 | 2026-10-08 | 09:33 | AlexChu (Claude Code) | Thêm D7 (build trên ThinkPad, kết nối thẳng, VPS tối ưu sau); sửa A2, Q6, mục MTProxy trong scope; link Setup Plan v1.0.3 | — | Draft |
| v1.0.6 | 2026-10-09 | 11:52 | AlexChu (Claude Code) | Ghi kết quả B4 đo thật (A4, bổ sung D5): giữ Qwen 3.5 9B, 5,7 giây/tin; link Setup Plan v1.0.4 | — | Draft |
| v1.0.7 | 2026-10-09 | 15:09 | AlexChu (Claude Code) | B0, B2, B5 xong; sửa A1 (Ubuntu 26.04.1); thêm D8 (repo thử nghiệm); link Setup Plan v1.0.5 | — | Draft |
| v1.0.8 | 2026-10-09 | 15:27 | AlexChu (Claude Code) | B3 xong phần làm tay ⇒ chuẩn bị máy giai đoạn 1 hoàn tất (B0, B2–B5); link Setup Plan v1.0.6 | — | Draft |

## 9. Approval & Sign-off
- [ ] **Product Lead**: @dev - Date: YYYY-MM-DD
- [ ] **Tech Lead**: @dev - Date: YYYY-MM-DD
- [ ] **QA Lead**: @dev - Date: YYYY-MM-DD
- [ ] **Project Manager**: AlexChu - Date: YYYY-MM-DD

## 10. Notes & Assumptions

### Quyết định đã chốt (2026-10-07)

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.
| # | Quyết định | Hệ quả |
|---|---|---|
| D1 | Repo `AgentBuilder`, chỉ local | Tracking bằng `Docs/` + `.agent-tasks/`; chưa có issue/PR. Ticket do listener tạo phải trỏ tới **repo khác** (xem Q2). |
| D2 | Nghe Telegram bằng **tài khoản cá nhân qua MTProto** | Dùng Telethon + `api_id`/`api_hash` (my.telegram.org). Session file có toàn quyền tài khoản, nên coi như mật khẩu. |
| D3 | Mỗi worker cài **Claude Code + Codex CLI + Antigravity** | Hai CLI chạy headless được; Antigravity chỉ nhận việc thủ công. |
| D4 | Docker chạy trên **một máy khác**, không phải máy worker Ubuntu đã setup | Bootstrap worker coi Docker là tuỳ chọn. Lỗi Docker xử lý riêng. |
| D5 | **Model local Qwen 3.5 9B** (Ollama) trên máy gateway — ThinkPad 16 GB RAM — làm bộ phân loại tin nhắn và soạn nội dung ticket | Không tốn quota subscription, tin nhắn nội bộ không rời máy. Chạy CPU (ước lượng 8–15 token/giây cho model cỡ 8–9B), cần khoảng 6–7 GB RAM ở mức nén 4-bit. Context đặt 16k–32k (`OLLAMA_CONTEXT_LENGTH`), Ollama chỉ nghe `127.0.0.1`. Việc viết code vẫn dùng Claude Code/Codex trên subscription. Tag Ollama chính xác phải tra lại trên ollama.com/library trước khi cài. |
| D5 (bổ sung 2026-10-09) | Đã đo trên máy thật: **giữ Qwen 3.5 9B** | 5,7 giây/tin (yêu cầu ≤ 20 giây). Tiêu chí "≥ 8 token/giây" bị bỏ vì tốc độ viết bị giới hạn bởi băng thông RAM, còn agent chỉ cần một dòng JSON ngắn. |
| D6 | **Giai đoạn 1 (MVP) chỉ gồm Telegram Triage Agent chạy local**: nghe Telegram → phân loại bằng Qwen 3.5 9B → tạo GitHub issue | Máy worker, Claude Code/Codex/Antigravity và tầng phân loại 3 (`claude -p`) chuyển sang giai đoạn sau. MVP không dùng subscription. Hạ tầng MVP: ThinkPad gateway + VPS MTProxy. Kế hoạch: Setup Plan v1.0.2. |
| D7 | **Giai đoạn 1 chạy toàn bộ trên ThinkPad, kết nối Telegram trực tiếp**; tối ưu lên VPS sau | Kiểm tra 2026-10-08: mạng hiện tại không chặn Telegram. Bỏ VPS + MTProxy khỏi MVP (B1 hoãn). Lộ trình VPS (chỉ MTProxy / listener / cả model) ở Setup Plan §14. |
| D8 | Trong lúc thử nghiệm (B5–B7), agent tạo issue vào **repo riêng `tg-triage-sandbox`** (private) | Tránh issue rác trong repo sản phẩm. Repo sản phẩm vẫn chờ CTO chốt (Q2); chuyển bằng cách thêm repo vào token và đổi `GH_REPO`. |

### Điểm kỹ thuật quan trọng (ảnh hưởng thiết kế)
- **Bot Telegram không nhận được tin nhắn của bot khác trong group.** Nếu mỗi worker dùng bot riêng để nói chuyện thì các bot sẽ không "nghe" được nhau. Tài khoản người dùng (MTProto, D2) thì nhìn thấy mọi tin nhắn, kể cả của bot. Vì vậy **listener MTProto đóng vai "tổng đài"**: worker gửi bằng bot riêng, listener đọc hết và điều phối.
- **Chỉ một instance listener cho mỗi tài khoản.** Chạy cùng session trên nhiều máy sẽ tạo issue trùng và dễ bị Telegram đánh dấu bất thường. Một máy được chỉ định làm gateway.
- **Rủi ro khoá tài khoản Telegram**: userbot chỉ nên **đọc và tạo ticket**, không tự gửi tin hàng loạt từ tài khoản cá nhân. Tin nhắn gửi đi nên dùng bot.
- **Rủi ro subscription**: một tài khoản Claude/ChatGPT dùng chung cho nhiều máy chạy 24/7 có thể chạm giới hạn dùng hoặc trái điều khoản của nhà cung cấp. Cần kiểm tra trước Phase 4; nếu vướng thì phần chạy tự động chuyển sang API key.
- **Secrets**: `.env`, `*.session`, `secrets/` đã có trong `.gitignore`. Mọi token đọc từ env trên từng máy.
- **Harness**: danh tính bất biến vẫn là **AlexChu** (chủ dữ liệu). `worker_id` chỉ là danh tính runtime của máy, không thay owner của task history.

### Giả định
- A1 (*đã xác nhận 2026-10-09*): ~~Ubuntu 24.04 LTS~~ → máy gateway chạy **Ubuntu 26.04.1 LTS** (kernel 7.0). Mọi bước đã làm (Ollama, cấu hình, SSH, TLP) chạy bình thường trên bản này.
- A2 (*sửa 2026-10-08*): ~~Telegram bị chặn ở mạng hiện tại~~ → đã kiểm tra: **mạng hiện tại không chặn Telegram**. MTProxy chưa cần cho giai đoạn 1; vẫn có thể bị chặn về sau nên code giữ ô cấu hình proxy.
- A3 (*sửa 2026-10-08*): MTProxy **không** chạy được trên máy Docker nội bộ: proxy chỉ chuyển tiếp, đặt trong mạng đang bị chặn thì chính nó cũng không tới được Telegram. MTProxy chạy trên **VPS nước ngoài**. Lỗi Docker nội bộ (D4) vì vậy không còn chặn tiến độ.
- A4 (*đã đo 2026-10-09*): gateway là **ThinkPad E15 Gen 4**, 2 × 8 GB DDR4-3200 kênh đôi, không có GPU rời. Qwen 3.5 9B chạy CPU: 4,7–5,0 tokens/s, **5,7 giây/tin** khi gọi giống agent, còn 5,7 GiB RAM khả dụng. Chi tiết: Setup Plan §7 B4.

### Câu hỏi mở — cần trả lời trước khi duyệt
- **Q1**: Bao nhiêu máy, vai trò của từng máy (ví dụ: `gateway`, `fe-dev`, `be-dev`, `qa`)? Máy Ubuntu đã setup đảm nhận vai nào?
- **Q2**: Ticket tạo vào **repo GitHub nào** (ví dụ `vlink-nexora-fe`), có cần đưa lên project board không?
- **Q3**: Bộ nhãn phân loại: dùng `bug / feature / question / task` hay theo nhãn sẵn có của repo đích?
- **Q4** — *đã trả lời một phần bằng D5*: phân loại theo 3 tầng: **luật từ khoá** → **Qwen 3.5 9B local** → (tuỳ chọn) `claude -p` khi model local không chắc. Còn chờ: chấm 50 tin nhắn mẫu theo KPI ≥ 85% để xác nhận Qwen 3.5 9B đủ tốt, và chốt có bật tầng Claude hay không.
- **Q5**: Nghe **group/chat nào**? Có cần nghe tin nhắn riêng (DM) không?
- **Q6** — *đã trả lời (D7)*: chưa cần MTProxy; giai đoạn 1 kết nối thẳng, VPS là bước tối ưu sau.
- **Q7**: Lỗi Docker trên máy chủ Docker: cần log (`systemctl status docker`, `journalctl -u docker -n 50`) từ **chính máy đó**.

### Changelog chuẩn hóa

| Version | Ngày giờ (Asia/Saigon) | Tác giả | Thay đổi |
|---|---|---|---|
| v1.0.9 | 2026-10-15 21:12 | Codex / owner AlexChu | Bổ sung phạm vi user yêu cầu, nguồn nghiệp vụ và nghiên cứu; giữ Draft/sign-off gốc. |
