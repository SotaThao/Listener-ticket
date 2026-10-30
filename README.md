# Listener-ticket — Telegram triage → GitHub ticket

Bot đọc tin nhắn trong group Telegram của team (text, ảnh, caption, reply), hiểu hội thoại và:

- **tạo GitHub issue** cho việc cần làm (bug, task, feedback);
- **bổ sung** vào ticket đã có khi có Reply đúng nguồn;
- **hỏi lại** khi yêu cầu chưa rõ;
- **bỏ qua** chat thường, cảm ơn, câu hỏi thông tin.

> **Đây là bản sao chụp** từ repo `PersonalProjectJob/telegram-agent` (AgentBuilder) tại commit `d6e3ed8` (`main`, 2026-10-30). **Nguồn chính vẫn là repo đó** — sửa ở đó, repo này chưa nhận thay đổi ngược. Không mang lịch sử commit cũ.

## Bắt đầu đọc từ đâu

| Bạn là… | Đọc |
|---|---|
| Agent hoặc người tiếp nhận | [`listener/AGENTS.md`](listener/AGENTS.md) — tra vai, quyền, cổng bật trước khi làm bất cứ việc gì |
| Vận hành | [`listener/README.md`](listener/README.md) (config, dry-run, trace, reformat) · [`deploy/README.md`](deploy/README.md) |
| Nghiệp vụ | [Business DOCS — TG-01…TG-33](Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md) (nguồn nghiệp vụ duy nhất) · [Overview](Docs/00-master/telegram-triage-noissue_overview_261027_v1.0.0.md) · [Rulebook](Docs/01-product/telegram-triage-noissue_rulebook_261027_v1.0.0.md) · [Luồng nghiệp vụ](Docs/01-product/telegram-triage-noissue_business-flow_261028_v1.0.0.md) |
| Quy tắc quyết định (đích, DRAFT) | [`listener/rules/README.md`](listener/rules/README.md) |

## Cấu trúc

```
listener/                 code (src/tg_triage), prompts, templates/github-ticket, rules (DRAFT), tests
Docs/                     tài liệu Telegram triage (+ tài liệu Worker Agent Fleet cũ được trace tham chiếu)
scripts/                  policy checker, reformat ticket, smoke model (fake tools)
deploy/                   build_wheel.py, agentfleet_deploy.py (THINKPAD)
config/listener.example.toml, systemd/tg-triage.service, .env.example
```

## Kiểm tra

```bash
cd listener && PYTHONPATH=src python -m unittest discover -s tests -t .
python scripts/check-telegram-triage-policy.py
python scripts/test_telegram_triage_policy.py
python scripts/test_reformat_telegram_issue.py
```

Kết quả tại thời điểm tách: `Ran 1269 tests … OK (skipped=2)`, checker `PASS` (33 rule, 52 runtime file).

`deploy/test_deploy.py` **không** được mang sang: bộ test đó kiểm cả service NailHub (`nailhub/`), phần này ở lại repo gốc. Script deploy vẫn dùng được cho `--service listener`.

## Không commit

Session Telegram, file `.env`, config thật có token, database SQLite. Xem `.gitignore`.
