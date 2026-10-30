# QA: Telegram triage THINKING

**Version** v1.6.0 · **Updated** 2026-10-16 15:23 Asia/Saigon · **Owner** AlexChu · **Executor** Codex

## Related Documents

- [Master scope](../00-master/worker-agent-fleet-1_master_261007_v1.0.8.md)
- [TG-01..TG-33](../01-product/telegram-triage-noissue_business_261015_v1.0.0.md)
- [Engineering and research](../02-engineering/telegram-triage-noissue_thinking-research_261015_v1.0.0.md)

## Historical Harness foundation acceptance (2026-10-16)

Các case dưới là yêu cầu kiểm, chưa phải kết quả PASS. Dùng temporary SQLite và fake tools, không mở database production/session hoặc gọi model thật. Bằng chứng configured-model cũ không chứng minh chất lượng semantic routing mới vì foundation chưa triển khai routing đó.

| Case | Kết quả bắt buộc |
|---|---|
| Ba source cùng yêu cầu / ba yêu cầu độc lập | Projection đúng một / ba topic theo partition đã chứng minh; không đổi originals accounting |
| #46 design image/caption và backend chat | Giữ topic đúng scope source; backend không biến thành cùng yêu cầu do gần thời gian |
| #47 POS originals và reply photo | Proven root giữ đúng originals/followup, không tạo issue hay gửi Telegram mới |
| Reply source/notice cũ sau restart | Giữ đúng cùng-chat root và topic; không đoán root format hoặc lấy tuổi tin làm authority |
| Forged URL, cross-chat IDs, legacy time-only followup | Không có write authority hay membership được xác nhận sai |
| Followup split có anchors xác nhận và media chưa gán | Registry ghi scope original_intake; trace hiện sibling/binding/unassigned metadata và gap split_partition_not_projected, không giả định current partition một ticket |
| Hydration hai lần/restart, sửa partition | Không event trùng; correction append event mới và giữ revision cũ; original tables unchanged |
| Old schema thiếu topic/decision tables | Trace báo capability/gap, không migrate, không lỗi do thiếu module provider |
| Trace read-only | Không logical writes/migration/recovery/network/auth/model. Với snapshot rollback-journal độc lập: DB hash/mtime và directory entries không đổi. WAL/concurrent writer kiểm riêng và báo giới hạn auxiliary files, không hứa immutability mọi trạng thái |
| Privacy | Default không text/title/summary/raw plan/result/payload/context; text opt-in riêng tư |
| Bounds | SQL/traversal/output bị giới hạn; truncation có marker, không coi tập bị cắt là context đầy đủ |
| Repository+issue ambiguity | Không gộp nhầm issue cùng số ở repo/chat khác; target chưa chứng minh được báo rõ |
| Processing vs lifecycle | Case done không bị diễn giải GitHub closed; không reopen/recurrence writes trong foundation |

Chạy targeted topic/trace tests, toàn bộ listener unittest, compileall, policy drift checker và 17 policy negative tests; reviewer khác writer kiểm authority/privacy/data integrity. So diff phạm vi thực tế và package module mới. Không suy ra p95/accuracy hoặc production enablement từ offline suite.

Lifecycle policy user đã chốt được ghi ở nguồn nghiệp vụ; test cho API recurrence/related và event-driven readiness thuộc increment tiếp theo. Foundation chỉ PASS khi báo rõ giới hạn này, không tuyên bố đã thay hoàn toàn gom hai phút.

### Foundation verification — 2026-10-16 10:28 Asia/Saigon

Final source-only verification: independent listener suite ran **806 tests**, `OK (skipped=1)` in 102.201s; targeted topic/trace **20 tests PASS** in 14.756s; policy checker **32 rules / 32 runtime files PASS** and **17 negative tests PASS**. Final independent review: **APPROVED**. Legacy CLI audit confirmed isolated rollback-journal file hash/mtime/directory immutability and no network/auth/model/recovery; WAL test verifies logical read-only behavior and reports sidecar caveat. Package-shape audit includes 27 modules and 5 prompts, not a release wheel or installed-runtime claim.

Evidence is local under `.context/harness-foundation-independent-*`; these are captured test results, not live production state. Runtime fingerprint: `69917b6b0a6c8f7acd37416a9fb7cdc41aa0cddedc2752f107983b00897553d0`. No model accuracy benchmark, commit, push or THINKPAD deployment performed in this increment. The generic developer `harness-verify.mjs` reports Node-only/no package.json for this Python repo; its formal gate is **not PASS**. Actual Python commands and independent review above are recorded separately, not substituted silently for that gate.

## Test Strategy

POS regression must execute the actual daemon startup factory: five forwarded messages identify one POS printing bug on app and web with a wired printer, without re-asking supplied facts. The old default-prompt override must reproduce zero-call budget failure. Twenty-source packets must preserve identity/reply/time/text and stay bounded; contact redaction must leave internal numeric IDs valid while raw user JSON receives no internal trust.

Reply photo454 must reach the provider in the same USER packet as original context/caption, with its exact SHA and visible error `Nothing was sent to the printer. Try printing again.` A validated update must refresh the same fake ticket through the canonical BUG renderer, include uploaded evidence, and create no extra issue. Test new reply-role acceptance only for update+case_analysis; reject foreign, excluded and split reply-role IDs. Detected manual body/title/owned-label edits, ambiguous siblings, denied/unavailable vision, uncertain uploads, deadline-before-write and unknown-after-write must preserve scope and produce an honest outcome. Restart/repeated ingress must not repeat uploaded/reserved refresh operations. Explicit empty model fields must clear prior hypotheses; omitted fields preserve prior facts.

Use `--case pos_print_context --sources-fixture /path/to/reviewed-sources.json --image-path /path/to/454.jpg` with the configured-model smoke. It uses memory SQLite and fake GitHub/upload/Telegram tools; historical production repair is a separate reviewed operation, never a replay of completed ingress. Retain the initial `invalid_source_roles` real-model failure alongside final source/installed samples.

Hosted repair regression uses the real CloudProvider/Pipeline with fake network/tools: malformed JSON or parser-invalid output then valid partition must use two cloud calls with identical bundle/schema; network failure must use cloud then configured local; two malformed outputs must stop after two calls with honest scoped clarification. Legacy THINKING-off fallback remains covered. Installed malformed-image failure is retained; successful reruns do not erase it or establish model accuracy.

Test the production Pipeline, Store and provider entry paths with fake GitHub/Telegram tools and temporary SQLite. The plan was recorded before implementation in the task's durable `.context/thinking-runtime-plan.md`; this artifact captures acceptance cases. Existing safety suites run with THINKING enabled by default. A separate opt-in smoke uses the configured real model, synthetic text and fake external tools; it never opens live SQLite or Telegram session. Independent reviewer/verifier and deployment controller report their own machine output; this document does not grant itself a release PASS.

## Test Cases

### TC-01: Three messages, one request

| Property | Value |
|---|---|
| Priority | P0 |
| Type | Happy path |
| Precondition | Real default classifier prompt; fake provider response; isolated Store |

**Steps**: send three untagged messages describing one broken save button; close the window.

**Expected Result**: one issue; one reply to source 1 with its actual canonical link; decision planned before create, verified afterward.

### TC-02: Three independent requests

| Property | Value |
|---|---|
| Priority | P0 |
| Type | Happy path |
| Precondition | One window, three distinct work requests |

**Steps**: send save-button bug, date-filter request and stop-opt-out request; classify three partitions.

**Expected Result**: three issues and replies to sources 1/2/3 with matching links; no tag required.

### TC-03: Missing information

| Property | Value |
|---|---|
| Priority | P0 |
| Type | Happy path |
| Precondition | Work request recognized but screen/result unknown |

**Steps**: return needs_human with a focused missing-screen question.

**Expected Result**: tracking issue and group reply asking the missing question, bot xưng Em; no silent approval queue.

### TC-04: Screenshot context and restart

| Property | Value |
|---|---|
| Priority | P0 |
| Type | Happy path |
| Precondition | Synthetic sources 429/431 work, 430/432 captionless media in one original ticket |

**Steps**: reply “cần lên 2 ticket”; reply source 429 “đây là ticket 1”; add seven history turns; restart SQLite; reply source 431 “đây là ticket 2”.

**Expected Result**: confirmed bindings survive trim/restart; original issue plus one sibling; 430/432 explicitly unassigned; source 431 only in sibling; no repeated request for known ticket-1 anchor. Duplicate ingress does not create again.

### TC-05: Rollback and durable trace

| Property | Value |
|---|---|
| Priority | P0 |
| Type | Happy path |
| Precondition | `[thinking] enabled=false` |

**Steps**: process a normal case; inspect SQLite and provider output cap.

**Expected Result**: legacy semantic path/output limits; existing reservations and historical decision records remain; no new THINKING journal entry.

### TC-06: Budget, queue and stale state

| Property | Value |
|---|---|
| Priority | P0 |
| Type | Edge case / Performance |
| Precondition | Invalid provider outputs; occupied model lock; two writers on same decision key |

**Steps**: exhaust cloud retries then attempt fallback; expire a lock wait; submit a stale decision version; exhaust text/read bounds.

**Expected Result**: at most two actual model attempts shared across retries/fallback; no HTTP attempt consumed during queue-only expiration; stale CAS rejected; retained confirmations and safe clarification fallback.

### TC-07: Authority and ambiguous effects

| Property | Value |
|---|---|
| Priority | P0 |
| Type | Edge case / Error handling |
| Precondition | Existing followup safety fixtures |

**Steps**: run conflicting sender, cross-root/cross-chat, invented source ID, third-request-as-ancillary, reserved POST crash and notice-failure cases.

**Expected Result**: no invented authority, hidden third request or duplicate POST; confirmed anchors survive; ambiguous outcome requires reconciliation. Unknown profile uses Anh/Chị.

### TC-08: Existing DeepSeek think=false reaches the wire

| Property | Value |
|---|---|
| Priority | P0 |
| Type | Error handling / Provider contract |
| Precondition | Configured hosted thinking false; fake HTTP connections |

**Steps**: call bounded transport for DeepSeek, OpenAI, compatible, Anthropic and Ollama; capture serialized request bodies.

**Expected Result**: DeepSeek has exactly `thinking: {type: disabled}` and `temperature: 0`; the other four have no new top-level `thinking`/`temperature` field. Provider/model/consent and production20/40 budgets stay unchanged. No reasoning_content is read or persisted. Run default20/40 configured-model smoke with the original fixed labels; diagnostic30/60 experiments must never be reported as production behavior.

### TC-09: Intake essentials versus debugging extras

| Property | Value |
|---|---|
| Priority | P0 |
| Type | Happy path / Missing information |
| Precondition | Known module/setting, observed contrary behavior and desired correction supplied |

**Steps**: run the unchanged fully specified three-request model fixture; compare with a genuinely vague “nút này lỗi” request.

**Expected Result**: the clear existing-setting bug is actionable without demanding platform/logs/full reproduction; vague object/behavior gets a focused question. Do not change confidence thresholds, fixture labels or source partition assertions. Do not invent any unknown debugging facts.

### TC-10: Image-caption regression acceptance

Production-path fixtures in `listener/tests/test_thinking_image_context.py` cover: captioned image + long backend Q&A + ack; lone caption image; fair marked excerpts with all IDs/reply refs; uncertain image group beside independent task/chat; image-reply partition contradiction; unavailable pixels/pure questions; cloud-image-consent false; explicit command failure/type; exact pending notice/source proof and fresh reply intake preserving old rows/dedup. Existing image tests now check text-only classification with unavailable-pixel markers and scoped failure instead of asserting a whole-case fallback ticket.

Opt-in actual-image smoke (fake GitHub/Telegram/upload, memory SQLite):

```text
python scripts/telegram-thinking-model-smoke.py --config /etc/tg-triage/listener.toml --case image_caption_context --image-path /path/to/reviewed.jpg --sources-fixture /path/to/ignored-sources.json
```

The source fixture is optional; exact historical source export and pixels remain ignored/local and are never committed. Assert one actionable task/feedback for source442, matching reply, grounded Salon/visibility/selected Chat only context, no unneeded design-layout/deadline/ordering questions, source443 Q&A and444 ack classified chat/noise. Actual provider transport must receive the exact image SHA under existing consent and remain within8000 text bytes. Reports expose sanitized generated wording, JSON diagnostics and counts, never private reasoning. Run all four earlier model cases too. No real tickets/notices/live DB replay. Code tests alone do not establish vision grounding; failed semantic previews remain evidence until corrected package proof passes.

## Performance Benchmarks

Semantic model work has a shared 40-second deadline including model-lock queue, bounded DNS, TCP/TLS and response reading; each attempted model request gets at most 20 seconds of remaining time. Two attempts total, three logical scoped retrieval batches and conservative 8000 UTF-8 text bytes per request. Source-role decisions cap 512 output tokens; complete topic/followup drafts retain configured output limits. Images have provider-specific token costs outside the text bound. Attachment/GitHub/Telegram operations keep their existing timeouts: **40 seconds is not an end-to-end ticket/reply SLA**. Assert budgets with deterministic clocks plus real queue/DNS stop tests; small live-model smoke is not p95 or accuracy measurement.

## Security Tests

Verify evidence IDs/sender/root/repository before tools; sanitize generated fields; journal excludes raw transcript, bio, secrets and private reasoning. DNS accepts only public pinned addresses for hosted providers, local Ollama stays loopback. The model-smoke runner exposes no real GitHub/Telegram client. Run existing consent/redaction and profile tests unchanged.

## Test Data Requirements

Synthetic fixtures in `listener/tests/test_thinking.py` and existing `test_followups.py`; recognizable short request phrases reproduce the user's reported failure without copying private profile/account data. Screenshot IDs are fixture labels, never production replay inputs. The opt-in model smoke uses synthetic IDs 901..904. No claim that the proposed 60-conversation human-labelled corpus has been completed.

## Environment Setup

From `listener`, set `PYTHONPATH=src` (PowerShell: `$env:PYTHONPATH='src'`) then run:

```text
python -m unittest tests.test_thinking -q
python -m unittest discover -s tests -t . -q
```

From repo root:

```text
python scripts/check-telegram-triage-policy.py
python scripts/test_telegram_triage_policy.py
```

Opt-in on THINKPAD, with the configured listener user/venv and the reviewed package/source:

```text
python scripts/telegram-thinking-model-smoke.py --config /etc/tg-triage/listener.toml
```

## Acceptance Criteria

- [ ] Default THINKING wire has one USER context bundle containing captions, all source IDs/reply edges and all permitted images; multiple-image order maps explicitly to source IDs for local and cloud. Actual-image smoke reports `single_context_bundle=true`.
- [ ] All P0 deterministic cases and full listener regression pass on reviewed source.
- [ ] Independent code review and verification approve current source.
- [ ] Real configured-model smoke passes; report sample size and limitations.
- [ ] Wheel contains all five prompts and both THINKING modules; installed bytes match commit.
- [ ] Listener active, additive SQLite tables initialized, NailHub/shared modules unchanged.
- [ ] Canonical rule trace checker and negative tests pass.

UI/browser/responsive/a11y changes: none. No merge authorization inferred from a PR update.

## Changelog

| Version | Date/time | Author | Change |
|---|---|---|---|
| v1.6.0 | 2026-10-16 15:23 Asia/Saigon | Codex / owner AlexChu | Booking incident/historical pixel isolation and pre-side-effect semantic review acceptance. |
| v1.5.0 | 2026-10-16 12:00 Asia/Saigon | Codex / owner AlexChu | Chat timing/late context/anti-spam and real-history packet pressure acceptance. |
| v1.4.0 | 2026-10-16 11:31 Asia/Saigon | Codex / owner AlexChu | Autonomous/lifecycle business acceptance, actual-context/model regression và separate release proofs. |
| v1.3.0 | 2026-10-16 10:11 Asia/Saigon | Codex / owner AlexChu | Acceptance topic/trace foundation: provenance, immutable history, readonly/legacy/privacy/bounds và scope lifecycle. |
| v1.2.0 | 2026-10-16 09:11 Asia/Saigon | Codex / owner AlexChu | Actual startup/POS source-context, new reply image, managed refresh, source roles and generated-field smoke assertions. |
| v1.1.0 | 2026-10-15 23:29 Asia/Saigon | Codex / AlexChu owner | Image-caption/context, privacy, scoped failure and fresh-intake regression with actual-image proof criteria. |
| v1.0.2 | 2026-10-15 22:26 Asia/Saigon | Codex / AlexChu owner | Intake essentials and DeepSeek sampling wire regression; fixed-label smoke remains required. |
| v1.0.1 | 2026-10-15 22:19 Asia/Saigon | Codex / AlexChu owner | Wire-level existing think=false contract and fixed-label default-budget verification. |
| v1.0.0 | 2026-10-15 21:39 Asia/Saigon | Codex / AlexChu owner | Capture production-path acceptance, model-smoke isolation and honest budget limits. |

## Autonomous/lifecycle acceptance v1.4.0

Release verification must exercise the final frozen source and installed wheel separately. Offline tests cover three-to-one/three-to-three, interleaved topics, exact old replies/new scope, pending answer continuation, samebatch images/captions, ordinal corrections, current split partition, closed evidence/recurrence/unclear, successful POST with failed readback, repo drift, route/case/batch crash resume and ambiguous operations. Nonanchor batch source trace must resolve its job, and a reply to a recurrence notice must survive actual Telegram ingress normalization.

The configured-model smoke uses actual startup factory and supplied #46/#47 JPEG fixtures with fake GitHub/Telegram/in-memory SQLite only. Assertions require generated Salon/Chat only task grounding, no backend-Q&A contamination, POS app+web+wired facts and literal printer error after image supplementation, correct source replies and recurrence102 subsequent update. Keep failed samples; no weakening assertions to hide a failure. Results are a small regression corpus, not model accuracy or service SLA. Final verification/release evidence is recorded after source freeze and deployment; historical foundation counts above do not prove this increment.

### Configured-model preview result (2026-10-16 11:31 Asia/Saigon)

Five cases PASS using the actual configured startup provider, actual reviewed #46/#47 JPEG pixels, fake GitHub/Telegram and memory SQLite. Three-to-one replies [1]; three-to-three replies [1,2,3]. #46 generates a TASK for Salon information & visibility / Chat only, source442 only, backend Q&A/ack CHAT. #47 preserves essential sources448..451/canonical source links, app+web+wired environment and generated exact printer error, refreshes issue101, replies448/454. Closed recurrence creates related102 from current502 exact temporal quotes; subsequent503 refreshes102 without a third ticket. Preview archive binding: all37 runtime/prompt files match reviewed fingerprint `aee0d68b9eb11de1baaff3fea5f4b40920180f2afce892cc8c9d5b162de92d43`.

Earlier failed samples were retained on the private THINKPAD preview path: #46 over-clarification, #47 second-packet budget exhaustion and missing recurrence proof. They prompted code/prompt fixes and stronger assertions, not relaxed parsing or budget increases. Complete metadata is under `/tmp/autonomous-v2-preview/model-result*.json` for this test session; temporary files are not a durable product database. Five examples do not establish general AI accuracy. Installed/release verification remains separate.

### Final frozen source verification

Independent listener: `python -m unittest discover -s tests -t . -q`, 867 tests in114.340s, one Windows skip, exit0 (2026-10-16 11:30–11:32 Asia/Saigon). Independent targeted review83 PASS, implementation integration100 PASS. Policy checker32 rules/37 runtime files and17 negative tests PASS; compileall/diffcheck PASS. Evidence `.context/autonomous-v2-independent-refrozen-*`; previous moving-tree reports remain provisional and are not substituted for these frozen results. Fingerprint remains `aee0d68b9eb11de1baaff3fea5f4b40920180f2afce892cc8c9d5b162de92d43`. Generic developer Harness gate remains Node-only/no package.json for this Python repository; its formal gate is not PASS.

## Chat pacing acceptance — 2026-10-16

Required: no model/notice before30s quiet; same-chat continuation resets quiet up to60s cap; other chats retain independent deadlines even while a model call is blocked. Initial and followup ingress both wait; direct Pipeline tests stay immediate. Actual worker + Pipeline must analyze synthetic login text/later photo in one packet and reply correct first source; mixed requests still split semantically. Late (>60s) reply/photo must retrieve pending questions/context, avoid normalized duplicate clarification across restart, and keep new topic/new essential questions visible. Stop/cancel/replay/queue.join and sealed batch boundary must preserve every queued row and prevent the active call from consuming the next batch.

Production incident metadata (read-only): new sources457/458/461 were context_budget_exhausted with zero model calls; no source text/credentials printed. Reproduce ordinary login under3 historical candidates7+1+6 sources: actual classifier request must fit8KB and execute; preserve exact old replies, bindings/source IDs; removed candidate pixels cannot remain in packet, and model cannot choose a removed candidate. Mandatory-only overflow retains visible technical fallback. Provider failures send one batch question without merging independent source accounting. Supplied screenshot contains credentials; use synthetic fixtures and never upload it in tests. Final source/release/installed evidence is appended only after frozen verification.

### Frozen verification / pacing increment

Independent listener904 tests in122.011s, one Windows skip, exit0 (2026-10-16 12:16–12:18 Asia/Saigon). Independent targeted review41 PASS17.690s; scheduler17 +existing transport11 PASS; conversation14 and packet5 regressions included in full suite. Policy32 rules/38 runtime files and17 negative tests PASS; compileall/diff checks PASS. Frozen fingerprint `8e4236ec78a733783b8e2935ad8cb1ad1f92ee98f7f4c8dff10db2ee045d779e`. Metadata trace preserves packing/outage/clarification evidence without exporting question/hash.

Configured-model preview7/7 PASS using fake GitHub/Telegram and memory SQLite: five historical cases plus synthetic login under3 oldtopic pressure and clarification reply after90s. New caption test keeps login sources457/461 together and billing458 separate; late pending creates one ticket and replies original601. This preview predates final debug/scoped-recovery deltas in autonomous.py/pipeline.py, so it is provisional for the complete frozen tree. Immutable installed7-case rerun is required after commit. New login fixture uses synthetic text; actual pixel bundling/timing are exercised with fake HTTP and virtual clock in worker tests, not a credential-bearing screenshot.

Recovery scope is checked when entering a sweep/batch. A model/tool call already in flight is allowed to finish; later typing cannot safely cancel an already reserved external operation. Quiet/cap and sealed-source guarantees apply to newly collected ingress. Full semantic merging of multiple old independent pending roots is not implemented; unclear membership remains explicit. Generic developer Harness gate remains Node-only/no package.json for this Python repo; no formal PASS is claimed.

Independent deployment suite46 PASS168.530s; real staged listener904 PASS111.371s(skip1), NailHub227 PASS27.144s(skip5). Final frozen hash drift empty at12:19:58 Asia/Saigon. Evidence .context/chat-pacing-independent-final.json. Immutable commit/installed proof remains a separate release step.

## Incident grouping correction — v1.6.0, 2026-10-16 15:23 Asia/Saigon

### TC-11: Booking incident with competing old login history
| Property | Value |
|---|---|
| Priority | P0 |
| Type | Context/vision regression |
| Precondition | Fake GitHub/Telegram and memory SQLite; actual configured model; reviewed booking image463, safe synthetic pending login/billing history; no credential-bearing login pixels |

Steps: enqueue463 image COMMON_VALIDATION_ERROR,464 “Kiểu nầy chết luôn”,465 “AI voice không gửi link booking”,466 “Khách vào web không book được luôn”; process through real startup provider in one ingress.
Expected: one booking ticket, all four sources, one confirmation replying463; no oldlogin457 membership or Invalid username or password claim; no standalone emotional/hung-system bug. Compare against the observed production failure, not a preselected mocked partition. Unit coverage alone does not prove model grouping.

### TC-12: Historical pixel isolation and bounded review
| Property | Value |
|---|---|
| Priority | P0 |
| Type | Safety/error handling |
| Precondition | Current image plus optional historical image candidates; exact-reply control case; injected provider outcomes |

Steps: inspect preparation/download order and one context packet; exercise risky first partition then corrected/invalid second proposal; exercise deadline/call cap.
Expected: current pixels remain available under image limits, nonreply historical candidates/pixels neither receive routing authority nor get sent, exact-reply context/pixels retained; no plans/issues before accepted review; invalid/timeout output stays pending with at most one technical notice. No budget expansion. Independently requested login/billing/three-topic cases stay separate; late evidence still uses original scope.

Execution status: pending new source/model/release verification; results must be recorded separately with actual counts, model, timestamps and artifact identity.

### TC-13: Reply ownership is required to change old content
| Property | Value |
|---|---|
| Priority | P0 |
| Type | Authority/recovery regression |
| Precondition | Existing old pending/open/closed cases and proven bot notices; mixed new groups with and without real replies |

Steps: send a similar new request with no Reply; then Reply to oldsource/notice; submit a partition whose nonreply group tries to borrow a sibling group's exact candidate; recover a cached pre-rule nonreply CONTINUE/CLARIFY plan.
Expected: no-Reply inputs cannot update or promote old content. Exact source/notice Reply still authorizes semantic/lifecycle evaluation; no forced scope match. Each group must carry its own proven Reply authority, and cached/recovery cannot bypass it. Booking plus independent new login confirms463 and467, keeps old457/458/461 unchanged, and creates no standalone emotion bug. Current image multi-group review still runs with zero history candidates.
