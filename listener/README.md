# Telegram Triage Agent

**Documentation version**: v1.8.0 · **Updated**: 2026-10-21 (Asia/Saigon) · **Owner**: AlexChu

Runtime/operator guide. The single business authority is [Telegram triage TG-01..TG-33](../Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md); this README does not define a second policy. Historical normalization baseline was `9746ef2`; the subsequent THINKPAD runtime release was `6e2c913`. Autonomous/topic/lifecycle changes below require pinned release and enablement evidence, recorded separately from source review. Release evidence distinguishes a commit-bound build from a reviewed worktree snapshot; ancestry alone does not identify deployed bytes. Default source configuration remains dry-run; actual installed mode depends on operator configuration.

Agents taking over triage or GitHub ticket logging start with [AGENTS.md](AGENTS.md).

## Offline verification (Python >= 3.11)

From `listener/`, PowerShell:

```powershell
$env:PYTHONPATH="src"
python -m unittest discover -s tests -t . -v
python -m compileall -q src tests
```

Tests use standard-library unittest, temporary SQLite files and injected HTTP
openers. They do not need packages, sockets, tokens, sessions or a local model.

## Configuration and commands

Use `config/listener.example.toml` as the TOML template. Store deployment config
at `/etc/tg-triage/listener.toml`, root:root mode 644. Do not place it inside the
root-only `/etc/agentfleet` directory. Supply `TG_API_ID`, `TG_API_HASH` through
the environment; live mode additionally requires `GH_TOKEN` and `GH_REPO`.
`TG_TRIAGE_MODE`, if non-empty, overrides TOML; an empty value is ignored.
MTProxy credentials come only from `TG_PROXY_SECRET`, required when proxy host
is configured. A proxy secret key in TOML is rejected. The CLI does not load dotenv.
`check-config` prints only the lengths of credential fields.

```text
python -m tg_triage check-config --config /etc/tg-triage/listener.toml
python -m tg_triage login --config /etc/tg-triage/listener.toml
python -m tg_triage run --config /etc/tg-triage/listener.toml
python -m tg_triage export-csv --config /etc/tg-triage/listener.toml --out /var/lib/agentfleet/tg/review.csv
python -m tg_triage export-csv --db /var/lib/agentfleet/tg/triage.sqlite3 --out /var/lib/agentfleet/tg/review.csv
```

For a dry-run, keep `mode = "dry-run"` or set `TG_TRIAGE_MODE=dry-run`.
Dry-run records classification and decisions; it never initializes or calls the
GitHub client. The owner CSV has a UTF-8 BOM and blank review columns. Formula
prefixes in cells are neutralized with an apostrophe; the file has mode 0600.
CSV export reads only the database path from TOML or `--db`; it does not load
Telegram/GitHub credentials and opens SQLite read-only without recovery or
schema changes. A missing database exits with code 2 without creating paths.

On the ThinkPad, the provisioned environment must have the declared Telegram
dependency and the package (or `listener/src` on PYTHONPATH). The supplied systemd
unit assumes `/opt/agentfleet/listener/.venv/bin/python` can import `tg_triage`.
The prompts (`classify.md`, `case.md`, `topics.md`, `followup.md`) are loaded from `listener/prompts/` in a
source checkout or from `share/tg-triage/prompts/` under the installed Python prefix.
`run` loads `topics.md` at startup and exits if it is missing, so a bad install is not
discovered window by window. Keep the prompts fixed while processing messages.

Run interactive login as `tglistener` before starting the service; phone, OTP
and 2FA prompts do not echo input. The session file is set to mode 600. The
service never prompts for login. It requires a user account, ignores outgoing
and private messages, and processes only configured group IDs.

Ensure `/var/lib/agentfleet/tg` is owned by `tglistener` with mode 700, and
`/etc/agentfleet/tg-triage.env` is root-owned mode 600. Both session and database
contain sensitive data. SQLite files are set to mode 600. Run a single service
instance per database. On Linux, run/login reject data directories unless they
have mode 0700 and belong to the current user. The CLI sets umask 0077. Run and login hold
a non-blocking flock on `tg-triage.lock` in the database directory, acquired
before recovery; a contended lock exits with code 3. Windows skips this OS lock.
CSV export does not perform recovery or change message decisions.

## Provider configuration and telemetry

Business/consent/reasoning boundaries: [TG-30..TG-32](../Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md#tg-30--provider-và-privacy-reasoning). Provider fields below are technical examples, not the installed production profile.

```toml
[ollama]                   # server address and the "not sure" threshold
url = "http://127.0.0.1:11434"
confidence_threshold = 0.7

[model]
provider = "ollama"        # or a hosted provider, see below
model = "qwen3.5:9b"
think = true               # turn the model's reasoning on (Ollama "think": true)
timeout_seconds = 600      # reasoning is slow on CPU; measure on the real host before fixing the value
```

- **Provider layer** (`tg_triage/provider.py`): the pipeline talks to a provider with two calls,
  `classify_topics(transcript)` and `analyze_case(messages, forced_type=None)`, both returning the same
  records as before; code validates and decides everything. `OllamaProvider` (built on `classifier.py`) is
  the first one. `classify_topics` accepts the window's messages or an already built transcript string.
- **`provider = "claude"` is not supported yet:** it is a `ConfigError` ("unsupported provider"), as are unrecognized values outside the supported provider list. When it is built, e-mail and phone numbers must be redacted
  before text leaves the machine.
- **Old layout:** `model` and `timeout_seconds` under `[ollama]` are still read when `[model]` does not set
  them; `[model]` wins. Without any timeout the default is 600 seconds. No new dependency.
- **Hosted provider, images, backup, usage and price** (`provider = "deepseek" | "openai" | "anthropic" |
  "compatible"`, see `config/listener.example.toml`). Hosted calls need `cloud_consent = true`; e-mail and phone
  numbers in the text are redacted before it leaves the host.
  - **Images:** a hosted OpenAI-compatible provider (deepseek / openai / compatible) receives images only when
    `cloud_consent` AND `cloud_image_consent` are both `true` (default `false`). They are sent as
    `data:<mime>;base64,...` parts of a user message (mime sniffed from the bytes: jpeg, png or webp; any other
    type, or an `anthropic` provider, never leaves the host). A request with images may be up to 20 MiB; a text-only
    request stays limited to 128 KiB. Without the second consent the case is judged by local Ollama vision.
  - **Backup:** `[model] fallback = "ollama"` (default for a hosted provider; `"none"` keeps the old refusal),
    `fallback_model` (default `qwen3.5:9b`), `fallback_think` (default `true`); the Ollama address and timeout come
    from `[ollama]` / `[model] timeout_seconds`. When the hosted call fails (any provider error, broken JSON, an
    unusable answer after the usual retry, missing key or consent) the very same request runs on local Ollama with
    its local vision. If that fails too the answer is "none" as before (needs-human). The pipeline never sees the
    switch.
  - **Usage:** every model call, errors and fallbacks included, adds one row to the SQLite table `model_usage`
    (`ts`, `provider`, `model`, `role` = `primary` | `fallback`, `outcome` = `ok` or an error code such as
    `rate_limited`, `timeout`, `invalid_json`, `invalid_output`, token counts, `cache_hit_tokens`, `latency_ms`,
    `cost`). Rows are kept 90 days. Message text, images, prompts and keys are never stored or logged.
  - **Price:** `[model.pricing]` (per 1M tokens at peak): `currency` (`USD`), `input_cache_hit` (`0.006`),
    `input_cache_miss` (`0.3`), `output` (`1.2`), `offpeak_factor` (`0.5`), `peak_hours_utc`
    (`["01:00-04:00", "06:00-10:00"]`, Monday-Friday UTC; everything else is off-peak). The defaults are
    deepseek-flash (api-docs.deepseek.com/quick_start/pricing, measured 2026-10-15); openai / anthropic / compatible
    have no built-in price and cost 0 until set. `cost = (cache_hit * hit + (prompt - cache_hit) * miss +
    completion * output) / 1e6`, times `offpeak_factor` off-peak. Local Ollama costs 0.
  - **Console summary:** `agentfleet-usage.json` (schema 1, camelCase) is written next to the database, atomically
    (temp, fsync, replace; mode 0640 and group `tglistener` off Windows) at startup and after a model call, at most
    once per 30 seconds. It holds `today` / `7d` / `30d` windows (local calendar days of the server, with a
    per-model breakdown) and 30 `daily` lines (oldest first, empty days as zero lines).
- **Reasoning stays private:** with `think = true` Ollama returns the reasoning in `message.thinking` next to
  the JSON in `message.content`. Only `content` is parsed (a leading `<think>...</think>` block left in
  `content` is stripped first). The reasoning is never read, logged, stored in SQLite or put in an issue.
- **What the model returns** (topics and single case alike): `category` in `{bug, task, feedback, chat}`,
  `type` (kept for compatibility), `actionable` (bool), `confidence`, `title`, `summary`, `priority`,
  `missing_info` (list) and `reason` (a short why). `verdict` is optional: when absent it is derived
  (`chat` = noise, a ticket category = ticket, or needs-human when `actionable` is false).
- **`reason`** is stored for the reviewer only, in `messages.model_reason` (a new column added with a safe
  `ALTER`; the older `reason` column keeps the code's own reason such as `auto_needs_human`) and exported in the
  `model_reason` column of `export-csv`. It is bounded to 300 characters and is never put in an issue, a log
  line or a GitHub call. The model is told not to put e-mail or phone numbers in it.

## Listening scope and reactions (Engineering Doc B.16)

`[telegram] listen_all_groups` (boolean, default `false`). When `false`, only the
non-empty `chat_ids` list is listened to. When `true`, every basic group and
supergroup the account is in is listened to (private chats and broadcast channels
are ignored, as are outgoing messages), `chat_ids` must be empty or omitted
(otherwise `ConfigError`), and startup replay of queued messages covers all chats.
Enabling it stores content of every joined group in SQLite; decide deliberately.


Text replies and reactions are defined by [TG-11..TG-13](../Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md); live mode can send success, clarification, error and quota notices through the existing Telegram client.

## Delivery and failure behavior

Incoming content is committed as `queued` before metadata lookups and queueing.
The worker transitions it to `classifying`. Restart replays queued/classifying
records from stored content, retaining sender and public chat metadata. Each
classification increments `attempts`; startup quarantines queued/classifying
records with attempts >= 2 as error for human review. Invalid UTF-8 ingress
surrogates become U+FFFD; invalid model strings are rejected with a bounded retry.
Unexpected per-message failures are quarantined without classification fields
and the worker continues with the next message. Completed
keys are skipped. Only immediately before calling GitHub does it become `pending`.
The hourly issue quota counts reserved issue-creation attempts (including sibling creates and timeout/5xx outcomes; comments do not consume it)
and timestamps in the future if the clock moves backwards.
GitHub errors are recorded without retry. Old pending records are marked error
on startup and require manual review against their
`<!-- tg:chat:message -->` issue marker before any manual retry. This prevents
automatic duplicate creation after an ambiguous request outcome; it does not
promise delivery of every issue after a crash.
Errors after a durable GitHub reservation warn that an issue may already exist
and require a repository check before any manual retry, including errors while
writing the result to SQLite. Errors before reservation retain the exception
class without blaming GitHub. Failure quarantine clears classification fields.

A single queue worker runs synchronous pipeline work in a thread. SIGTERM and
SIGINT stop taking further queue entries. Incoming messages during shutdown are
still stored as queued without entering the memory queue. The handler remains
registered until Telegram disconnects. The
service waits up to 120 seconds for the active worker, then disconnects Telegram.
Remaining queued content stays durable for restart. If the active call exceeds
the deadline, the service disconnects and exits the process without closing
SQLite under that thread; queued/classifying records replay and pending records
require review on next startup. Systemd allows 150 seconds before forced stop.
Terminal disconnects exit nonzero for systemd restart. Configuration errors,
missing login sessions and bot accounts exit 2, which systemd does not restart automatically.
Startup calls `get_me()` directly: only a missing user means missing login;
RPC and network errors exit 1 so systemd can restart the service.
Queue backlogs and an alive heartbeat are logged without content. Exception
diagnostics include only class names. Transport warnings forward logger name,
severity and exception class, never the library's original message or traceback.
HTTP bodies, headers and raw exception text are never logged. Labels are mapped
exclusively from the fixed code allowlist.
User/model fields disable GitHub references, links and Markdown images; the
defense covers bare `www.` links regardless of case. The
code-generated Telegram source link stays clickable. CSV formula defenses also
cover leading whitespace and full-width formula characters.

Reconnect reference: [Telethon client documentation](https://docs.telethon.dev/en/stable/modules/client.html). The adapter checks the installed client signature before constructing it. Integration evidence is specific to the measured host/version; unit tests do not establish AI accuracy or uptime SLA.

## Storage and operator recovery APIs

For storage locations, table/key mapping, read-only SQL, failure diagnosis and recovery evidence, use the canonical [memory and debug procedure](../Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md#bộ-nhớ-truy-vấn-và-debug-vận-hành). Confirm the active host/config before using deployment-specific paths.

See [TG-16..TG-27](../Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md) for trusted routing, binding, split, ambiguity and recovery eligibility. `cases`, `groups` and `reconciliation` preserve original accounting; additive `followup_roots`, `followup_jobs`, `followup_operations` and `followup_analysis_retries` hold conversation state and auditable operations. Main SQLite contains original text and context; usage/log privacy does not mean the main database is content-free.

`Store.ensure_followup_job(chat_id, message_id)` is the explicit, single-message legacy seed API. `Store.retry_followup_analysis(chat_id, message_id, configured_repo, expected_notice_id)` is the narrowly guarded terminal-analysis retry API. Neither is a global replay command. Read the eligibility and no-POST-retry restrictions in the canonical rules before operator use. Preserve backups, original IDs and external-operation evidence; never reset original cases to recover uncertain GitHub outcomes.

Profile lookup is performed by `make_public_profile_address_resolver` at notice send/edit time; caches remain transient. See [TG-28..TG-29](../Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md) for permitted evidence and fallback.

## Media configuration and packaging

`[aggregation]` controls `window_minutes`, `max_messages_per_case`, `max_messages_per_window`. `[attachments]` controls `max_bytes`, `max_per_case`, `max_documents_per_case`, `max_per_hour`; use the checked [example TOML](../config/listener.example.toml) for current defaults. Image and document counters are separate per-case limits and share upload quota. Uploaded evidence belongs to the configured repository: verify its access before enabling media.

Allowlisted images are signature-checked JPG/PNG/WEBP; document metadata is checked by `attachments.document_extension`. The pipeline downloads into private temporary directories, reuses prepared images for initial vision/upload and deletes temporary files after processing. `gh release upload` uses environment credentials and generated asset names. Followup scope planning reuses stored media provenance; only a new reply photo is prepared for multimodal enrichment, without re-reading old pixels. Detailed behavior/limits live in [TG-14..TG-15/TG-22](../Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md).

## Topic memory and read-only trace

The [canonical foundation/lifecycle contract](../Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md#nâng-cấp-harness-phạm-vi-foundation-và-quy-tắc-đích) defines authority and scope. This release adds opt-in autonomous semantic routing, batched followups and closed-ticket lifecycle. Original intake history remains separate from the validated `triage_current_topics` partition; partial siblings and unresolved membership are explicit. Legacy window routing remains available when autonomous is disabled.

```toml
[thinking]
enabled = true
topic_memory_enabled = false
autonomous_enabled = false
```

`topic_memory_enabled` defaults to false. When explicitly enabled, normal `run` initializes additive `triage_topics`, `triage_topic_messages`, `triage_topic_events` and `triage_topic_hydration` after acquiring the process lock. Startup hydrates up to 100 historical cases using a persisted cursor; subsequent starts continue the cursor. Current touched cases project through existing writable persistence hooks. Turning the flag off stops projection and preserves stored history. It does not reset decisions/reservations or remove tables. Historical hydration is incremental; absence from the registry can mean not yet hydrated, not absence from originals. Per-case projection bounds originals (1000), roots (20) and members per root (1000); respect `membership_truncated` rather than treating the projection as exhaustive history.

After installing this version, query local storage without model calls, Telegram login, GitHub access or recovery/migrations:

```bash
sudo -u tglistener /opt/agentfleet/listener/.venv/bin/python -m tg_triage trace \
  --config /etc/tg-triage/listener.toml \
  --repo PersonalProjectJob/telegram-agent --issue 47

sudo -u tglistener /opt/agentfleet/listener/.venv/bin/python -m tg_triage trace \
  --config /etc/tg-triage/listener.toml \
  --repo PersonalProjectJob/telegram-agent --chat-id -5264814171 --message-id 454
```

Repository is explicit `--repo` or `GH_REPO`; a message lookup requires both chat and message IDs. Default `--limit 100` is bounded (1–1000); inspect truncation/missing sections before concluding context is complete. JSON output defaults to metadata. `--include-text` deliberately opts into sensitive local source content; protect/redact it before sharing. Trace reports older schemas without creating missing tables. It reads stored observations, not current remote GitHub/Telegram state. SQLite `mode=ro` prevents logical writes; WAL/shared-memory auxiliary files may still be affected by SQLite or concurrent writers. Use a consistent backup for strict forensic immutability; do not blindly use `immutable=1` against live WAL. Use the canonical debug procedure for verified external readback and repair.

## Documentation drift check

From repository root:

```text
python scripts/check-telegram-triage-policy.py
python scripts/test_telegram_triage_policy.py
```

The checker reads the one trace block in the canonical policy, verifies source/test symbols and Git-LF-normalized runtime/prompt hashes, and flags `REVIEW_REQUIRED` for drift. It never loads policy into the daemon, calls a model or mutates a service. A hash match proves reviewed bytes, not semantic correctness.

For isolated THINKPAD deployment and rollback, see [deploy guide](../deploy/README.md). The [THINKING engineering/research](../Docs/02-engineering/telegram-triage-noissue_thinking-research_261015_v1.0.0.md) distinguishes implemented controller behavior from future pilot targets; [QA acceptance](../Docs/03-qa/telegram-triage-noissue_qa_261015_v1.0.0.md) documents proof criteria.

`[thinking] enabled=true` is the default when absent. Set false and restart the listener to restore the legacy semantic path. Additive `thinking_decisions` and `thinking_decision_events` hold versioned decision metadata; preserve them and the existing external-operation ledger during code rollback. Do not reset reservations to retry uncertain POSTs. The daemon loads packaged `thinking-topics.md`, not the canonical Markdown policy. Model/consent/auth settings remain unchanged.

To run a small synthetic check against the configured model while using memory SQLite and fake GitHub/Telegram tools:

```text
python scripts/telegram-thinking-model-smoke.py --config /etc/tg-triage/listener.toml
```

Run from repository root with the listener venv/user and the reviewed installed wheel or source on PYTHONPATH. This opt-in command uses model resources under existing consent. It does not open the live database or Telegram session, and prints only outcome metadata. It is not a model-accuracy benchmark. Semantic deadlines and current limitations are defined in TG-19/TG-30/TG-32, avoiding a second policy source here.

For reviewed image-caption fixtures, select `--case image_caption_context --image-path /path/to/reviewed.jpg`; optional `--sources-fixture /path/to/ignored-sources.json` supplies the three source texts/metadata. The JPEG and source export remain local/ignored. The report includes image SHA/transport byte counts and sanitized generated wording so semantic grounding can be reviewed. No real upload, issue, notice or live DB is used. See the QA document for fixed acceptance labels.

Technical vision-partition failures can create `context_pending` notice records for unclassified source components. These are not ticket-root authority. Exact sent-notice/pending-source proof routes a reply into fresh intake; see TG-12/TG-16. Preserve original case/group accounting and do not reset reservations to force a retry.

For the reviewed POS thread and reply photo, use `--case pos_print_context --sources-fixture /path/to/reviewed-sources.json --image-path /path/to/454.jpg`. The smoke uses the daemon startup factory and checks generated environment/error fields, one same-ticket refresh and the exact one-USER image binding. It uses fake tools and never replays historical production ingress.

## Reformat an existing ticket

Mẫu ticket/comment/notice được đóng gói tại `listener/templates/github-ticket/`. Đổi mẫu: sửa → chạy test → PR → release → chạy `check-config` trên bản cài → so sánh fingerprint với preview reformat. Mẫu lỗi làm `run` và `check-config` thoát mã 2; không có override hoặc fallback. Dùng reformat từ cùng release với daemon.

Use `python scripts/reformat-telegram-issue.py --input /path/to/reviewed-source.json --output /path/to/preview.json`
from repository root. Input contains `category`, `title`, `summary`, `sources` (IncomingMessage fields with
timezone-aware ISO `received_at`), `details`, and optional `images`/`documents` URL pairs. Only include
reviewed sources belonging to that ticket. The tool imports the same `format_issue` used by the daemon,
always enables the category template, and marks provenance as manual confirmation.

Review the preview, then append `--apply --repo owner/repo --issue NUMBER --expected-body-sha256 SHA`
where SHA is the UTF-8 SHA-256 of the current GitHub body. The tool validates the existing source marker,
uses GET/PATCH on that exact issue, preserves unrelated labels, and verifies the result. It cannot create
a ticket or send a Telegram reply. Read back after a failed/uncertain PATCH before retrying. Source binding
repairs require a separate reviewed maintenance operation; this command does not alter SQLite or source history.
Never write a replacement body by hand for reprocessing: supply reviewed structured fields to this renderer.

Offline checks: `python scripts/test_reformat_telegram_issue.py`.

## Changelog

| Version | Date/time (Asia/Saigon) | Author | Change |
|---|---|---|---|
| v1.7.0 | 2026-10-20 | Codex / owner AlexChu | Per-batch technical feedback, strict task diagnostics, current-text priority across review/repair and cloud redaction before excerpting; pinned worktree provenance recorded separately. |
| v1.6.0 | 2026-10-16 15:10 | Codex / owner AlexChu | Source-scoped ingress vision and bounded semantic review for incident grouping; model/release proof required separately. |
| v1.5.0 | 2026-10-16 14:43 | Codex / owner AlexChu | Record the approved DeepSeek release choice and unsuccessful isolated Gemini evaluation; preserve production provider and context safeguards. |
| v1.4.0 | 2026-10-16 12:16 | Codex / owner AlexChu | Document 30/60-second conversation pacing, late-context retrieval and durable clarification suppression. |
| v1.3.0 | 2026-10-16 11:31 | Codex / owner AlexChu | Autonomous activation/isolated model smoke and current topic/episode tracing; business DOCS remains canonical. |
| v1.2.0 | 2026-10-16 10:13 | Codex / owner AlexChu | Opt-in durable topic projection and local read-only trace operations; deployment evidence separate. |
| v1.1.1 | 2026-10-16 09:57 | Codex / owner AlexChu | Link storage and debug operations to the canonical business procedure. |
| v1.1.0 | 2026-10-15 21:47 | Codex / owner AlexChu | THINKING feature switch, additive journal operations and isolated configured-model smoke. |
| v1.0.0 | 2026-10-15 21:12 | Codex / owner AlexChu | Keep technical operations; replace evolving duplicated business descriptions with canonical rule links. |

### Installed provider decision — 2026-10-16

The Owner approved retaining DeepSeek after the isolated Gemini Free-tier trial. Preserve the existing installed DeepSeek model, endpoint, scoped credentials, consent and fallback settings during this release. The local example profile remains Ollama; it is not the THINKPAD production profile.

Gemini 3.8 Flash access was available, but the project returned HTTP 429 for its daily Free-tier request quota. Gemini 3.7 Flash returned HTTP 503 under load; one successful response through the existing JSON-object path also had the wrong top-level structure and was rejected. An isolated JSON Schema experiment passed one grouping case, but the complete seven-case experiment did not pass. These results do not establish Gemini's semantic accuracy, and the experimental transport is not part of this release. A future provider switch requires complete context regressions against the actual proposed transport and installed verification first. Never interpret a listed model or one successful probe as release readiness.

The DeepSeek baseline for unchanged runtime `416390d` passed all seven configured-model scenarios: three messages to one ticket, three independent tickets, image/caption #46, POS evidence #47, closed-ticket recurrence, login with competing history, and a pending reply after 90 seconds. Test side effects were isolated from GitHub, Telegram and the live database. Each subsequent deployment must record its own commit-bound package and installed health evidence; the provider decision does not change the 30/60-second collection contract or late-context rules.

### Autonomous operations

Enable `[thinking] enabled=true`, `topic_memory_enabled=true`, `autonomous_enabled=true` only on a tested installed release. Semantic ingress processes bounded same-chat queued sources and trusted candidates after per-chat30s quiet /60s collection cap; the cap is not a total processing SLA. Shared turn budget is two model attempts/40 seconds/six reads. One followup batch carries all new captions/images in one USER packet. Trace exposes immutable routes, cached plans, current partition, batch membership and recurrence episodes; see the canonical policy for lifecycle and recovery rules.

Configured-model isolated regression (fake GitHub/Telegram, in-memory SQLite, supplied reviewed image fixtures):

```text
python scripts/telegram-autonomous-model-smoke.py --config /etc/tg-triage/listener.toml --case all --image46 /private/442-1.jpg --image47 /private/454.jpg
```

This uses the actual startup provider and configured consent; it does not open the live database/session or send real group messages. The five-case corpus is a regression check, not an accuracy estimate. Packet saturation and candidate bounds can cause a visible clarification rather than a guessed ticket.

### Conversation pacing

In autonomous mode, `[thinking] context_quiet_seconds=30` and `context_max_wait_seconds=60` are defaults; allowed integers satisfy30 <= quiet <= max <=60. Each chat collects independently, then processes sealed batches sequentially. No early model/clarification for each arriving message. Context retrieval still applies to messages arriving after60 seconds; the collection deadline does not expire stored topic/reply/question history. Pending clarification reservations suppress normalized repeats across restart. See canonical Business DOCS for late-context and technical fallback behavior.

Each fresh invalid batch may send one technical notice at its persisted host anchor, even while an earlier chat outage remains open. Same-anchor reserved/failed/unknown deliveries and restarts remain at-most-once. This changes neither semantic topic membership nor authorization to replay old plans. Technical feedback confirms storage/failure briefly without asking the user to describe the same report again.

For repeated generic questions, inspect message reason and the decision journal's model_calls_used first: zero calls/context_budget_exhausted is a packing failure, not proof of semantic uncertainty. Trace packing metadata exposes optional candidates omitted and text-byte bounds; original sources remain in SQLite. The new clarification table contains conversation text and needs the same private database protection; default trace exports metadata only.

### Debug: one collected batch became several wrong tickets

Check installed `agentfleet.json`, config/provider and service PID first, then compare `ingress_candidates.source_ids`, `ingress_packing`, cached `autonomous_plans` and final ingress results. A shared batch means collection succeeded; it does not prove model topic grouping or pixel attribution was correct. In the observed463..466 case, the photo was assigned to oldpending457 and a reaction invented a hung-system bug. Preserve that evidence before corrections.

Current-source pixels take priority; optional retrieved candidate images are not automatically sent with a new incident. Exact-reply evidence remains eligible, and per-source metadata distinguishes supplied pixels from unavailable imagery. Unlinked-image multi-group proposals are reviewed before plans/tool writes even without historical candidates using the shared turn (ordinary short documents: two calls/40 seconds; bounded long-document preprocessing: TG-14). Inspect `ingress_review` metadata and model-call counts; rejected review remains visible pending. A journal hash or unit-test pass alone is not configured-model semantic evidence. Existing completed plans/routes must not be silently replayed or overwritten to repair old issues.

Operator clarification (2026-10-16): old content is updated only through an actual Reply to its source or proven bot notice. Retrieval/similarity alone cannot continue an old pending/ticket. Inspect each group's own reply_source_ids and cached/recovery guards when debugging accidental old-case updates. A current-image multi-group proposal still gets semantic review without historical candidates.

That second semantic call is a constrained critic: every host-validated proposed group receives exactly one `approve`, `context` or `reject` verdict. Approval preserves the original canonical draft; context lowers the whole group to neutral silent CHAT. The critic cannot rewrite facts, add evidence/targets, upgrade actions or merge partitions. Any rejection, malformed coverage or insufficient packet budget leaves the entire batch pending before effects, with one technical notice. Full untrusted proposals and the current evidence remain bounded by the existing limits; no third call is added. Incorrect first partitions therefore fail closed instead of being repaired by full-draft regeneration. TG-06 defines this contract.

### Analysis replies (autonomous mode)

Requests to analyze or list problems use `ANALYZE`: the Agent uses the available evidence to define a concise proposed task with the work and verifiable completion criteria. It makes the initial judgment instead of sending an OCR transcript or asking the user to choose hypotheses. A genuinely blocking ambiguity about target, intent or authority may still require one focused question. A concrete request to fix a bug follows normal ticket intake.

The model receives up to eight earlier stored same-chat messages as `READ_ONLY_CONTEXT`, even without a Reply. Their optional images follow current-source and exact-reply images in download priority and remain subject to existing image/consent limits. Packing evicts optional context before mandatory sources; truncation and missing pixels are explicit. This is a bounded excerpt, not a scan of all group history. It grants no old-case or ticket update authority. Exact replies may supply older proven evidence for analysis without assigning it to a new ticket.

`ANALYZE` records a separate terminal chat outcome (`semantic_analyze`), sanitizes its reply, and uses the durable notice reservation before sending. Dry-run sends nothing; duplicates, restart and ambiguous delivery do not resend. Existing completed conversations are not replayed. Offline acceptance tests verify routing, image attribution and recovery; configured-model semantic quality and production rollout need separate validation.

Fresh `ANALYZE` output requires `task={title,work,acceptance}`: title<=90 characters, work<=160, and one or two distinct completion criteria<=90 each. The host renders three complete sanitized lines within500 characters, without a routine scope disclaimer or selection menu. The model defines the work; the host does not invent a task by truncating a long answer. This is a task proposal, not a claim that implementation is complete or permission to write GitHub. Missing pixels and uncertain causes must be described truthfully; unverified links remain unverified.

An explicit change request uses `NEW`; its work and completion criteria belong in `analysis.details`, with no sibling `task`. Fresh-output diagnostics identify missing, forbidden or invalid task fields without recording model prose; legacy cached formats remain readable. Current text up to the existing700-character cap takes priority over optional history within the unchanged8KB envelope. Cloud redaction applies to complete source text before excerpting; local fallback keeps its existing contact policy. See TG-06/TG-30 for the authoritative limits and repair contract.

For screenshot issue reports, assess the rendered UI before interpreting embedded conversation content. Check containment, wrapping, clipping and overlap in the target application; distinguish its frame from the outer photo crop. Layout bugs need no error banner. Separate visible symptoms from unproven causes, and use the reporter's clarification to correct an earlier bot hypothesis. Model validation must check the intended findings; a valid `ANALYZE` route alone does not establish visual accuracy.

The fresh provider boundary rejects missing or invalid tasks, nonempty options and true `needs_choice`; task fields are forbidden on other actions. Legacy parser/cache records retain the old nonblank answer and valid option-state format. Previously sent offers still bind exact same-chat/repository Reply selections through the durable notice ledger; new task notices do not become selection offers. Restart, duplicates and uncertain delivery do not resend. Unrelated work in mixed batches keeps its own intake. Technical failure preserves context and reports incomplete analysis without inventing a task or requiring a new description.

Direct questions about whether a request has a ticket use read-only `STATUS`, rather than silent `CHAT`. The model identifies intent; the host resolves the actual same-chat Reply source or proven sent notice and its established partition, then answers from the local operation/issue ledger. It distinguishes confirmed ticket, processing, not yet recorded, ambiguous POST and unavailable proof. This is the bot's recorded state, not a claim that no issue exists anywhere on GitHub. Missing scope gets one focused request to Reply to the source. Status queries never create, update, comment on or retry an issue. Cached status plans re-read host evidence and retain the normal notice reservation/deduplication behavior.

Exact Reply excerpts from old no-ticket conversations remain read-only evidence and are prioritized over optional recent context; they do not become write candidates. Sparse `DOCUMENTS` metadata binds each attachment to its source ID and exposes its sanitized filename, MIME, declared size and default `read_status=unread`. Under packing pressure, `DOCUMENT_DEFAULTS` can carry shared presence/unread values and optional filename/MIME/size may be omitted with `document_metadata_truncated=true`; source IDs, document presence and each current caption's first30 characters remain intact. A document-only source must not be represented as a blank text message. Forwarded prose, filenames and document content do not grant execution or deployment authority.

Current Markdown/text and HTML/HTM attachments are read automatically as bounded strict UTF-8 (BOM accepted) evidence. TG-14 defines the authoritative limits:4 files,256KiB each/512KiB raw aggregate,192KiB extracted aggregate; HTML is inert static extraction including form constraints, with no script execution, rendering or URL fetching. Unknown filenames are not inferred as HTML from octet-stream MIME. Raw/extracted hashes and extraction kind are separate. Short text is supplied completely; longer text (including JSON-escaping pressure) is processed through every host-numbered chunk by the agent, with host-grounded block selection and complete receipt validation. These selections can be lossy; they do not prove JavaScript behavior or perfect preservation of all requirements. Failed/missing parts block effects. The base stays≤8000bytes and actual serialized document text lane≤49152bytes; private fallback messages are excluded from the actual wire projection/count, and cloud/local fallback are checked separately with existing consent/media bounds. Ordinary turns keep2calls/40s; long processing shares a host-derived finite Turn across chunks, routing and followup (TG-14). Current CONTINUE documents and proofs must reach actual followup analysis and persist before effects/resume. Raw bodies are not general telemetry. Other uploadable formats remain unread.

The model creates a ticket from substantive document requirements through existing NEW/POST reservation guards. It merges translations of the same work by actual content and preserves independent requests. Missing implementation repository/deploy access does not block a clear triage ticket in the configured repository. Referenced assets are not treated as received attachments without receipts. The bot returns a brief verified issue link. STATUS remains readonly; historical processed messages are not reset. Live verification may forward the same source files into fresh intake when the user authorizes creating the actual ticket.

Fresh ingress validation failures expose bounded host metadata only: fixed error codes, known field paths, enum types and capped lengths; unknown field names, values, document bodies and model reasoning are excluded. The existing second attempt may receive at most600 UTF-8 bytes of trusted corrective guidance, repacked within the ordinary8000-byte base and unchanged full-document/image limits. Optional historical context may be omitted under the existing packing priority; current source IDs, current/required image evidence and full current documents remain protected. Repaired output passes the same source, task and authority checks; ordinary turns have no third attempt or extra fallback allowance; long-document preprocessing uses the single host-derived TG-14 budget, never a fresh per-part allowance. Cached outcomes are not rewritten.


Documentation change v1.8.0 — 2026-10-21 10:00 Asia/Saigon: align operator guidance with TG-14 HTML/long-document processing, wire accounting and CONTINUE proofs. Deployment and live success require separate evidence.
