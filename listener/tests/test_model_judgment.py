"""Assistant definition 12.12: the model judges, code keeps only the safety locks. Offline: fake model
(fake Ollama HTTP or fake classifiers), fake GitHub, in-memory or temporary SQLite. No network."""

import csv
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tg_triage.classifier import (CASE_SCHEMA, TOPIC_SCHEMA, Classifier, parse_case_analysis,
                                  parse_topic_groups, strip_thinking)
from tg_triage.config import ConfigError, ModelConfig, load_config
from tg_triage.models import CaseAnalysis, Decision, clean_reason
from tg_triage.pipeline import Pipeline
from tg_triage.provider import ModelProvider, OllamaProvider, build_provider
from tg_triage.store import COLUMNS, Store
from tests.helpers import (EXAMPLE, FAKE_HASH, NOW, FakeCaseModel, FakeGitHub, FakeHTTP, FakeTopicModel,
                           NumberedGitHub, classification, config, message, ticket, topic)
from tests.test_cases import CHAT, CaseBase, msg
from tests.test_topics import TopicBase

THINKING = "SUY-LUAN-RIENG-TU nguoi nay co ve muon bao loi, toi nghi day la bug 0901234567"
REASON = "LY-DO-CHO-NGUOI-DUYET vi tin goi truc tiep va mo ta loi"


def group_json(ids, **extra):
    """One topic group as the new prompt asks for it: no `verdict`, with actionable and reason."""
    group = {"topic": "Đăng nhập", "message_ids": ids, "category": "bug", "type": "bug", "actionable": True,
             "title": "Lỗi đăng nhập", "summary": "Không đăng nhập được.", "priority": "high",
             "confidence": 0.9, "missing_info": [], "reason": REASON}
    group.update(extra)
    return json.dumps({"groups": [group]}, ensure_ascii=False)


def envelope(content, thinking=THINKING):
    """What Ollama returns with `think: true`: the reasoning next to the JSON answer."""
    return {"message": {"role": "assistant", "thinking": thinking, "content": content}}


def provider_pipeline(test, *responses, mode="live", limit=20, window=0):
    store = Store(":memory:")
    test.addCleanup(store.close)
    test.http = FakeHTTP(*responses)
    test.provider = OllamaProvider(config().ollama, opener=test.http, prompt="fixed", case_prompt="CASE",
                                   topics_prompt="TOPICS")
    test.github = NumberedGitHub()
    test.store = store
    return Pipeline(config(mode, limit, window=window), store, test.provider, test.github)


class SafetyLockTests(unittest.TestCase):
    """The locks that no model answer can open, through the real provider and a fake Ollama."""

    # 12.13 changed this test (was: a no-tag group the model calls a bug is never an issue). A confident
    # bug needs no tag: it is an automatic issue (`auto_bug`). Below the threshold it waits for review.
    def test_a_no_tag_group_the_model_calls_a_bug_is_an_automatic_issue(self):
        pipeline = provider_pipeline(self, envelope(group_json([1], confidence=0.99)))
        result = pipeline.process(message(1, "không đăng nhập được app nexora", mentions_listener=False))
        row = self.store.get(CHAT, 1)
        self.assertEqual((result.decision, row["decision"], row["reason"]), ("issue", "issue", "auto_bug"))
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(row["issue_number"], 101)
        self.assertEqual(row["model_reason"], REASON)

    def test_a_no_tag_group_the_model_is_not_sure_is_a_bug_asks_for_clarification(self):
        pipeline = provider_pipeline(self, envelope(group_json([1], confidence=0.5)))
        result = pipeline.process(message(1, "không đăng nhập được app nexora", mentions_listener=False))
        row = self.store.get(CHAT, 1)
        self.assertEqual((result.decision, row["reason"]), ("needs_human", "auto_needs_human"))
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)
        self.assertEqual(row["issue_number"], 101)
        self.assertEqual(self.github.calls[0].labels, ("needs-human", "source:telegram"))
        self.assertEqual(row["type"], "bug")                     # what the model thought is kept for the reviewer
        self.assertEqual(row["model_reason"], REASON)

    # 12.13 changed this test (was: a command the model calls chat is kept for review with a warning).
    # A /command with content is a forced issue whatever the model said; the command's own type is kept.
    def test_a_command_the_model_calls_chat_is_still_a_forced_issue(self):
        pipeline = provider_pipeline(self, envelope(group_json(
            [1, 2], category="chat", type="task", actionable=False, reason="Chỉ là trao đổi thường.")),
            window=15)
        pipeline.process(msg(1, 0, text="hôm nay họp mấy giờ nhỉ", mentions_listener=False))
        pipeline.process(msg(2, 1, text="/bug nút lưu không bấm được", mentions_listener=False))
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.github.calls[0].labels[0], "bug")
        self.assertEqual([self.store.get(CHAT, i)["decision"] for i in (1, 2)], ["issue"] * 2)
        self.assertEqual(self.store.get(CHAT, 2)["reason"], "forced_command")
        self.assertEqual(self.store.get(CHAT, 2)["model_reason"], "Chỉ là trao đổi thường.")

    def test_a_tagged_message_the_model_calls_a_task_is_one_issue(self):
        pipeline = provider_pipeline(self, envelope(group_json(
            [1], category="task", type="task", title="Mời anh vào repo")))
        result = pipeline.process(message(1, "@vlink_bot invite anh vào repo đi", mentions_listener=True))
        self.assertEqual(result.decision, "issue")
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.github.calls[0].labels, ("task", "triage", "source:telegram"))
        self.assertEqual(self.store.get(CHAT, 1)["issue_number"], 101)

    # 12.13 changed this test (was: a tagged message the model finds unclear is kept for review).
    # A tag forces an issue: an unclear message gives a needs-human issue (`forced_mention`).
    def test_a_tagged_message_the_model_finds_unclear_is_a_needs_human_issue(self):
        pipeline = provider_pipeline(self, envelope(group_json(
            [1], actionable=False, confidence=0.3, missing_info=["Bạn cần gì?"])))
        pipeline.process(message(1, "@vlink_bot cái này sao vậy", mentions_listener=True))
        row = self.store.get(CHAT, 1)
        self.assertEqual((row["decision"], row["reason"]), ("needs_human", "forced_mention"))
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)

    def test_a_no_tag_message_the_model_finds_unclear_asks_for_clarification(self):
        pipeline = provider_pipeline(self, envelope(group_json(
            [1], actionable=False, confidence=0.3, missing_info=["Bạn cần gì?"])))
        pipeline.process(message(1, "cái này sao vậy", mentions_listener=False))
        row = self.store.get(CHAT, 1)
        self.assertEqual((row["decision"], row["reason"]), ("needs_human", "auto_needs_human"))
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)

    def test_a_command_the_model_finds_unclear_is_still_a_ticket_for_a_human(self):
        pipeline = provider_pipeline(self, envelope(group_json([1, 2], actionable=False, confidence=0.3)),
                                     window=15)
        pipeline.process(msg(1, 0, text="cái này lạ quá"))
        pipeline.process(msg(2, 1, text="/bug nút lưu lỗi"))
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)
        self.assertEqual(self.github.calls[0].labels[0], "bug")

    def test_the_hourly_issue_limit_still_holds_for_model_tickets(self):
        pipeline = provider_pipeline(self, envelope(group_json([1])), envelope(group_json([2])), limit=1)
        with self.assertLogs("tg_triage.pipeline", level="WARNING"):
            first = pipeline.process(message(1, "lỗi một", mentions_listener=True))
            second = pipeline.process(message(2, "lỗi hai", mentions_listener=True, sender_id=2))
        self.assertEqual((first.decision, second.decision), ("issue", "rate_limited"))
        self.assertEqual(len(self.github.calls), 1)

    def test_dry_run_never_calls_github_and_still_records_the_model_reason(self):
        pipeline = provider_pipeline(self, envelope(group_json([1])), mode="dry-run")
        result = pipeline.process(message(1, "không đăng nhập được app", mentions_listener=True))
        self.assertEqual(result.decision, "dry_run_issue")
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.store.get(CHAT, 1)["model_reason"], REASON)

    def test_a_window_of_all_three_outcomes_reconciles(self):
        answer = json.dumps({"groups": [
            json.loads(group_json([1]))["groups"][0],
            json.loads(group_json([2], category="chat", type="task", actionable=False))["groups"][0],
            json.loads(group_json([3], confidence=0.5))["groups"][0]]}, ensure_ascii=False)
        pipeline = provider_pipeline(self, envelope(answer), window=15)
        # 12.13: the third group is a bug the model is not sure about and nobody tagged: pending_review.
        for index, (text, tagged) in enumerate((("app crash khi mở", True), ("trưa nay ăn gì", False),
                                                 ("app crash lần nữa", False)), 1):
            pipeline.process(msg(index, index, text=text, mentions_listener=tagged))
        pipeline.close_due_cases()
        tally = self.store.connection.execute("SELECT * FROM reconciliation").fetchone()
        self.assertEqual((tally["received"], tally["issue"], tally["chat"], tally["pending_review"],
                          tally["balanced"], tally["status"]), (3, 1, 1, 0, 1, "done"))


class ThinkingTests(unittest.TestCase):
    def ask(self, content, *, thinking=THINKING, think=True):
        http = FakeHTTP(envelope(content, thinking))
        provider = OllamaProvider(config().ollama, opener=http, prompt="fixed", topics_prompt="TOPICS",
                                  case_prompt="CASE", think=think)
        return provider, http

    def test_the_request_turns_reasoning_on_by_default_and_off_when_configured(self):
        for think in (True, False):
            with self.subTest(think=think):
                provider, http = self.ask(group_json([1]), think=think)
                provider.classify_topics([message(1, "lỗi")])
                self.assertIs(json.loads(http.calls[0][0].data)["think"], think)
        self.assertIs(OllamaProvider(config().ollama, prompt="x").think, True)

    def test_the_parser_ignores_the_thinking_field_of_the_envelope(self):
        provider, _ = self.ask(group_json([1]))
        groups = provider.classify_topics([message(1, "lỗi")])
        self.assertEqual((groups[0].category, groups[0].message_ids, groups[0].reason), ("bug", (1,), REASON))
        self.assertNotIn("SUY-LUAN", repr(groups))

    def test_the_case_request_works_with_thinking_too(self):
        content = json.dumps({"category": "bug", "type": "bug", "actionable": True, "title": "T",
                              "summary": "S", "priority": "low", "confidence": 0.9, "missing_info": [],
                              "reason": REASON, "key_message_ids": [1]})
        provider, http = self.ask(content)
        value = provider.analyze_case([message(1, "lỗi")])
        self.assertEqual((value.verdict, value.category, value.actionable, value.reason),
                         ("ticket", "bug", True, REASON))
        self.assertIs(json.loads(http.calls[0][0].data)["think"], True)
        self.assertEqual(json.loads(http.calls[0][0].data)["format"], CASE_SCHEMA)

    def test_a_think_block_left_inside_content_and_a_thinking_key_in_the_json_are_ignored(self):
        content = "<think>\nđang nghĩ " + THINKING + "\n</think>\n" + group_json([1])
        self.assertEqual(parse_topic_groups(content)[0].message_ids, (1,))
        self.assertEqual(strip_thinking("<think>x</think>  {}"), "{}")
        extra = json.loads(group_json([1]))
        extra["thinking"] = THINKING
        extra["groups"][0]["thinking"] = THINKING
        self.assertEqual(parse_topic_groups(json.dumps(extra))[0].message_ids, (1,))
        self.assertNotIn("SUY-LUAN", repr(parse_topic_groups(json.dumps(extra))))

    def test_an_answer_with_only_thinking_and_no_json_content_is_not_usable(self):
        provider, _ = self.ask("")
        self.assertIsNone(provider.classify_topics([message(1, "lỗi")]))

    def test_the_reasoning_never_reaches_the_logs_the_issue_or_sqlite(self):
        pipeline = provider_pipeline(self, envelope(group_json([1])), envelope("not json"),
                                     envelope("not json"))
        with self.assertLogs(level="DEBUG") as logs:
            first = pipeline.process(message(1, "không đăng nhập được", mentions_listener=True))
            # A bad answer (retried once, then the needs-human fallback) must not leak it either.
            second = pipeline.process(message(2, "lỗi khác nữa", mentions_listener=True, sender_id=2))
        self.assertEqual((first.decision, second.decision), ("issue", "needs_human"))
        self.assertNotIn("SUY-LUAN", "\n".join(logs.output))
        self.assertNotIn("LY-DO", "\n".join(logs.output))
        for issue in self.github.calls:
            self.assertNotIn("SUY-LUAN", issue.title + issue.body)
        dump = repr([tuple(row) for row in self.store.connection.execute("SELECT * FROM messages")])
        self.assertNotIn("SUY-LUAN", dump)
        self.assertNotIn("0901234567", dump)


class ReasonTests(TopicBase):
    def test_the_reason_is_stored_and_exported_but_never_put_in_the_issue(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1], reason=REASON), topic(
            [2], verdict="noise", category="chat", reason="Chỉ là xã giao."))))
        pipeline.process(msg(1, 1, text="app crash khi mở", mentions_listener=True))
        pipeline.process(msg(2, 2, text="cảm ơn nhé", mentions_listener=False))
        self.close(pipeline)
        issue = self.github.calls[0]
        self.assertNotIn("LY-DO", issue.title + issue.body)
        self.assertEqual([self.row(i)["model_reason"] for i in (1, 2)], [REASON, "Chỉ là xã giao."])
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "review.csv"
            self.store.export_csv(out)
            rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
        self.assertIn("model_reason", COLUMNS)
        self.assertEqual({row["message_id"]: row["model_reason"] for row in rows},
                         {"1": REASON, "2": "Chỉ là xã giao."})

    def test_a_group_reason_is_stored_on_every_message_of_the_group(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1, 2], reason=REASON),)))
        for index in (1, 2):
            pipeline.process(msg(index, index, text=f"app crash lần {index}"))
        self.close(pipeline)
        self.assertEqual([self.row(i)["model_reason"] for i in (1, 2)], [REASON, REASON])

    def test_clean_reason_bounds_and_rejects_non_text(self):
        self.assertEqual(clean_reason("  một \n dòng  "), "một dòng")
        self.assertEqual(len(clean_reason("x" * 1000)), 300)
        for value in (None, 5, "", "   ", ["a"], "bad\ud83d"):
            self.assertIsNone(clean_reason(value))

    def test_a_decision_without_a_reason_stores_null(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.claim(message(1))
        store.finish(message(1), Decision("noise", "rule", classification(type="noise")))
        self.assertIsNone(store.get(CHAT, 1)["model_reason"])


class MigrationTests(unittest.TestCase):
    OLD = '''CREATE TABLE messages (
        chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, received_at TEXT NOT NULL,
        sender_name TEXT NOT NULL, text TEXT NOT NULL, tier TEXT NOT NULL,
        type TEXT, confidence REAL, title TEXT, summary TEXT, priority TEXT,
        decision TEXT NOT NULL, issue_number INTEGER, issue_url TEXT, error TEXT,
        processed_at TEXT, latency_ms INTEGER, github_called_at TEXT, chat_username TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, case_key TEXT, sender_id INTEGER, reply_to_id INTEGER,
        is_forward INTEGER NOT NULL DEFAULT 0, has_image INTEGER NOT NULL DEFAULT 0,
        image_size INTEGER, image_mime TEXT, group_key TEXT, reason TEXT,
        reacted_received INTEGER NOT NULL DEFAULT 0, reacted_ok INTEGER NOT NULL DEFAULT 0,
        mentions_listener INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, message_id))'''

    def old_database(self, directory):
        path = Path(directory) / "old.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute(self.OLD)
        connection.execute('''INSERT INTO messages (chat_id, message_id, received_at, sender_name, text, tier,
            decision, reason) VALUES (-5, 1, ?, 'Cũ', 'tin cũ', 'model', 'pending_review', 'no_direct_call')''',
                           (NOW.isoformat(),))
        connection.commit()
        connection.close()
        return path

    def test_an_old_database_gets_the_model_reason_column_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.old_database(directory)
            store = Store(path, clock=lambda: NOW)
            try:
                columns = {row[1] for row in store.connection.execute("PRAGMA table_info(messages)")}
                self.assertIn("model_reason", columns)
                old = store.get(-5, 1)
                self.assertEqual((old["decision"], old["reason"], old["model_reason"]),
                                 ("pending_review", "no_direct_call", None))
                new = message(2, "tin mới", chat_id=-5)
                store.claim(new)
                store.finish(new, Decision("pending_review", "model", classification(), reason="no_direct_call",
                                           model_reason="Vì sao"))
                self.assertEqual(store.get(-5, 2)["model_reason"], "Vì sao")
            finally:
                store.close()
            Store(path).close()   # an already migrated database reopens cleanly

    def test_read_only_export_of_an_old_database_has_an_empty_model_reason_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.old_database(directory)
            reader = Store(path, read_only=True)
            try:
                out = Path(directory) / "out.csv"
                reader.export_csv(out)
            finally:
                reader.close()
            rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
        self.assertEqual((rows[0]["decision"], rows[0]["model_reason"]), ("pending_review", ""))


class ProviderConfigTests(unittest.TestCase):
    ENV = {"TG_API_ID": "123", "TG_API_HASH": FAKE_HASH}

    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path, self.ENV)

    @staticmethod
    def example(**replacements):
        text = EXAMPLE.read_text(encoding="utf-8")
        for old, new in replacements.items():
            assert old in text, old
            text = text.replace(old, new)
        return text

    def test_the_example_declares_the_model_table(self):
        value = load_config(EXAMPLE, self.ENV)
        self.assertEqual(value.model, ModelConfig("ollama", "qwen3.5:9b", True, 600.0))
        self.assertEqual((value.ollama.model, value.ollama.timeout_seconds), ("qwen3.5:9b", 600.0))

    def test_defaults_without_a_model_table_keep_the_older_ollama_keys(self):
        text = self.example().replace("[model]", "[ignored_model]")
        text = text.replace('[ollama]\n', '[ollama]\nmodel = "other:1b"\ntimeout_seconds = 90\n', 1)
        value = self.load(text)
        self.assertEqual(value.model, ModelConfig("ollama", "other:1b", True, 90.0))

    def test_timeout_defaults_to_600_when_nothing_sets_it(self):
        text = self.example().replace("timeout_seconds = 600\n", "")
        self.assertEqual(self.load(text).model.timeout_seconds, 600.0)

    def test_model_table_overrides_and_think_can_be_turned_off(self):
        text = self.example(**{'model = "qwen3.5:9b"': 'model = "qwen3:4b"', "think = true": "think = false",
                               "timeout_seconds = 600": "timeout_seconds = 900"})
        value = self.load(text)
        self.assertEqual(value.model, ModelConfig("ollama", "qwen3:4b", False, 900.0))
        self.assertEqual((value.ollama.model, value.ollama.timeout_seconds), ("qwen3:4b", 900.0))

    def test_provider_claude_is_refused_with_a_clear_message(self):
        with self.assertRaises(ConfigError) as error:
            self.load(self.example(**{'provider = "ollama"': 'provider = "claude"'}))
        self.assertIn("chưa hỗ trợ", str(error.exception))
        self.assertIn("model.provider", str(error.exception))

    def test_an_unknown_provider_and_bad_values_are_refused_without_echo(self):
        for change, field in (({'provider = "ollama"': 'provider = "ghp_TEST"'}, "model.provider"),
                              ({'provider = "ollama"': "provider = 5"}, "model.provider"),
                              ({"think = true": 'think = "yes"'}, "model.think"),
                              ({"timeout_seconds = 600": "timeout_seconds = 0"}, "model.timeout_seconds")):
            with self.subTest(change=change):
                with self.assertRaises(ConfigError) as error:
                    self.load(self.example(**change))
                self.assertIn(field, str(error.exception))
                self.assertNotIn("ghp_TEST", str(error.exception))

    def test_build_provider_returns_the_ollama_provider_with_the_configured_think(self):
        settings = config()
        provider = build_provider(settings, prompt="x", topics_prompt="y")
        self.assertIsInstance(provider, OllamaProvider)
        self.assertIsInstance(provider, ModelProvider)
        self.assertEqual((provider.name, provider.think), ("ollama", True))
        quiet = build_provider(replace(settings, model=ModelConfig(think=False)), prompt="x")
        self.assertIs(quiet.think, False)

    def test_build_provider_refuses_claude_even_when_the_config_object_was_built_in_code(self):
        with self.assertRaisesRegex(ConfigError, "chưa hỗ trợ"):
            build_provider(replace(config(), model=ModelConfig(provider="claude")))

    def test_the_provider_interface_is_minimal_and_complete(self):
        for name in ("classify_topics", "analyze_case"):
            self.assertTrue(callable(getattr(OllamaProvider, name)))
            with self.assertRaises(NotImplementedError):
                getattr(ModelProvider(), name)([])

    def test_classify_topics_takes_a_transcript_string_as_well_as_messages(self):
        http = FakeHTTP(envelope(group_json([1])), envelope(group_json([1])))
        provider = OllamaProvider(config().ollama, opener=http, prompt="x", topics_prompt="TOPICS")
        by_messages = provider.classify_topics([message(1, "lỗi")])
        by_text = provider.classify_topics("[1] A · 2026-10-12 09:00: lỗi")
        self.assertEqual(by_messages[0].message_ids, by_text[0].message_ids)
        self.assertEqual(json.loads(http.calls[1][0].data)["messages"][1]["content"],
                         "[1] A · 2026-10-12 09:00: lỗi")


class ParserDerivationTests(unittest.TestCase):
    """12.12: `verdict` is optional; the parser derives it from category and actionable."""

    @staticmethod
    def one(**fields):
        group = json.loads(group_json([1]))["groups"][0]
        group.update(fields)
        return parse_topic_groups(json.dumps({"groups": [group]}))

    def test_category_and_actionable_give_the_verdict(self):
        self.assertEqual(self.one()[0].verdict, "ticket")
        self.assertEqual(self.one(category="chat", actionable=False)[0].verdict, "noise")
        self.assertEqual(self.one(category="chat")[0].verdict, "noise")
        self.assertEqual(self.one(actionable=False)[0].verdict, "needs_human")
        self.assertEqual(self.one(category="feature")[0].category, "feedback")

    def test_an_explicit_ticket_verdict_is_downgraded_when_not_actionable(self):
        self.assertEqual(self.one(verdict="ticket", actionable=False)[0].verdict, "needs_human")
        self.assertEqual(self.one(verdict="ticket")[0].verdict, "ticket")
        self.assertEqual(self.one(verdict="noise")[0].verdict, "noise")

    def test_no_verdict_and_no_usable_category_is_not_usable(self):
        self.assertIsNone(self.one(category="other"))
        self.assertIsNone(self.one(verdict="issue"))

    def test_actionable_and_reason_are_read_with_bounds(self):
        group = self.one(reason="x" * 1000, actionable="yes")[0]
        self.assertIsNone(group.actionable)           # only a real boolean counts
        self.assertEqual(len(group.reason), 300)
        self.assertEqual(self.one(reason=7)[0].reason, None)

    def test_case_analysis_derives_the_verdict_and_reads_the_new_fields(self):
        base = {"category": "bug", "type": "bug", "actionable": True, "title": "T", "summary": "S",
                "priority": "low", "confidence": 0.9, "missing_info": ["Máy nào?"], "reason": "R",
                "key_message_ids": [1]}
        value = parse_case_analysis(json.dumps(base))
        self.assertEqual((value.verdict, value.category, value.actionable, value.reason, value.missing_info),
                         ("ticket", "bug", True, "R", ("Máy nào?",)))
        chat = parse_case_analysis(json.dumps({**base, "category": "chat", "actionable": False}))
        self.assertEqual((chat.verdict, chat.classification.type), ("noise", "noise"))
        unclear = parse_case_analysis(json.dumps({**base, "actionable": False}))
        self.assertEqual((unclear.verdict, unclear.classification), ("needs_human", None))
        self.assertIsNone(parse_case_analysis(json.dumps({**base, "category": "other"})))
        self.assertIsNone(parse_case_analysis(json.dumps({**base, "missing_info": "x"})))
        self.assertEqual(parse_case_analysis(json.dumps({**base, "thinking": THINKING})).verdict, "ticket")

    def test_the_schemas_ask_for_the_12_12_fields(self):
        item = TOPIC_SCHEMA["properties"]["groups"]["items"]
        for schema in (item, CASE_SCHEMA):
            for name in ("category", "actionable", "reason", "missing_info", "confidence"):
                self.assertIn(name, schema["required"])
            self.assertEqual(schema["properties"]["category"]["enum"], ["bug", "task", "feedback", "chat"])
            self.assertEqual(schema["properties"]["actionable"], {"type": "boolean"})


class ClassifierInterfaceTests(TopicBase):
    def test_a_classifier_with_only_classify_topics_works_in_the_pipeline(self):
        class Provider:
            calls = []

            def classify_topics(self, messages):
                self.calls.append([item.message_id for item in messages])
                return (topic([item.message_id for item in messages]),)

        pipeline = self.pipeline(Provider())
        pipeline.process(msg(1, 1, text="app crash khi mở"))
        self.close(pipeline)
        self.assertEqual(Provider.calls, [[1]])
        self.assertEqual(self.decisions(1), ["issue"])


class SingleCaseMissingInfoTests(CaseBase):
    def test_a_case_model_ticket_that_misses_information_is_a_needs_human_ticket_with_questions(self):
        analysis = CaseAnalysis("ticket", classification(), (1,), "bug", True, REASON, ("Máy nào?",))
        pipeline = self.pipeline(model=FakeCaseModel(analysis))
        pipeline.process(msg(1, 0, text="app lỗi đăng nhập"))
        self.clock.advance(60)
        pipeline.close_due_cases()
        issue = self.github.calls[0]
        self.assertIn("needs-human", issue.labels)
        self.assertIn("Máy nào?", issue.body)
        self.assertNotIn("LY-DO", issue.body)
        self.assertEqual(self.row(1)["model_reason"], REASON)

    def test_a_case_model_ticket_that_is_not_actionable_asks_for_clarification(self):
        # 12.13: without a tag (was: tagged, pending_review). A tagged one would be a needs-human issue.
        analysis = CaseAnalysis("ticket", classification(), (1,), "bug", False, REASON)
        pipeline = self.pipeline(model=FakeCaseModel(analysis))
        pipeline.process(msg(1, 0, text="app lỗi đăng nhập", mentions_listener=False))
        self.clock.advance(60)
        pipeline.close_due_cases()
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)
        self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]), ("needs_human", "auto_needs_human"))


if __name__ == "__main__":
    unittest.main()
