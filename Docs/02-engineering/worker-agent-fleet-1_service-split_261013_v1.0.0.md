# Engineering: Tách tg-triage và NailHub thành 2 service độc lập

<!-- telegram-triage-historical -->
> **Trạng thái hiện hành riêng phần service split:** listener và NailHub đã được tách triển khai; quan sát chỉ đọc 2026-10-15 cho thấy cả hai service hoạt động độc lập. Draft/chưa code và chờ split phía dưới là trạng thái lịch sử. Xem [guarded deployment](../../deploy/README.md) và [listener operations](../../listener/README.md) cho đường dẫn/service hiện hành. Không coi ghi chú này là thay đổi nghiệp vụ NailHub.
> **Telegram triage — phần lịch sử/đề xuất:** mô tả nghiệp vụ triage trong tài liệu này không còn là nguồn hiện hành. Xem [TG-01..TG-32](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md) cho bản đã triển khai baseline `9746ef2`. Các kế hoạch Fleet/worker ngoài triage, sign-off, incident và số đo cũ được giữ nguyên; không coi đề xuất là chức năng đã chạy.


## Metadata
- **Last Updated**: 2026-10-15 21:15 (Asia/Saigon)

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.
- **Feature ID**: 1 (ticket nội bộ)
- **Created**: 2026-10-13
- **Version**: v1.0.2
- **Author**: AlexChu (Claude Code)
- **Status**: Draft, chờ duyệt. Chưa code.
- **Liên quan**: `worker-agent-fleet-1_assistant-definition_261012_v1.0.0.md` (§12.15), `nailhub-postings-noissue_engineering_261012_v1.0.0.md` (trong main checkout, chưa commit), quyết định `D-024`.

---

## 1. Bối cảnh và sự cố

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.

Hai feat đang chạy chung trên THINKPAD trong cùng một package `tg_triage`, cùng venv `/opt/agentfleet/listener/.venv` và cùng service `tg-triage`:

| Feat | Code | Nhánh |
|---|---|---|
| Listener (triage, template issue §12.15) | `tg_triage/*` | `feat/1_worker-agent-fleet`, `feat/1_issue-type-templates` |
| NailHub (đăng bài, lệnh của Owner) | `tg_triage/nailhub/`, `tg_triage/model_gate.py`, các hook trong `telegram_io.py` và `classifier.py`, extra `nailhub` trong `pyproject.toml` | `codex/nailhub-automation` và phần chưa commit trong main checkout |

**Sự cố ngày 2026-10-13:**
- **16:42**: deploy commit `48bb761` (template issue) bằng `pip install --force-reinstall`. Wheel không có NailHub nên lệnh này gỡ `nailhub/` và `model_gate.py` khỏi bản cài.
- **16:42 → 17:16**: journal không có dòng NailHub nào. NailHub ngừng khoảng 34 phút.
- **17:16**: phiên NailHub chạy `nailhub-model-policy-recovery-deploy.py`, cài lại `nailhub/`, `model_gate.py`, `telegram_io.py`.
- **Từ 17:16**: THINKPAD chạy một bản trộn, không ứng với commit nào. `classifier.py`, `pipeline.py`, `issue_format.py` lấy từ `48bb761`; phần NailHub lấy từ main checkout.

**Nguyên nhân gốc:** hai feat không có ranh giới deploy. Bước kiểm sau deploy của mỗi bên chỉ so với file của chính mình, nên không bên nào thấy mình vừa làm hỏng bên kia.

## 2. Mục tiêu

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.

1. Hai service systemd độc lập: `tg-triage.service` (listener) và `tg-nailhub.service` (NailHub). Restart hay deploy một bên không đụng file, venv hay process của bên kia.
2. Hai package Python, hai venv, hai script deploy. Mỗi script chỉ cài và kiểm package của nó, và **từ chối chạy** nếu venv đích chứa module không thuộc package đó.
3. Mỗi bên có nhánh và lịch sử riêng, bản cài luôn ứng với đúng một commit.

Không thuộc phạm vi: đổi logic triage, đổi logic đăng bài NailHub.

## 3. Thiết kế

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.

### 3.1 Code trong repo

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.

| Thành phần | Hiện tại | Sau khi tách |
|---|---|---|
| Listener | `listener/src/tg_triage/` | giữ nguyên, **bỏ** `nailhub/` và các hook NailHub trong `telegram_io.py` |
| NailHub | `listener/src/tg_triage/nailhub/` | package riêng `nailhub/src/nailhub/`, `pyproject.toml` riêng (`nailhub`, phụ thuộc `telethon`, `playwright`) |
| Model gate | `tg_triage/model_gate.py` (khoá trong process) | khoá **liên process**, xem §3.3. Mã nhỏ, mỗi package giữ một bản sao, cùng tên file khoá và cùng giao thức |
| Test | `listener/tests/test_nailhub_*` | chuyển sang `nailhub/tests/` |

NailHub không import `tg_triage`, listener không import `nailhub`. Có test kiểm tra điều này bằng cách quét import.

### 3.2 Telegram: session thứ hai (đã chốt)

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.

- NailHub chạy Telethon client riêng, session `/var/lib/agentfleet/nailhub/nailhub.session`, dùng cùng tài khoản và cùng `TG_API_ID`/`TG_API_HASH`.
- Owner đăng nhập một lần bằng lệnh tương tác (nhập mã Telegram) khi cài lần đầu. Agent không nhập mã thay.
- NailHub chỉ nhận **lệnh của Owner** (giữ nguyên `verify_owner`), không đọc tin nhóm để triage. Listener bỏ nhánh `route_command`.
- File mẫu `systemd/tg-triage-nailhub.conf.example` đang ghi *"Do NOT run a second Telegram process/session"*. Quyết định này **được thay** bởi D-024. Phải sửa file mẫu cho khớp.

### 3.3 Chia CPU cho model giữa hai process

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.

Hiện `MODEL_GATE` bảo đảm mỗi lúc chỉ một lời gọi Ollama, và triage được ưu tiên hơn NailHub. Khoá này chỉ có hiệu lực trong một process, nên khi tách phải thay bằng cơ chế khác:

- Khoá file `fcntl.flock` tại `/run/agentfleet/ollama.lock` (thư mục do `tmpfiles.d` tạo, nhóm chung `agentfleet`), giữ trong suốt một lời gọi model.
- Ưu tiên triage: listener tạo file `/run/agentfleet/ollama.foreground` khi có lời gọi đang chờ. NailHub (lời gọi nền) không lấy khoá khi file này còn, và kiểm lại sau mỗi 2 giây.
- Có test cho hai process thật (multiprocessing): triage không bao giờ phải chờ một lời gọi NailHub mới bắt đầu.

Lý do bắt buộc: `think = true` đã bật từ 17:14. Một lời gọi triage có thể mất tới 600 giây. Không có khoá thì hai process gọi Qwen song song trên CPU và cả hai cùng chậm.

### 3.4 Trên THINKPAD

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.

| | tg-triage | tg-nailhub |
|---|---|---|
| Service | `tg-triage.service` | `tg-nailhub.service` (mới) |
| User | `tglistener` | `tgnailhub` (mới), cùng nhóm `agentfleet` |
| Code + venv | `/opt/agentfleet/listener` | `/opt/agentfleet/nailhub` |
| Config | `/etc/tg-triage/listener.toml` | `/etc/nailhub/nailhub.toml` (chuyển từ `/etc/tg-triage/nailhub.toml`) |
| State | `/var/lib/agentfleet/tg` | `/var/lib/agentfleet/nailhub` (đã có: browsers, DB) |
| Backup | `/var/backups/agentfleet/listener-*` | `/var/backups/agentfleet/nailhub-*` |

### 3.5 Script deploy

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.

Mỗi bên có `deploy/<service>.py`, cùng một khuôn:
1. Kiểm wheel khớp mã nguồn của **commit đang deploy** (ghi hash commit vào kết quả).
2. Chạy test của package bằng venv đích.
3. **Chốt chặn:** liệt kê module trong venv đích. Có module không thuộc package hoặc dependency đã khai báo ⇒ dừng, không cài. Đây là chốt đã thiếu ngày 13/10.
4. Backup, stop **chỉ service của mình**, cài, kiểm hash, start.
5. Kiểm service của mình **và** service kia vẫn active, NRestarts không đổi.

### 3.6 Bổ sung sau review (v1.0.1, Owner chốt 2026-10-13)

> Phạm vi triage tại mục này là snapshot/đề xuất cũ: [quy tắc hiện hành](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md). Không áp lại gate phải tag, chỉ reaction, local-only, /question→issue, quota comment hoặc late-reply→comment từ snapshot này.

Lý do bổ sung: `nx-reviewer` trả CHANGES_NEEDED và `nx-verifier` trả FAIL cho bản code đầu tiên.

- **Backup và rollback** (thay bước 4 của §3.5):
  - Backup SQLite **sau khi** stop service.
  - Rollback tự động chỉ trả lại **code và config**. Chỉ restore DB khi lỗi xảy ra trước lúc start, hoặc khi Owner quyết định tay.
  - Thay đổi ở service kia (MainPID, NRestarts) **chỉ được báo cáo**, không kích hoạt rollback service đích.
  - Sau khi start, kiểm `ActiveState=active` và `NRestarts=0`. Không so với giá trị trước khi stop, vì stop/start thủ công đặt lại bộ đếm này.
- **Chốt chặn** kiểm thêm: file có trong RECORD của bản đang cài nhưng **không có trong wheel mới**. Đây đúng là cơ chế của sự cố 16:42, khi pip gỡ chính những file đó.
- **NailHub không xác minh được Owner lúc khởi động:** thoát với exit 1 để systemd thử lại. Khi sẵn sàng thì log dòng `NailHub ready owner_verified=1`, và script deploy chờ dòng này (timeout 60 giây).
- **Tin `/nailhub …` trong nhóm:** listener bỏ qua theo cấu hình `ignored_command_prefixes` trong `listener.toml`. Code listener vẫn không nhắc tới NailHub.
- **Thứ tự cutover:**
  1. `/nailhub pause` và xác nhận NailHub đã dừng.
  2. Gỡ drop-in `NAILHUB_CONFIG` khỏi `tg-triage`, chạy `daemon-reload`, rồi restart `tg-triage`.
  3. Xác nhận journal **không còn dòng NailHub nào** và triage vẫn chạy.
  4. Chuyển config, `chown` thư mục, cài và start `tg-nailhub`.

  Không có thời điểm nào hai process cùng chạy NailHub, và triage không bao giờ bị sập trong quá trình này.

## 4. Thứ tự thực hiện

| Bước | Việc | Ai |
|---|---|---|
| 0 | Phiên NailHub commit toàn bộ code đang chạy trên THINKPAD vào `codex/nailhub-automation`. Bản cài hiện tại là bản trộn, nên commit phải ứng với đúng phần NailHub đang chạy | phiên NailHub |
| 1 | Duyệt doc này | Owner |
| 2 | Nhánh listener: bỏ hook NailHub, thêm khoá liên process, script deploy có chốt chặn. Test | `/dispatch` |
| 3 | Nhánh NailHub: tách thành package `nailhub`, Telethon client riêng, khoá liên process, unit `tg-nailhub.service`, script deploy. Test | phiên NailHub hoặc `/dispatch` |
| 4 | THINKPAD: tạo user/thư mục, cài `tg-nailhub`, Owner đăng nhập session 2, start, kiểm NailHub nhận lệnh và gửi notice | Owner chạy, agent soạn script |
| 5 | THINKPAD: deploy listener bản không NailHub, kiểm cả hai service | Owner chạy, agent soạn script |
| 6 | Theo dõi 24 giờ: không có lời gọi model chồng nhau, cả hai service đều không restart ngoài ý muốn | agent đọc journal |

**Rollback:** mỗi bước 4–5 có backup riêng. Nếu bước 4 lỗi, giữ nguyên bản trộn đang chạy. Nếu bước 5 lỗi, cài lại bản listener trong backup và dừng `tg-nailhub`.

**Trong lúc chờ tách:** không deploy listener lên THINKPAD nữa. Mọi deploy đều có nguy cơ gỡ NailHub.

## 5. Rủi ro và việc chưa chốt

- **Hai session cùng tài khoản:** Telegram cho phép, nhưng cả hai đều nhận update của mọi nhóm. NailHub phải bỏ qua mọi tin không phải lệnh của Owner. Cần test.
- **Phần NailHub sửa trong `classifier.py`** (bọc `MODEL_GATE` quanh lời gọi Ollama) đang không có trong bản cài, vì recovery 17:16 không cài lại `classifier.py`. Nghĩa là **hiện triage không đi qua gate**, và triage với NailHub có thể gọi Ollama cùng lúc. Bước 2 sẽ xử lý.
- **Tin 201** (case đến hạn 17:19) chạy với `think = true` đến 17:26 vẫn chưa xong. Cần đo latency thật trước khi chốt giới hạn timeout và cơ chế ưu tiên ở §3.3.
- User mới `tgnailhub` hay dùng chung `tglistener`: đề xuất tách user, chờ Owner chốt ở bước 4.

---

## Changelog
| Version | Date | Time | Author | Changes |
|---------|------|------|--------|---------|
| v1.0.0 | 2026-10-13 | 17:40 | AlexChu (Claude Code) | Bản đầu: sự cố 13/10, thiết kế 2 service, khoá model liên process, session Telethon thứ 2, thứ tự thực hiện |
| v1.0.1 | 2026-10-13 | — | AlexChu (Claude Code) | §3.6 sau review: backup sau stop, rollback chỉ code+config, kiểm NRestarts=0 sau start, chốt chặn thêm file bị gỡ khỏi RECORD, NailHub exit 1 khi Owner chưa xác minh + dòng ready, `ignored_command_prefixes`, thứ tự cutover không làm sập triage |

## Changelog chuẩn hóa Telegram triage

| Version | Ngày giờ (Asia/Saigon) | Tác giả | Thay đổi |
|---|---|---|---|
| v1.0.2 | 2026-10-15 21:12 | Codex / owner AlexChu | Gắn nhãn snapshot/đề xuất, thêm đối chiếu nguồn TG-01..TG-32; giữ lịch sử và sign-off. |
