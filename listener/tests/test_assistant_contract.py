import hashlib
import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from tg_triage import assistant_contract as contract
from tg_triage.__main__ import _load_assistant_pack, _load_decision_rules
from tg_triage.assistant_pack import load as load_pack
from tg_triage.config import AssistantConfig, ConfigError, load_config
from tg_triage.pipeline import Pipeline
from tg_triage.rules_pending_store import RulesPendingStore
from tg_triage.store import Store
from tests.helpers import EXAMPLE, FAKE_HASH, NOW, FakeGitHub, config, message
from tests.test_assistant_integration import Provider


def group(ids=(42,), **changes):
    defaults = dict(message_ids=list(ids), state="CHAT", rule="R8", template=None,
                    issue=None, target=None, relation=None, question=None, refs=[],
                    confidence=0.8, reasoning_summary="Grounded in the source")
    return {**defaults, **changes}


def issue(*names, value="Receipt printer", source_ids=(42,), title="Printer"):
    return {"title": title, "fields": {
        name: {"value": value, "source_ids": list(source_ids)} for name in names
    }}


def wire(*groups):
    return {"contract": "decide/v1", "groups": list(groups)}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.batch = [message(42, text="Receipt printer jammed")]
        self.ctx = contract.GuardContext(
            message_ids=frozenset({42}), chat_id=self.batch[0].chat_id,
            gh_repo="sandbox/triage", reply_targets={}, tool_targets={},
            rule_ids=frozenset({"R6", "R8", "R9"}), rule_text=lambda rule: rule + " · snapshot text",
            question_block_ids=frozenset({"ask.object", "question.fallback"}),
        )

    def parse(self, *groups, batch=None, **changes):
        return contract.parse(wire(*groups), batch=self.batch if batch is None else batch,
                              ctx=replace(self.ctx, **changes))

    def test_schema_accept(self):
        schema = contract.json_schema()
        choices = schema["properties"]["groups"]["items"]["properties"]
        self.assertEqual(set(choices["state"]["enum"]), contract.STATES)
        self.assertEqual(choices["refs"]["maxItems"], 5)
        self.assertEqual(choices["reasoning_summary"]["maxLength"], 500)
        ready = group(state="READY", rule="R9", template="bug", issue=issue("object", "observed"),
                      refs=[{"number": 10}], reasoning_summary="a" * 500)
        groups, errors = contract.parse(json.dumps(wire(ready)), batch=self.batch,
                                        ctx=replace(self.ctx, tool_targets={10: "open"}))
        self.assertEqual(errors, [])
        self.assertIsInstance(groups[0], contract.GroupDecision)
        self.assertIsInstance(groups[0], contract.Decision)
        self.assertEqual(groups[0].rule_text, "R9 · snapshot text")
        with self.assertRaises(TypeError):
            groups[0].issue["fields"]["object"]["value"] = "changed"

    def test_schema_reject(self):
        for response in (
            "broken", '{"contract":"other","contract":"decide/v1","groups":[]}',
            {"contract": "decide/v2", "groups": [group()]}, wire(),
            wire(group(message_ids=[True])), wire(group(confidence=float("nan"))),
            wire(group(confidence=1.01)), wire(group(confidence=True)),
            wire(group(refs=[{"number": 1}] * 6)),
            wire(group(refs=[{"number": 1.5}])), wire(group(reasoning_summary="a" * 501)),
            wire(group(template="other")), wire(group(issue=issue("invented"))),
            wire(group(issue=issue("object", source_ids=[]))),
            wire(group(target={"basis": "search", "number": 23, "repo": "evil/repo"})),
            wire(group(question={"block_id": "ask.object", "fields": ["invented"]})),
        ):
            with self.subTest(response=response):
                groups, errors = contract.parse(response, batch=self.batch, ctx=self.ctx)
                self.assertEqual(groups, [])
                self.assertEqual(errors[0].guard, "schema")

    def test_guard_coverage(self):
        for groups, batch in (
            ([group(ids=(44,))], self.batch),
            ([group(), group()], self.batch),
            ([group(ids=(42, 42))], self.batch),
            ([group()], [replace(self.batch[0], chat_id=-22)]),
            ([group()], self.batch + self.batch),
        ):
            with self.subTest(groups=groups, batch=batch):
                _, errors = self.parse(*groups, batch=batch)
                self.assertEqual([error.guard for error in errors], ["guard_coverage"])

    def test_guard_state(self):
        _, errors = self.parse(group(state="INVENTED"))
        self.assertEqual([(error.guard, error.group_index) for error in errors], [("guard_state", 0)])

    def test_guard_missing_field(self):
        for template, fields, valid in (
            (None, ("object", "observed"), False), ("bug", ("object",), False),
            ("bug", ("object", "observed"), True), ("task", ("object", "work"), True),
            ("task", ("work",), False), ("feedback", ("problem",), True),
            ("feedback", ("proposal",), True), ("feedback", ("object",), False),
        ):
            with self.subTest(template=template, fields=fields):
                _, errors = self.parse(group(state="READY", template=template, issue=issue(*fields)))
                self.assertEqual([error.guard for error in errors], [] if valid else ["guard_missing_field"])

    def test_guard_repo(self):
        for repo, valid in (("Sandbox/Triage", True), ("foreign/triage", False), ("", False)):
            with self.subTest(repo=repo):
                groups, errors = self.parse(
                    group(state="APPEND", target={"number": 12, "basis": "search"}, relation="duplicate"),
                    tool_targets={12: "open"}, tool_target_repos={12: repo},
                )
                self.assertEqual([error.guard for error in errors], [] if valid else ["guard_repo"])
                self.assertEqual(bool(groups[0].targets), valid)

    def test_guard_target(self):
        for basis, number, state, relation, confidence, allowed in (
            ("reply", 12, "APPEND", None, 0.0, True),
            ("reply", 99, "APPEND", None, 0.0, False),
            ("search", 12, "APPEND", "duplicate", 0.8, False),
            ("search", 23, "APPEND", "duplicate", 0.8, True),
            ("search", 23, "APPEND", "duplicate", 0.79, False),
            ("search", 23, "APPEND", "related", 1.0, False),
            ("search", 22, "APPEND", "duplicate", 1.0, False),
            ("search", 22, "READY", "related", 0.3, True),
        ):
            with self.subTest(basis=basis, number=number, state=state, relation=relation, confidence=confidence):
                groups, errors = self.parse(
                    group(state=state, target={"number": number, "basis": basis}, relation=relation,
                          confidence=confidence, template="feedback", issue=issue("proposal")),
                    reply_targets={42: (12, "open")}, tool_targets={22: "closed", 23: "open"},
                )
                self.assertEqual([error.guard for error in errors], [] if allowed else ["guard_target"])
                if state == "READY":
                    plan = contract.to_plan(groups[0], rules_fingerprint="snapshot")
                    self.assertEqual(plan["related"], 22)
                    self.assertEqual(plan["targets"], [])
                if number == 23 and not allowed:
                    self.assertFalse(errors[0].blocking)
                    self.assertEqual(groups[0].target, {"basis": "reply", "number": 12})
                    self.assertEqual(groups[0].related, (23,))
                    self.assertEqual(groups[0].state, "APPEND")
                elif errors:
                    self.assertTrue(errors[0].blocking)

    def test_search_downgrade_preserves_question_and_rechecks_grounding(self):
        candidate = group(state="APPEND", target={"number": 22, "basis": "search"},
                          confidence=0.6, relation="duplicate", template="bug",
                          issue=issue("object"), question={"block_id": "ask.object", "fields": ["object"]})
        ctx = dict(tool_targets={22: "open"}, rule_ids=self.ctx.rule_ids | {"R12"})
        groups, errors = self.parse(candidate, **ctx)
        self.assertEqual([(v.guard, v.blocking) for v in errors], [("guard_target", False)])
        self.assertEqual((groups[0].state, groups[0].rule), ("NEED_INFO", "R12"))
        plan = contract.to_plan(groups[0], rules_fingerprint="snapshot")
        self.assertEqual((plan["question_block"], plan["related"], plan["targets"]), ("ask.object", 22, []))
        _, errors = self.parse({**candidate, "issue": issue("object", source_ids=(99,))}, **ctx)
        self.assertEqual([(v.guard, v.blocking) for v in errors],
                         [("guard_target", False), ("guard_grounding", True)])

    def test_guard_channel(self):
        for block, fields, allowed in (
            ("invented", ["object"], False),
            ("ask.object", ["object", "observed", "expected", "work"], False),
            ("ask.object", ["object"], True), ("question.fallback", [], True),
        ):
            with self.subTest(block=block, fields=fields):
                _, errors = self.parse(group(state="NEED_INFO", question={"block_id": block, "fields": fields}))
                self.assertEqual([error.guard for error in errors], [] if allowed else ["guard_channel"])

    def test_refs_keep_only_unambiguous_proofs_in_write_repo(self):
        groups, errors = self.parse(
            group(refs=[{"number": n} for n in (1, 2, 3, 4, 120)]),
            reply_targets={42: (3, "open")}, tool_targets={4: "open", 120: "open"},
            tool_target_repos={2: "other/repo", 4: self.ctx.gh_repo, 120: None},
        )
        self.assertEqual([dict(ref) for ref in groups[0].refs], [{"number": 3}, {"number": 4}])
        self.assertEqual([(v.guard, v.blocking) for v in errors], [("guard_target", False)])

    def test_guard_grounding(self):
        for source_ids, batch, observed, allowed in (
            ((43,), self.batch, frozenset(), False),
            ((42,), [message(42, text="", has_image=True)], frozenset(), False),
            ((42,), [message(42, text="", has_image=True)], frozenset({42}), True),
        ):
            with self.subTest(source_ids=source_ids, observed=observed):
                _, errors = self.parse(group(issue=issue("object", source_ids=source_ids)),
                                       batch=batch, seen_image_ids=observed)
                self.assertEqual([error.guard for error in errors], [] if allowed else ["guard_grounding"])

    def test_guard_image_split(self):
        for image_id in (42, 43):
            batch = [replace(self.batch[0], has_image=image_id == 42),
                     message(43, reply_to_id=42, has_image=image_id == 43)]
            for groups, allowed in (([group(), group(ids=(43,))], False),
                                    ([group(ids=(42, 43))], True)):
                with self.subTest(image_id=image_id, groups=groups):
                    _, errors = self.parse(*groups, batch=batch, message_ids=frozenset({42, 43}))
                    self.assertEqual([error.guard for error in errors], [] if allowed else ["guard_image_split"])

    def test_guard_document(self):
        batch = [message(42, text="", has_document=True)]
        for read in (frozenset(), frozenset({42})):
            with self.subTest(read=read):
                _, errors = self.parse(group(), batch=batch, read_document_ids=read)
                self.assertEqual([error.guard for error in errors], [] if read else ["guard_document"])

    def test_guard_sanitize(self):
        for raw, cutoff, expected in (
            ("abcdefghijk", 7, "abcdefg"),
            ("<b>fix</b>", 40, "&lt;b&gt;fix&lt;/b&gt;"),
            ("someone@example.com +1 345 678 9012", 80, r"\[email đã ẩn\] \[số đã ẩn\]"),
            ("someone@example.\x00com", 80, "[email đã ẩn]"),
        ):
            with self.subTest(raw=raw, cutoff=cutoff):
                groups, errors = self.parse(group(issue=issue("object", value=raw, title=raw)),
                                            max_value_chars=cutoff)
                self.assertEqual(errors, [])
                self.assertEqual(groups[0].issue["title"], expected)
                self.assertEqual(groups[0].issue["fields"]["object"]["value"], expected)
        groups, errors = self.parse(group(issue=issue("object", value="someone@example.com")),
                                    max_value_chars=4)
        self.assertEqual(errors, [])
        self.assertNotIn("some", groups[0].issue["fields"]["object"]["value"])

    def test_guard_unknown_rule(self):
        _, errors = self.parse(group(rule="INVENTED"))
        self.assertEqual([error.guard for error in errors], ["guard_unknown_rule"])
        groups, errors = self.parse(group(), rule_text=lambda rule: "ộ" * 1000)
        self.assertEqual(errors, [])
        self.assertEqual(groups[0].rule_text, "ộ" * (2048 // 3))

    def test_to_plan_round_trips_through_pending_store(self):
        groups, errors = self.parse(group(state="READY", rule="R9", template="bug",
                                          issue=issue("object", "observed"), refs=[{"number": 21}]),
                                    tool_targets={21: "open"})
        self.assertEqual(errors, [])
        fingerprint = _load_decision_rules().fingerprint
        plan = contract.to_plan(replace(groups[0], run_id=8), rules_fingerprint=fingerprint)
        legacy_keys = {
            "source_keys", "state", "intent", "intent_rule", "rule", "present", "questions",
            "asked_fields", "task", "analysis_review", "analysis_origin", "lifecycle",
            "lifecycle_assets", "lifecycle_sources", "lifecycle_pixels", "lifecycle_context",
            "lifecycle_question", "lifecycle_evidence_answer", "lifecycle_recurrence_answer",
            "lifecycle_source_evidence", "targets", "pending_topic", "pending_reply_ids", "document_reads",
        }
        self.assertEqual(set(plan) - legacy_keys, set(contract.PLAN_KEYS_DECIDE))
        self.assertTrue(legacy_keys <= set(plan))
        self.assertEqual(plan["source_keys"], [[self.ctx.chat_id, 42]])
        self.assertEqual(plan["present"], issue("object", "observed")["fields"])
        self.assertEqual(plan["run_id"], 8)
        with closing(Store(":memory:", clock=lambda: NOW)) as store:
            pending = RulesPendingStore(store)
            self.assertTrue(pending.save_batch("decide-test", fingerprint, [plan]))
            self.assertEqual(pending.batch("decide-test"), (fingerprint, [(0, plan, "PLANNED")]))

    def test_draft_pack_matches_active_sources(self):
        listener = Path(__file__).resolve().parents[1]
        draft = load_pack(listener / "assistant" / "drafts" / "decide-v1", listener / "rules")
        self.assertNotEqual(draft.fingerprint, _load_assistant_pack().fingerprint)


class ConfigTests(unittest.TestCase):
    def load(self, extra):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(EXAMPLE.read_text(encoding="utf-8") + "\n" + extra, encoding="utf-8")
            return load_config(path, {"TG_API_ID": "1", "TG_API_HASH": FAKE_HASH})

    def test_config_defaults_and_check_config_dump(self):
        public = self.load("").redacted()["assistant"]
        self.assertEqual(public["authority"], "propose")
        self.assertEqual(public["decide_chat_ids"], [])
        self.assertEqual(public["append_confidence"], 0.8)

    def test_config_decide_requires_enabled_and_live(self):
        for extra in ('authority = "decide"', 'authority = "decide"\nmode = "live"\nenabled = false',
                      'authority = "decide"\nmode = "shadow"\nenabled = true', 'authority = "typo"'):
            with self.subTest(extra=extra), self.assertRaisesRegex(ConfigError, "assistant.authority"):
                self.load(extra)

    def test_config_decide_chat_ids(self):
        with patch('tg_triage.config._assistant_token', return_value='test'):
            settings = self.load('authority = "decide"\nmode = "live"\nenabled = true\n'
                                 'decide_chat_ids = [-1001, -1002]')
        self.assertEqual(settings.assistant.authority, "decide")
        self.assertEqual(settings.assistant.decide_chat_ids, (-1001, -1002))
        self.assertEqual(settings.redacted()["assistant"]["decide_chat_ids"], [-1001, -1002])
        for value in ('[true]', '["-1001"]', '[1.5]', '-1001'):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "assistant.decide_chat_ids"):
                self.load(f"decide_chat_ids = {value}")

    def test_config_append_confidence(self):
        for value in (0.5, 0.8, 1):
            with self.subTest(value=value):
                self.assertEqual(self.load(f"append_confidence = {value}").assistant.append_confidence, value)
        for value in ('0.499', '1.01', 'nan', 'inf', 'true', '"0.8"'):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "assistant.append_confidence"):
                self.load(f"append_confidence = {value}")

    def test_pack_mode_requires_decide_and_defaults_to_full(self):
        self.assertEqual(self.load("").redacted()["assistant"]["pack_mode"], "full")
        for mode in ('"core"', '"small"', 'true'):
            with self.subTest(mode=mode), self.assertRaisesRegex(ConfigError, "assistant.pack_mode"):
                self.load(f"pack_mode = {mode}")
        with patch('tg_triage.config._assistant_token', return_value='test'):
            settings = self.load('enabled = true\nmode = "live"\nauthority = "decide"\npack_mode = "core"')
        self.assertEqual(settings.redacted()["assistant"]["pack_mode"], "core")


class ProposeCompatibilityTests(unittest.TestCase):
    def test_propose_ab_byte_identity_decisions_runs_and_calls(self):
        # Same fixed-clock production seam as test_assistant_integration, with
        # the startup pack fingerprint, a live ticket write and an ops notice.
        snapshot = _load_decision_rules()
        pack = _load_assistant_pack()
        results = []
        for assistant in (AssistantConfig(), AssistantConfig(authority="propose")):
            with closing(Store(":memory:", clock=lambda: NOW)) as store:
                store.assistant_pack_fingerprint = pack.fingerprint
                settings = config(mode="live")
                settings = replace(settings, autonomous_enabled=True, decision_rules_enabled=True,
                                   assistant=assistant, telegram=replace(settings.telegram, ops_chat_id=-1009876543210))
                github, telegram = FakeGitHub(), Mock(return_value=123)
                pipeline = Pipeline(settings, store, Provider(), github, rules_snapshot=snapshot,
                                    timer=lambda: 0, notice_sender=telegram)
                source = message(42, text="POS printer jammed")
                store.enqueue(source)
                with patch("tg_triage.router_transport.chat", side_effect=AssertionError("disabled")) as chat, \
                     patch("time.monotonic", return_value=0):
                    decision = pipeline.process(source)
                    due = pipeline.close_due_cases()
                self.assertIsNone(pipeline.assistant_runtime)
                chat.assert_not_called()
                added = ("code_precheck_json", "log_thread_id")
                rows = []
                for row in store.connection.execute("SELECT * FROM classification_runs ORDER BY run_id"):
                    self.assertEqual(tuple(row[column] for column in added), (None, None))
                    rows.append({column: row[column] for column in row.keys() if column not in added})
                self.assertTrue(rows)
                self.assertTrue(github.calls)
                self.assertTrue(telegram.called)
                results.append(json.dumps({
                    "decision": asdict(decision), "due": [asdict(item) for item in due],
                    "runs": rows, "github": [asdict(item) for item in github.calls],
                    "telegram": [(call.args, call.kwargs) for call in telegram.call_args_list],
                }, sort_keys=True).encode("utf-8"))
        self.assertEqual(*results)
        self.assertEqual(hashlib.sha256(results[0]).hexdigest(),
                         "37f60d71d13f0d2ed94f27124e5a434c19e1c725276ba14a1deab89b0c7f110d")
        self.assertEqual(snapshot.fingerprint, "10681d80e99b0a3610ec4c380e8dba9b09b2414f0eed2d1f3c78dc6bc1400a21")
        self.assertEqual(pack.fingerprint, "35c72d95ea7017cf56f31a799defdc4a9122d3944b88c4e99ba0e76928c83e67")
