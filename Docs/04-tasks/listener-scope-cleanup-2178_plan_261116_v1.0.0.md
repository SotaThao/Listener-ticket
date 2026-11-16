# Kế hoạch gỡ đợt 2 và đợt 3: đưa repo về đúng tính năng log ticket

**Ngày:** 2026-11-16 · **Owner:** AlexChu · **Trạng thái:** Đợt 1 + 2 đã code, review, mở PR (chưa merge, chưa deploy); đợt 3 chưa bắt đầu · **Refs:** vlink-group/vlink-nexora#2178
**Nguyên tắc Owner:** repo `Listener-ticket` chỉ chứa tính năng log ticket Telegram Triage; phần còn lại thuộc repo khác.

---

## 0. Bàn giao — đọc mục này trước (cập nhật 2026-11-16)

Agent tiếp nhận: đọc hết mục 0, rồi `listener/AGENTS.md` (vai, quyền, cổng GO). Mục 1 trở xuống là kế hoạch gốc và quyết định Owner, giữ nguyên làm lịch sử.

### 0.1 Đã xong

| Đợt | Việc | Commit · nhánh | PR |
|---|---|---|---|
| 1 | Gỡ hợp đồng AgentFleet console (`receipt.py`, `console_revision`, file `agentfleet-usage.json`) và 7 docs `worker-agent-fleet-1_*`. Business DOCS v1.16.37, inventory 61 → 60 | `5127df9` · `feature/2178_remove-console-fleet-docs` | [SotaThao/Listener-ticket#5](https://github.com/SotaThao/Listener-ticket/pull/5) → `main` |
| 2 (AgentBuilder) | Package `agentfleet-common` 1.0.0 (`agentfleet_common`, `PROTOCOL = 1`) trong `common/`: `ai_transport`, `model_lock` (chép nguyên văn từ `nailhub/`, chỉ đổi dòng docstring), 22 test, `build_wheel.py --service common` | `02d1604` · `feature/2178_agentfleet-common` | [PersonalProjectJob/telegram-agent#171](https://github.com/PersonalProjectJob/telegram-agent/pull/171) → `main` |
| 2 (Listener) | Đổi 12 import sang `agentfleet_common`, xóa 2 bản sao, pin `agentfleet-common==1.0.0`, `listener/tests/test_packaging.py` kiểm version/`PROTOCOL`/hash file đã cài. Business DOCS v1.16.38, inventory 60 → 58 | `b113b3a` · `feature/2178_agentfleet-common` | [SotaThao/Listener-ticket#6](https://github.com/SotaThao/Listener-ticket/pull/6) → nhánh của #5 (xếp chồng) |

Bằng chứng đợt 2: full suite Listener `Ran 2217 tests … OK (skipped=2)` (= 2231 − 16 test chuyển/bỏ + 2 mới, `nx-verifier` đối chiếu từng tên); policy checker PASS 33 rule / 58 file; package 22 test OK; `nx-reviewer` GATE 0b **APPROVED** sau 2 vòng sửa. Executor: Codex `gpt-6.1-sol` · `high`.

### 0.2 Việc còn lại, theo thứ tự

| # | Việc | Ai | Ghi chú |
|---|---|---|---|
| 1 | Review + merge **#171 → #5 → #6** đúng thứ tự | Owner | #6 xếp chồng trên #5; merge #5 xong GitHub tự đổi base #6 về `main`. Không repo nào có CI cho các nhánh này — không có check tự động |
| 2 | Build wheel `agentfleet-common` từ **commit merge của #171**: `python deploy/build_wheel.py --service common --commit <sha> --stage <dir>` (trong AgentBuilder) | Owner/operator | Không build từ worktree chưa commit |
| 3 | Đặt wheel vào `/var/cache/agentfleet/wheels` trên gw-01 và `pip install --no-index --find-links /var/cache/agentfleet/wheels agentfleet-common==1.0.0` vào **venv listener** (không cài vào venv NailHub — guard của NailHub sẽ từ chối) | Owner (sudo) | Phải làm **trước** deploy listener; deploy cài listener bằng `--no-deps` |
| 4 | Precheck DB live trước khi stop bot, như sau sự cố 12/11 (chạy gate khởi động của bản mới trên bản sao DB live, giả lập hết hạn topic OPEN) | Agent soạn, Owner chạy sudo | Chưa tự động hóa trong deploy — xem 0.4 |
| 5 | Phiếu preflight mới trong `listener/rules/preflight.md` cho release gồm #5 + #6 (P1–P13, Owner ký GO) | Agent soạn, Owner ký | Phiếu cuối trong repo là `064e1c8` (06/11); các bản cài sau đó chưa có phiếu |
| 6 | Deploy listener với `--allow-removed` cho `tg_triage/receipt.py`, `tg_triage/ai_transport.py`, `tg_triage/model_lock.py` | Owner | Staged test `test_packaging` chạy trong venv đích trước khi stop: lệch version/hash package ⇒ deploy dừng, bot không bị đụng |
| 7 | Sau deploy: kiểm journal (PID mới, `NRestarts=0`, dòng `rules fingerprint=`, 0 `ERROR`/`Traceback`/`RulesHalt`) | Agent đọc, Owner chạy | |
| 8 | NailHub (và NailBlog) chuyển sang `agentfleet-common`, bỏ bản sao riêng | Repo AgentBuilder | Trước khi chuyển, `common/tests/test_parity.py` giữ hai bên cùng giao thức khóa. Đổi `PROTOCOL` thì phải nâng mọi service cùng lượt |
| 9 | Đợt 3: gỡ engine window + autonomous | Agent qua `/dispatch` (GATE 0b) | Kế hoạch mục "Đợt 3" + kết quả đo bước 3.0 + quyết định Q2–Q4 ở cuối file. Bắt đầu từ `main` sau khi #6 merge |

**Rollback sau deploy đợt 2:** ưu tiên `agentfleet_deploy.py --service listener --rollback <backup của lần deploy đó>`. Chỉ khi deploy lại một release cũ hơn thì mới `pip uninstall agentfleet-common` khỏi venv listener, ngay trước lệnh deploy; nếu deploy đó tự rollback về bản mới thì cài lại package trước khi tin vào unit (chi tiết: `listener/README.md`).

### 0.3 Câu hỏi còn mở (cần Owner/operator trả lời, đừng đoán)

- **Production build listener từ repo nào?** README deploy trỏ `/srv/agentfleet-split`. AgentBuilder vẫn còn thư mục `listener/` cũ (có bản sao `ai_transport`/`model_lock` cũ). Nếu gw-01 build listener từ AgentBuilder thì #5/#6 không bao giờ tới production. Kiểm `git -C /srv/agentfleet-split remote -v` trên gw-01 trước bước 6.
- **`deploy/` và `systemd/` trong repo này** là hạ tầng AgentFleet (deploy nhiều service). AgentBuilder cũng có `deploy/` + `systemd/`. Hai bản có thể lệch; Owner quyết bản nào là nguồn, rồi gỡ bản kia (chưa nằm trong đợt nào).
- **`legacy_reconciliation.py`** gỡ được khi DB live không còn dữ liệu engine cũ cần chuyển — cần đọc DB gw-01 (chỉ đọc, bản sao).
- **PR #5, #6 chưa gán assignee**: `PersonalProjectJob` không phải collaborator của `SotaThao/Listener-ticket`, GitHub bỏ qua lệnh gán.

### 0.4 Việc từ các audit ngày 16/11 chưa làm

Các audit hạ tầng (DX 4/10) và cấu trúc thư mục (6/10) ngày 16/11 đề xuất thêm các việc dưới đây. Theo ranh giới repo, phần lớn thuộc **AgentBuilder** (hạ tầng), chỉ mục đầu thuộc repo này:

| Việc | Thuộc | Ghi chú |
|---|---|---|
| Subcommand `tg_triage precheck` (gate khởi động trên bản sao DB live, giả lập topic hết hạn; phase hiện tại chặn deploy, phase hết hạn chỉ cảnh báo) | Listener | Dùng lại `check_before_recovery` + `expire_due`; chạy thêm `check-config` của stage |
| Deploy gọi precheck trước `stop`; gate chạy lại trên backup sau `stop` | AgentBuilder `deploy/` | Phân loại kết quả theo JSON stdout, không theo exit code (argparse cũng trả 2) |
| Timer kiểm gate mỗi giờ trên code đang cài | AgentBuilder `systemd/` | Listener không gửi cảnh báo này (Owner: listener chỉ log ticket) |
| Cảnh báo khi bot chết (`OnFailure=`), gửi qua Telethon + MTProxy vào group ops, chống trùng | AgentBuilder `systemd/` | Bot API trực tiếp bị chặn trên gw-01 (chỉ đi được qua MTProxy) |
| Hướng dẫn cài máy mới, lỗi `check-config` tách "không có file/không có quyền/TOML sai", help cho subcommand, config mẫu rules mode | Listener + AgentBuilder | |
| Đổi tên test theo hành vi (`test_review_round2/3`, `test_round3_settings`, `test_review_fixes`) | Listener | |

### 0.5 Bẫy đã gặp — đọc trước khi làm tiếp

- **Push vào `SotaThao/Listener-ticket`** cần tài khoản `SotaThao`; git mặc định dùng `PersonalProjectJob` (chỉ đọc) và bị 403. Dùng token của `gh auth token --user SotaThao` cho riêng lệnh push/`gh pr`.
- **Không có nhánh `test`** ở cả hai repo; PR `feature/*` vào `main` (khác bảng mặc định của rule commit-pr).
- **Test `deploy.test_deploy.WheelTests.test_real_suites_from_real_staged_layout` của AgentBuilder lỗi sẵn** trên `origin/main` sạch khi checkout Windows `core.autocrlf=true` (fixture template thành CRLF). Không do các PR này gây ra.
- **Codex `gpt-6.1-sol` không có tool `update_plan`** → BLOCKED ngay lượt đầu nếu prompt bắt buộc. Cho phép in kế hoạch đánh số + `STEP n DONE` trong tin nhắn; thanh `%` của `codex-progress.py` sẽ không chạy. Đề xuất đổi rule đã gửi `harness/inbox/codex-261116-update-plan-unavailable.md` (trùng với `codex-261104-update-plan-fallback.md`).
- **Ghi file Docs có tiếng Việt bằng PowerShell/console redirect làm mất dấu** (thành `?`), policy checker không bắt. Ghi bằng Python `encoding='utf-8'`, rồi đếm `?` trong dòng vừa sửa.
- **Heredoc không quote mà chứa backtick** trong bash sẽ chạy nội dung backtick như lệnh (đã làm mất chữ trong một commit message). Ghi message ra file bằng tool Write rồi `git commit -F`.
- **Business DOCS trace**: mọi sửa file trong `listener/src`, `listener/prompts`, `listener/templates` phải làm mới hash trong `runtime_inventory` và `runtime_fingerprint` (thuật toán ghi ngay trong block), giữ định dạng `json.dumps(indent=2, ensure_ascii=False)`. Checker hiện cứng `TG-01…TG-33` — đợt 3 phải sửa checker trước khi gỡ rule.
- **State vận hành** của đợt 2 nằm ở task harness `T-261116-dot-2-tach-ai-transport-model-lock-thanh` trong `.agent-tasks/` của checkout chính (`node <skill-repo-vlink>/harness/bin/harness-task.mjs show <id>`), trạng thái `todo` kèm ghi chú bàn giao.

---

## Đợt 2 — Tách code dùng chung thành package riêng

### 2.1 Hiện trạng đã kiểm

| File | Có ở đâu (bản giống hệt, so SHA-256 sau chuẩn hóa CRLF) | Kết luận |
|---|---|---|
| `ai_transport.py` (244 dòng) | listener, `AgentBuilder/nailhub`, `AgentBuilder-nailhub-slice1/nailhub` — cùng `3534ca68…` | **Dùng chung**, tách |
| `model_lock.py` (254 dòng) | listener, nailhub, **nailblog** (worktree `nailblog-automation-review`) — cùng `52a02869…` | **Dùng chung**, tách |
| `thinking_transport.py` (185 dòng) | **Chỉ listener** | **Không dùng chung** — ở lại repo này. Đính chính audit trước |

- Ba bản sao được giữ đồng bộ bằng tay. Listener tự kiểm qua `tests/test_model_lock.py::test_copies_are_byte_identical`, nhưng test đó so với `nailhub/src/nailhub/model_lock.py` — đường dẫn không tồn tại trong repo này.
- API listener dùng: `ai_transport` → `ProviderError, ENDPOINTS, PROVIDERS, PinnedHTTPS, LIMIT, IMAGE_URL, complete, read_key, redact, usage_of, validate_endpoint`; `model_lock` → `MODEL_LOCK, LockUnavailable, LockStopped`.
- Cả hai module chỉ dùng thư viện chuẩn, không import ngược vào `tg_triage` — tách được sạch.
- `model_lock` là **giao thức giữa các tiến trình** (khóa file trong `/run/agentfleet`): listener và NailHub chạy cùng máy phải dùng **cùng phiên bản giao thức** tại mọi thời điểm.

### 2.2 Thiết kế

```
repo agentfleet-common (mới, hoặc thư mục trong AgentBuilder — Owner chọn, xem Q1)
└── src/agentfleet_common/
    ├── ai_transport.py      (chuyển nguyên văn từ listener)
    ├── model_lock.py        (chuyển nguyên văn)
    └── __init__.py          (__version__ = "1.0.0", PROTOCOL = 1)
    tests/ (test_model_lock nhiều tiến trình + test ai_transport chuyển từ listener)

listener/pyproject.toml
  dependencies = ["telethon>=1.45,<2", "agentfleet-common==1.0.0"]   ← pin chính xác

listener/src/tg_triage/
  provider.py, classifier.py, config.py, document_reader.py, thinking_transport.py:
      from .ai_transport import X   →   from agentfleet_common.ai_transport import X
      from .model_lock import X     →   from agentfleet_common.model_lock import X
  ai_transport.py, model_lock.py   →   XÓA
```

**Phân phối:** máy chạy cài offline (`pip install --no-index --find-links /var/cache/agentfleet/wheels`), deploy cài wheel listener với `--no-deps`. Vì vậy wheel `agentfleet_common-1.0.0` phải nằm trong cache và được cài vào venv **trước** deploy listener. Deploy guard (`guard_environment`) đã kiểm phụ thuộc khai báo có mặt trong venv, nên thiếu là dừng trước khi stop bot.

### 2.3 Thứ tự triển khai

```
B1  Tạo package agentfleet-common 1.0.0 = bản sao hiện tại, byte-identical      (repo khác)
B2  Build wheel, đặt vào /var/cache/agentfleet/wheels trên gw-01                (Owner/operator)
B3  Listener: đổi import, khai báo dependency, xóa 2 file, sửa test             (repo này)
B4  Cài agentfleet_common vào venv listener → deploy listener (phiếu GO mới)      (gw-01)
B5  NailHub, NailBlog chuyển sang package khi tiện; trong lúc chờ, bản sao       (repo khác)
    của chúng vẫn byte-identical với package 1.0.0 nên giao thức khóa tương thích
B6  Từ đây mọi đổi ai_transport/model_lock = bump package; đổi PROTOCOL thì
    phải nâng mọi service cùng lượt
```

### 2.4 Việc trong repo này

| # | Việc | File |
|---|---|---|
| 2.4.1 | Đổi 12 chỗ import sang `agentfleet_common` | `provider.py`, `classifier.py`, `config.py`, `document_reader.py`, `thinking_transport.py` |
| 2.4.2 | Xóa `ai_transport.py`, `model_lock.py` | `listener/src/tg_triage/` |
| 2.4.3 | Khai báo dependency pin chính xác | `listener/pyproject.toml` |
| 2.4.4 | Chuyển test thuần của hai module sang package; ở lại listener chỉ test tích hợp (provider gọi đúng `complete`, classifier giữ khóa). Đổi `patch('tg_triage.ai_transport…')` sang đường dẫn mới | `listener/tests/test_model_lock.py`, `test_usage_fallback.py` và các test patch transport |
| 2.4.5 | Test mới: phiên bản `agentfleet_common` cài đặt khớp pin, `PROTOCOL == 1` | `listener/tests/test_packaging.py` |
| 2.4.6 | Môi trường test offline: thêm package vào `PYTHONPATH` hoặc venv; ghi vào README phần "Offline verification" | `listener/README.md` |
| 2.4.7 | Business DOCS: inventory 60 → 58, trace TG-30 (provider) trỏ symbol mới nếu có; changelog | `Docs/01-product/…business…md` |

### 2.5 Rủi ro đợt 2

| Rủi ro | Xử lý |
|---|---|
| Deploy lên gw-01 khi wheel chưa có trong cache | Deploy guard dừng trước `stop` (đã có); thêm vào checklist preflight |
| Package và bản sao NailHub lệch nhau về sau | Package là nguồn duy nhất; NailHub chuyển ở B5; trước đó cấm sửa bản sao |
| Deploy guard thấy 2 file biến mất khỏi RECORD | `--allow-removed tg_triage/ai_transport.py --allow-removed tg_triage/model_lock.py` |
| Test offline không còn chạy bằng `PYTHONPATH=src` đơn thuần | 2.4.6 |

**Ước lượng đợt 2:** repo này ~1 ngày (CC ~1–2 giờ); package mới ~0,5 ngày; deploy + preflight theo lịch Owner.

---

## Đợt 3 — Gỡ engine intake cũ (window + autonomous)

### 3.1 Hiện trạng đã kiểm

`pipeline.py:327-376` chọn engine theo cờ; production chạy `decision_rules_enabled`:

```
Pipeline._process / close_due_cases
  ├─ decision_rules_enabled ─► rules_intake.process / sweep          ← GIỮ (production)
  ├─ autonomous_enabled     ─► autonomous.process / resume_case      ← GỠ
  └─ còn lại                ─► window: assign_case → _analyze_case   ← GỠ
                               + followups.sweep (followup kiểu cũ)  ← GỠ
```

**Rules mode không độc lập** — nó đang dùng các phần sau của engine cũ (đếm từ import thật):

| Rules mode dùng | Từ module | Xử lý |
|---|---|---|
| `Plan`, `Pipeline._run`, `_images`, `_combine`, `_acknowledge`, `_prepared_images`, `attachments`, `notice_sender`, `github` | `pipeline.py` | **Giữ** — đây là bộ thực thi ghi GitHub/ảnh dùng chung; chỉ bỏ nhánh intake |
| `_enrichment_comment`, `_source_body`, `_recurrence_body` | `followups.py` | **Chuyển** sang module của rules (vd `rules_comments.py`) |
| `projection`, `render`, `render_excerpts`, `explicit_task`, `valid_task`, `TASK_SCHEMA` | `analysis_task.py` | Giữ (phục vụ ANALYZE trong rules) — xem Q3 |
| `sent_notice_identity` | `ticket_status.py` | Giữ (STATUS trong rules) — xem Q3 |
| `FOLLOWUP_SCHEMA` | `followup_model.py` | Chuyển hằng sang `decision_rules.py` |
| `strip_thinking` | `classifier.py` | Chuyển sang module tiện ích; `classifier` còn dùng cho gọi model |
| `decision_turn`, `CURRENT`, `ContextJSON`, `LOCAL_REQUEST_BUDGET_BYTES`, … | `thinking.py` | **Giữ** — rules mode dùng nhiều |
| `autonomous_read_context`, `autonomy_case` | `autonomy_store.py` (mixin của `Store`) | Giữ 2 hàm này (chuyển vào store của rules), bỏ phần còn lại |
| `TopicStoreMixin`, `FollowupStoreMixin` | mixin của `Store` | Bỏ phần ghi; **bảng trong DB production vẫn còn** |
| Bảng `cases`, `groups` | `store.py` | **Giữ** — rules mode còn ghi; gate khởi động còn kiểm |

**Hai ràng buộc lớn:**

1. **Business DOCS: 23/33 rule TG lấy bằng chứng code từ engine cũ** (`pipeline._safety`, `autonomous.process`, `followups._plan`, `followup_store.*`, `autonomy_store.*`, …). Gỡ engine cũ ⇒ phải truy vết lại 23 rule sang code rules mode. Nếu rule nào **không có code tương ứng trong rules mode** thì đó là khoảng trống nghiệp vụ thật, phải Owner quyết (giữ rule, bỏ rule hay bổ sung code) — không được im lặng xóa.
2. **Gate khởi động** (`check_before_recovery` → `legacy_preflight.REGISTRY`) kiểm mọi bảng của engine cũ trong DB production. Bảng cũ vẫn nằm trong DB sau khi gỡ code, nên `legacy_preflight` và schema validation **phải giữ**.

Quy mô test: `pipeline` được 50 file test import, `classifier` 35, `followups` 7, `autonomous` 2.

### 3.2 Các bước

| Bước | Việc | Điều kiện qua |
|---|---|---|
| **3.0 Đo** | Chạy riêng các test rules mode bằng `trace`/`sys.settrace` (stdlib, không thêm dependency) để có danh sách hàm **thật sự** chạy trong rules mode. Đối chiếu với 23 rule TG | Bảng: mỗi rule TG → symbol rules mode tương ứng hoặc "KHÔNG CÓ" |
| **3.1 Owner duyệt** | Owner duyệt bảng 3.0, quyết các rule "KHÔNG CÓ" và Q2–Q4 | Câu trả lời trích nguyên văn |
| **3.2 Tách hàm mượn** | Chuyển `_enrichment_comment`, `_source_body`, `_recurrence_body`, `FOLLOWUP_SCHEMA`, `strip_thinking`, `autonomous_read_context`, `autonomy_case` vào module của rules. **Không đổi hành vi**, chỉ di chuyển | Full suite xanh, diff chỉ là move + import |
| **3.3 Truy vết lại** | Sửa trace block Business DOCS: 23 rule trỏ symbol rules mode; policy checker PASS | Checker PASS, reviewer người đọc lại 23 dòng |
| **3.4 Gỡ engine** | Xóa `autonomous.py`, phần intake window trong `pipeline.py`, `followups.sweep` và phần followup kiểu cũ, phần ghi của `topic_store`/`followup_store`/`autonomy_store`; xóa/viết lại test tương ứng | Full suite xanh; số test giảm có giải thích từng file |
| **3.5 Cờ config** | `decision_rules_enabled`, `autonomous_enabled`, `thinking.enabled` vẫn được **đọc** để config trên gw-01 không lỗi; giá trị khác `true` ⇒ `ConfigError` rõ nghĩa ("rules mode là chế độ duy nhất"). Cập nhật config mẫu | `check-config` với config live (bản sao) PASS |
| **3.6 Deploy** | Phiếu preflight mới; `--allow-removed` cho từng file xóa; precheck DB live trước khi stop | GO của Owner |

### 3.3 Không đụng trong đợt 3

| Giữ lại | Lý do |
|---|---|
| `legacy_preflight.py`, `offline_snapshot.py` | Gate khởi động dùng |
| `legacy_reconciliation.py` | Gỡ được khi xác nhận DB live không còn dữ liệu cần chuyển (cần đọc DB gw-01) — để đợt sau |
| Bảng cũ trong SQLite | Không migration xóa bảng; dữ liệu là lịch sử ticket |
| `thinking.py`, `thinking_transport.py`, `classifier.py` (phần gọi model) | Rules mode dùng |

**Ước lượng đợt 3:** ~4–6 ngày người (CC ~1 ngày) + thời gian Owner duyệt 3.1. Đụng code production ⇒ đi `/dispatch` (GATE 0b), reviewer độc lập.

**Kết quả kỳ vọng:** `src/` giảm thêm ~2.500–3.000 dòng; còn **một** engine log ticket; mất đường lui "tắt rules mode để quay về hành vi cũ" (rollback khi đó là deploy lại release trước).

---

## Quyết định của Owner (2026-11-16)

| # | Câu hỏi | Trả lời Owner |
|---|---|---|
| Q1 | Package dùng chung nằm ở đâu? | “Trong AgentBuilder” — thư mục `common/` trong `PersonalProjectJob/telegram-agent` (AgentBuilder), cạnh `nailhub/`, `nailblog/`. Listener: `SotaThao/Listener-ticket` |
| Q1b | Đợt 2 bắt đầu thế nào? | Owner commit đợt 1 trước; đợt 2 trên nhánh mới `feature/2178_agentfleet-common` |
| Q1c | Executor đợt 2? | Codex `gpt-6.1-sol` · `high`; GATE 0b ⇒ `nx-reviewer` review độc lập |
| Q2 | Bỏ đường lui về engine cũ? | “chấp nhận về engine cũ đã sai và lỗi thời” — bỏ |
| Q3 | ANALYZE/STATUS thuộc tính năng log ticket? | “là tính năng log ticket” — giữ, không phải ứng viên gỡ |
| Q4 | Rule TG nào ở bước 3.0 ra "KHÔNG CÓ" trong rules mode: giữ, bỏ hay bổ sung code? | “các quy tắc theo engine cũ cứ loại bỏ và note lại vào DOCS để bổ sung vào rules modes sau” |

### Kết quả bước 3.0 (đo 2026-11-16)

Phương pháp: chạy 7 file test rules mode (`test_ops_only_regression`, `test_request_budget`, `test_rules_analysis`, `test_rules_bootstrap`, `test_rules_extensions`, `test_rules_intake`, `test_source_followup`: 794 test, 0 lỗi) với `sys.setprofile`, ghi mọi hàm `tg_triage` được gọi **khi một module `rules_*` đang trên call stack** (274 symbol). Đối chiếu với trace code của 23 rule đang trỏ vào engine cũ, rồi đọc nội dung các rule không có bằng chứng để tìm code rules mode tương ứng. Giới hạn: hàm chạy ngoài call stack rules (entry point như `telegram_io`, `ProcessLock`) không được ghi, nên các dòng A′ cần xác nhận lại khi viết trace mới.

| Loại | Rule | Ý nghĩa | Việc ở bước 3.3 |
|---|---|---|---|
| **A** — trace đã có bằng chứng rules mode | TG-05, 06, 08, 12, 13, 14, 15, 18, 19, 24, 27, 30, 32 | Rules mode làm; chỉ bỏ dòng trace trỏ engine cũ | Bỏ ref engine cũ, giữ/thay test rules mode |
| **A (một phần)** | TG-09 | ANALYZE/STATUS chạy (`analysis_task`, `ticket_status`); **`analysis_choices` (chọn giữa các phương án phân tích) không chạy** trong rules mode | Giữ rule; phần `analysis_choices` vào backlog |
| **A′** — rules mode có làm nhưng trace chưa trỏ | TG-07 (`facts.md` · `command_intent`, `intent_resolve`), TG-10 (`command_intent`, `chat_only` F4), TG-16 (`RulesPendingStore.bind_reply`, `topic_for_source_reply`), TG-17 (kiểm `issue_url` khớp `gh_repo`, `rules_intake.py:1723`), TG-25 (`rules_intake.sweep`, `ProcessLock`) | Không mất tính năng | Viết trace mới + test rules mode chứng minh; xác nhận từng dòng |
| **B** — chỉ engine cũ làm | TG-20 Xác nhận ticket 1/2 · TG-21 Tách ticket · TG-22 Nguồn chưa gán · TG-23 Sửa sau khi tách | Cả cụm "tách một ticket đã tạo thành nhiều ticket" của followup engine cũ. Không thấy trong `rules/` hay `rules_*.py`. Production rules mode từ 04/11 **thực tế đã không có tính năng này** | Theo Q4: gỡ khỏi rule hiện hành, chuyển nguyên văn vào Backlog rules mode |

### Cách áp Q4 vào Business DOCS (bước 3.3)

- Rule loại A (rules mode có làm): giữ heading `TG-xx`, đổi trace sang symbol rules mode.
- Rule loại B (chỉ engine cũ làm): bỏ khỏi bảng quyết định hiện hành và khỏi trace; nội dung chuyển nguyên văn sang mục mới **"Backlog rules mode — quy tắc của engine cũ chờ bổ sung"**, mỗi dòng ghi mã TG cũ, nội dung, symbol engine cũ đã thực hiện, ngày gỡ. Mã TG **không tái sử dụng**.
- Rule chỉ mất một phần (ví dụ rules mode làm một nửa): ghi phần còn lại vào backlog, phần đã có giữ trong rule.
- `scripts/check-telegram-triage-policy.py` đang cứng `TG-01…TG-33` (`expected = {f'TG-{n:02}' for n in range(1, 34)}`): đổi thành đọc danh sách rule hiện hành + danh sách backlog từ trace block, kèm test âm trong `scripts/test_telegram_triage_policy.py`.

## Thứ tự tổng

```
Đợt 1 ✅ PR #5 ──► Đợt 2 ✅ PR #171 (AgentBuilder) + PR #6 ──► merge 171 → 5 → 6 ──► wheel + venv + preflight + deploy (mục 0.2)
Đợt 3: 3.0 đo ──► 3.1 Owner ──► 3.2 move ──► 3.3 trace ──► 3.4 gỡ ──► 3.5 cờ ──► 3.6 deploy
Đợt 2 và đợt 3 độc lập về file (trừ classifier.py: đợt 2 đổi import, đợt 3 chuyển strip_thinking)
⇒ làm đợt 2 trước để tránh xung đột.
```
