# Listener-ticket — Audit hạ tầng code trước khi làm bộ cài `install.exe`

**Version**: v1.0.0 · **Ngày**: 2026-10-30 · **Owner**: AlexChu · **Reviewer**: Claude (`/plan-devex-review`, mode DX POLISH) · **Base**: `main` `b2d051b`
**Trạng thái**: Audit xong. Chờ Owner chốt mục 8 trước khi agent sau bắt đầu code.

> **Cho agent tiếp nhận:** đọc theo thứ tự §1 (mục tiêu) → §4 (chặn cứng) → §7 (việc cần làm) → §8 (quyết định chờ Owner). Mọi phát hiện có `file:dòng` để kiểm lại. Quy tắc nghiệp vụ không đổi: [`listener/AGENTS.md`](../../listener/AGENTS.md), [Business DOCS](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md).

## 1. Mục tiêu đã chốt (Owner, 2026-10-30)

| Mục | Chốt |
|---|---|
| Cách làm | **A**: giữ code Python, đóng gói sẵn Python (Nuitka hoặc PyInstaller) + bộ cài (Inno Setup trên Windows). **Không** viết lại bằng C++/Go |
| Người cài | **Quản lý team không biết code**, máy Windows. Không biết terminal, biến môi trường, systemd. Bỏ cuộc nếu gặp lỗi không hiểu |
| Thời gian cài | **≤ 10 phút**, tính từ lúc chạy `install.exe` tới lúc thấy bot chạy |
| Khoảnh khắc chứng minh | **Ticket thử thật**: wizard nhờ người dùng gửi 1 tin mẫu vào group, bot tạo ticket vào repo họ chọn và hiện link |
| Repo làm việc | `SotaThao/Listener-ticket`. Nguồn chính của logic nghiệp vụ vẫn là `PersonalProjectJob/telegram-agent` (xem §8 D-8) |

## 2. Persona

```
NGƯỜI CÀI
Ai:       Quản lý / chủ group Telegram của team phần mềm
Bối cảnh: Muốn mọi bug, việc, góp ý trong group tự thành GitHub ticket
Chịu được: ~10 phút, ~6 bước; bỏ khi gặp màn hình đen hoặc lỗi tiếng Anh kỹ thuật
Mặc định: Có Telegram trên điện thoại, có tài khoản GitHub, máy Windows 10/11. Không có Python, Ollama, Linux
```

## 3. Trải nghiệm hôm nay (đi theo tài liệu thật)

> Tôi mở `README.md`. Bảng đầu tiên bảo tôi đọc `listener/AGENTS.md` và Business DOCS dài 2.000 dòng. Tôi chỉ muốn cài. Tôi kéo xuống mục "Kiểm tra" thì thấy `cd listener && PYTHONPATH=src python -m unittest…`. Máy tôi không có Python. Tôi mở `listener/README.md`, mục "Configuration and commands" nói config ở `/etc/tg-triage/listener.toml`, khóa bí mật đặt trong biến môi trường `TG_API_ID`, `TG_API_HASH`, `GH_TOKEN`. Tôi không biết `/etc` là gì trên Windows, không biết lấy `TG_API_HASH` ở đâu. `deploy/README.md` nói về `systemctl`, `/opt/agentfleet`, user `tglistener`. Tới đây là 15 phút và tôi chưa chạy được gì. Tôi đóng tab.

**TTHW hiện tại:** > 60 phút, và với persona này là **không hoàn thành được**.

## 4. Chặn cứng: không làm thì không có bộ cài Windows

| # | Chặn | Bằng chứng | Hệ quả nếu bỏ qua |
|---|---|---|---|
| B1 | Đường dẫn Linux cố định | `listener/src/tg_triage/__main__.py:39` mặc định `/etc/tg-triage/listener.toml`; `deploy/agentfleet_deploy.py:35-38` cố định `/opt/agentfleet`, `/etc`, `/var/lib`; `systemctl` ở `:231-321` | Bản Windows không tìm thấy config, không có cách cài/nâng cấp |
| B2 | Bí mật đọc từ biến môi trường | `config.py:240-263` đọc `TG_API_ID`, `TG_API_HASH`, `GH_TOKEN`, `GH_REPO` từ env (systemd `EnvironmentFile`) | Persona không đặt được env; nếu wizard ghi ra file text thì lộ token |
| B3 | Đăng nhập Telegram chỉ qua terminal | `telegram_io.py:68-74` dùng `getpass` cho mã OTP và mật khẩu 2 lớp | Không có terminal trong bộ cài ⇒ không đăng nhập được |
| B4 | **Khóa chạy một bản bị tắt trên Windows** | `runtime.py:40-44`: không import được `fcntl` thì `return self`, tức **không khóa gì**; `model_lock.py:127-142` cũng dùng `fcntl` | Hai bản chạy cùng lúc (cài lại, service + chạy tay) ⇒ **tạo ticket trùng** (vi phạm TG-24) |
| B5 | Phụ thuộc Ollama cục bộ | `provider.py:9-11`: gọi cloud lỗi thì chạy lại trên Ollama cục bộ | Persona không có Ollama ⇒ lỗi cloud thành lỗi khó hiểu thay vì "đang thử lại" |
| B6 | Không có service Windows | Chỉ có `systemd/tg-triage.service` | Bot tắt khi người dùng đăng xuất/khởi động lại |
| B7 | Template/prompt tìm theo `sys.prefix/share/tg-triage` | `ticket_templates.py:221-226`, `listener/pyproject.toml` data-files | Bản đóng gói (frozen) có `sys.prefix` khác ⇒ không thấy template ⇒ **thoát mã 2** (TG-33 fail-closed) |
| B8 | Nhóm Unix `tglistener` (**không chặn**, đã kiểm) | `provider.py:189` gọi `ai_transport.read_key` (`ai_transport.py:126`), hàm này đã bỏ qua kiểm nhóm khi `os.name == 'nt'`; `receipt.py:14-17`, `usage.py:105-116` cũng có nhánh `os.name != 'nt'` | Chỉ cần test file key trên Windows; quyền file Windows nên dựa vào kho bí mật (P1) thay vì mode `0600` |
| B9 | Quyền GitHub token chưa được kiểm trước | Ảnh upload lên release asset của repo (`attachments.py`, `release_tag`) | Token chỉ có quyền issues ⇒ ticket tạo được nhưng **ảnh lỗi**. Wizard phải kiểm quyền issues + contents trước |

## 5. Chấm điểm 8 trục DX

| Trục | Điểm | Vì sao | 10 điểm trông như thế nào |
|---|---|---|---|
| 1. Bắt đầu (cài + chạy lần đầu) | **1/10** | B1–B7; TTHW > 60 phút; cần Linux + Python + systemd + Ollama | `install.exe` → wizard 6 bước → link ticket thử trong ≤ 10 phút (§6) |
| 2. Giao diện CLI/config | **4/10** | CLI có `run`, `login`, `check-config`, `trace`, `export-csv` (`__main__.py:37`), mặc định hợp lý cho operator Linux; config TOML 9 bảng | Wizard cho persona; CLI giữ nguyên cho IT, thêm `--config` mặc định theo hệ điều hành |
| 3. Thông báo lỗi | **4/10** | Lỗi đúng và chặt nhưng viết cho dev: `"TG_API_ID: required positive integer"` (`config.py:252`), `"GH_TOKEN: required in live mode"` (`:261`) | Wizard dịch mọi `ConfigError` thành: chuyện gì xảy ra + vì sao + bấm gì để sửa, tiếng Việt |
| 4. Tài liệu | **3/10** | Tài liệu nghiệp vụ rất đầy đủ nhưng không có trang cài đặt cho người không code; README gốc trỏ vào tài liệu 2.000 dòng | `SETUP.md` 1 trang có ảnh từng bước wizard + mục "lỗi thường gặp" |
| 5. Nâng cấp | **5/10** Linux · **1/10** Windows | Linux có `agentfleet_deploy.py` với backup + test trên máy đích + rollback tự động; Windows không có gì | Bộ cài mới chạy đè: giữ DB và config, chạy preflight, lỗi thì tự quay về bản cũ |
| 6. Môi trường phát triển | **6/10** | 1.269 test chạy offline, chạy xanh trên Windows; checker policy; không CI trong repo mới | GitHub Actions chạy test + checker + build installer trên `windows-latest` mỗi PR |
| 7. Cộng đồng / tìm trợ giúp | **3/10** | Không có issue template, không có mục hỗ trợ | Nút "Gửi chẩn đoán" trong app (dùng `trace`, đã che bí mật) + issue template |
| 8. Đo trải nghiệm | **1/10** | Không có số liệu thời gian cài, tỉ lệ cài lỗi | Log cục bộ thời gian từng bước wizard; preflight ghi kết quả vào file |

**Tổng: 3/10.** Logic lõi tốt (test, an toàn ticket, template). Toàn bộ khoảng trống nằm ở lớp nền tảng và cài đặt.

## 6. Hành trình đích (≤ 10 phút)

| Bước | Người dùng làm | Thời gian | Điều kiện kỹ thuật |
|---|---|---|---|
| 1 | Tải và chạy `install.exe` (chữ ký số, không cần quyền admin nếu chọn cài cho 1 user) | 1 phút | Ký số để tránh cảnh báo SmartScreen (D-5) |
| 2 | **Telegram:** nhập số điện thoại → mã OTP → mật khẩu 2 lớp nếu có | 2 phút | B3: callback đăng nhập không dùng terminal; `api_id` theo D-1 |
| 3 | **Chọn group nguồn** từ danh sách group đã tham gia | 1 phút | Liệt kê dialog qua Telethon; chỉ group, không chat riêng |
| 4 | **GitHub:** hiện mã thiết bị → mở trang GitHub → chọn repo; wizard kiểm quyền issues + contents | 2 phút | Device flow (D-2); B9 |
| 5 | **Model:** dán API key cloud (mặc định) hoặc dùng Ollama nếu phát hiện có sẵn; bật đồng ý gửi ảnh/tài liệu | 1 phút | B5, D-3 |
| 6 | **Preflight tự động** (check-config, template, kết nối Telegram/GitHub/model, khóa một bản), rồi **gửi tin mẫu** vào group → thấy link **ticket thử** | 3 phút | `preflight.md` tự động hóa; ticket thử theo D-4 |

Mọi bước có nút "Quay lại" và giữ dữ liệu đã nhập. Lỗi ở bước nào hiện tại bước đó, kèm cách sửa.

## 7. Việc cần làm (cho agent tiếp nhận)

Thứ tự gợi ý; mỗi phase là một PR, chạy đủ test + checker trước khi mở PR.

| Phase | Việc | File chính | Xong khi |
|---|---|---|---|
| P1 Nền tảng | Lớp đường dẫn theo hệ điều hành (config, data, log) thay `/etc`, `/var/lib`; vẫn nhận `--config` và env cho IT | `__main__.py`, `config.py`, mới `platform_paths.py` | Test chạy được trên cả Linux + Windows với đường dẫn mặc định khác nhau |
| P1 | **Khóa một bản trên Windows** (`msvcrt.locking` hoặc named mutex) cho `runtime.py` và `model_lock.py` | `runtime.py`, `model_lock.py` | Test: bản thứ hai trên Windows ra `AlreadyRunning` (B4) |
| P1 | Kho bí mật: Windows DPAPI (ctypes `CryptProtectData`), Linux file `0600`; env vẫn ghi đè được | mới `secrets_store.py`, `config.py`, `provider.py:189` | Token không bao giờ nằm trong file text; test đọc/ghi/xoay vòng |
| P1 | Tìm template/prompt khi chạy bản đóng gói (`sys._MEIPASS` / thư mục cạnh exe) | `ticket_templates.py:221-226`, nơi nạp prompts | `check-config` của bản đóng gói thoát 0 (B7) |
| P2 Wizard | Giao diện thiết lập (đề xuất: trang web cục bộ bằng `http.server` stdlib, D-6), 6 bước §6 | mới `setup_wizard/` | Chạy được toàn bộ §6 với Telegram/GitHub giả (fake) trong test |
| P2 | Đăng nhập Telegram không qua terminal | `telegram_io.py:68-74` | Callback nhận OTP/2FA từ wizard; test với client giả |
| P2 | GitHub device flow + kiểm quyền repo | mới `github_auth.py` | Thiếu quyền contents ⇒ wizard báo rõ trước khi cài xong (B9) |
| P3 Service | Windows: Task Scheduler (chạy khi khởi động, tự khởi động lại) hoặc Windows Service (D-7); Linux: giữ systemd | mới `service/` | Khởi động lại máy, bot tự chạy; log ra file |
| P4 Đóng gói | Spec Nuitka/PyInstaller + script Inno Setup; gói templates, prompts, rules | mới `packaging/` | `install.exe` cài trên Windows sạch (VM không có Python) |
| P4 | Gỡ cài đặt + nâng cấp giữ DB/config, rollback khi preflight lỗi | `packaging/`, `deploy/` | Kịch bản nâng cấp v1 → v2 → lỗi → về v1 không mất dữ liệu |
| P5 Kiểm tra | Preflight tự động + ticket thử (D-4) + nút "Gửi chẩn đoán" (dùng `trace`, che bí mật) | `setup_wizard/`, `trace.py` | Persona hoàn thành §6 trong ≤ 10 phút trên máy thật |
| P5 | CI GitHub Actions: test + checker + build installer trên `windows-latest`, `ubuntu-latest` | mới `.github/workflows/` | PR đỏ khi test hoặc build lỗi |
| P5 | `SETUP.md` 1 trang cho persona + lỗi thường gặp; README gốc trỏ vào đó đầu tiên | `SETUP.md`, `README.md` | Người không code làm theo được mà không đọc tài liệu khác |

**Ràng buộc giữ nguyên:** không đổi logic nghiệp vụ TG-01…TG-33; mọi câu chữ gửi người dùng qua template (TG-33); không replay tin cũ (TG-32, xem D-4); test hiện có phải xanh; checker policy PASS.

## 8. Quyết định chờ Owner chốt

| # | Câu hỏi | Đề xuất | Vì sao |
|---|---|---|---|
| D-1 | `api_id`/`api_hash` Telegram: một bộ của ứng dụng dùng chung, hay mỗi người tự lấy ở my.telegram.org? | **Dùng chung** (đăng ký 1 lần cho app) | Tự lấy thêm ~5 phút và là bước persona dễ bỏ cuộc nhất. Cần chấp nhận rủi ro bộ dùng chung bị Telegram giới hạn |
| D-2 | GitHub: device flow (cần đăng ký 1 OAuth App/GitHub App) hay dán token thủ công? | **Device flow** | Không phải copy token; quyền được giới hạn ngay từ app |
| D-3 | Model mặc định cho persona: cloud (cần API key) hay Ollama cục bộ? Khi cloud lỗi mà không có Ollama thì làm gì? | **Cloud mặc định**; không có Ollama ⇒ giữ pending + thử lại, **không** rơi về Ollama | Persona không cài Ollama được (B5) |
| D-4 | Ticket thử lúc cài có vi phạm TG-32 ("không tạo issue thật để kiểm thử") không? | **Ngoại lệ có nhãn `listener-setup-test`**, chỉ cho tin mới người dùng gửi trong wizard, không replay tin cũ; ghi vào Business DOCS | Đây là khoảnh khắc chứng minh đã chốt; cần căn cứ nghiệp vụ |
| D-5 | Chứng chỉ ký số cho `install.exe` | **Có** (code signing) | Không ký ⇒ Windows SmartScreen chặn, persona tưởng là virus |
| D-6 | Giao diện wizard: trang web cục bộ (stdlib) hay cửa sổ desktop (tkinter / thư viện mới)? | **Web cục bộ** | Không thêm dependency, dễ test, dùng lại được cho Linux |
| D-7 | Windows: Task Scheduler hay Windows Service (`pywin32`, thêm dependency)? | **Task Scheduler** trước | Không cần quyền admin, không thêm dependency; đổi sau được |
| D-8 | Từ giờ phát triển ở repo nào? Bộ cài cần sửa code lõi (B1–B9) | **Chọn một nguồn chính** trước P1 | Sửa lõi ở hai repo ⇒ hai bản lệch nhau |
| D-9 | Nhóm xử lý (`rules/channels.md`, DRAFT) có vào wizard v1 không? | **Không**, v1 chỉ group nguồn | Code chưa triển khai; thêm sau khi rules ACTIVE |

## 9. Ghi chú quy trình

- Skill `/plan-devex-review` chạy dạng rút gọn: Bash trên máy audit hỏng, nên phần khởi động gstack, so sánh đối thủ bằng WebSearch và các bước hỏi theo từng phát hiện không chạy. Persona, thời gian mục tiêu, khoảnh khắc chứng minh và mức review đã hỏi Owner trực tiếp (§1). Các phát hiện còn lại được gom vào §8 thay vì hỏi từng câu, để giữ quota và để agent sau nhận đủ ngữ cảnh.
- Bằng chứng đọc từ repo này tại `b2d051b`; 1.269 test và checker đã chạy xanh trên Windows lúc tách repo.

## GSTACK REVIEW REPORT

| Run | Skill | Mode | Status | Findings |
|---|---|---|---|---|
| 1 | `/plan-devex-review` (rút gọn) | DX POLISH | DONE_WITH_CONCERNS | 9 chặn cứng (B1–B9), 8 trục chấm, tổng 3/10 |

VERDICT: Chưa sẵn sàng làm bộ cài. Cần P1 (nền tảng: đường dẫn, khóa một bản, kho bí mật, đường dẫn template khi đóng gói) trước mọi việc khác; B4 là rủi ro tạo ticket trùng.

**UNRESOLVED DECISIONS:**
- D-1 api_id Telegram dùng chung hay mỗi người tự lấy
- D-2 GitHub device flow hay token thủ công
- D-3 model mặc định và hành vi khi cloud lỗi
- D-4 ticket thử và TG-32
- D-5 chứng chỉ ký số
- D-6 công nghệ giao diện wizard
- D-7 Task Scheduler hay Windows Service
- D-8 repo nguồn chính
- D-9 group xử lý trong wizard v1
