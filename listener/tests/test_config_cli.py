import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tg_triage.__main__ import main
from tg_triage.config import ConfigError, load_config
from tg_triage.models import Decision
from tg_triage.store import Store
from tests.helpers import EXAMPLE, FAKE_HASH, FAKE_TOKEN, classification, config, message


class ConfigTests(unittest.TestCase):
    def env(self, **extra):
        return {"TG_API_ID": "123", "TG_API_HASH": FAKE_HASH, **extra}

    def load_text(self, text, env=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path, self.env() if env is None else env)

    def test_example_defaults_load_without_github_credentials(self):
        value = load_config(EXAMPLE, self.env())
        self.assertEqual(value.mode, "dry-run")
        self.assertEqual(value.gh_token, "")
        self.assertEqual(value.telegram.chat_ids, (-1001234567890,))
        self.assertEqual(value.ollama.model, "qwen3.5:9b")

    def test_missing_and_invalid_required_environment(self):
        for env, field in (({}, "TG_API_ID"), ({"TG_API_ID": "123"}, "TG_API_HASH"),
                           (self.env(TG_API_ID="0"), "TG_API_ID"),
                           (self.env(TG_API_ID="no"), "TG_API_ID"),
                           (self.env(TG_API_HASH="fake"), "TG_API_HASH")):
            with self.subTest(field=field, env=env):
                with self.assertRaisesRegex(ConfigError, field):
                    load_config(EXAMPLE, env)

    def test_live_requires_token_and_repository(self):
        for env, field in ((self.env(TG_TRIAGE_MODE="live"), "GH_TOKEN"),
                           (self.env(TG_TRIAGE_MODE="live", GH_TOKEN=FAKE_TOKEN), "GH_REPO")):
            with self.assertRaisesRegex(ConfigError, field):
                load_config(EXAMPLE, env)

    def test_bad_repository_formats_are_rejected_without_echo(self):
        for repo in ("owner", "owner/repo/extra", "https://github.com/owner/repo", "owner/repo?token=ghp_TEST", "owner/repo\n"):
            with self.assertRaises(ConfigError) as error:
                load_config(EXAMPLE, self.env(TG_TRIAGE_MODE="live", GH_TOKEN=FAKE_TOKEN, GH_REPO=repo))
            self.assertEqual(str(error.exception), "GH_REPO: expected owner/repo")

    def test_mode_override_and_invalid_override(self):
        env = self.env(TG_TRIAGE_MODE="live", GH_TOKEN=FAKE_TOKEN, GH_REPO="sandbox/triage")
        self.assertEqual(load_config(EXAMPLE, env).mode, "live")
        for mode in ("other",):
            with self.assertRaisesRegex(ConfigError, "mode"):
                load_config(EXAMPLE, self.env(TG_TRIAGE_MODE=mode))

    def test_empty_chats_and_invalid_numeric_settings(self):
        source = EXAMPLE.read_text(encoding="utf-8")
        changes = (("[-1001234567890]", "[]"), ("[-1001234567890]", "[true]"),
                   ("[-1001234567890]", "[123]"), ("timeout_seconds = 600", "timeout_seconds = 0"),
                   ("confidence_threshold = 0.7", "confidence_threshold = 1.1"),
                   ("confidence_threshold = 0.7", "confidence_threshold = nan"),
                   ("max_issues_per_hour = 20", "max_issues_per_hour = true"),
                   ("port = 443", "port = 70000"), ("min_text_length = 8", "min_text_length = -1"))
        for original, changed in changes:
            with self.subTest(changed=changed):
                with self.assertRaises(ConfigError):
                    self.load_text(source.replace(original, changed))

    def test_bad_toml_and_missing_tables_do_not_echo_input(self):
        for text in ('mode = "ghp_TEST"\ninvalid = [', 'mode = "dry-run"'):
            with self.assertRaises(ConfigError) as error:
                self.load_text(text)
            self.assertNotIn(FAKE_TOKEN, str(error.exception))

    def test_url_rejects_credentials_and_fragments(self):
        source = EXAMPLE.read_text(encoding="utf-8")
        for url in ("file:///tmp/data", "http://user:ghp_TEST@localhost", "http://localhost/#ghp_TEST"):
            with self.assertRaisesRegex(ConfigError, "ollama.url"):
                self.load_text(source.replace("http://127.0.0.1:11434", url))

    def test_credentials_hidden_in_repr_and_redacted_dictionary(self):
        value = load_config(EXAMPLE, self.env(GH_TOKEN=FAKE_TOKEN))
        output = repr(value) + json.dumps(value.redacted())
        self.assertNotIn(FAKE_HASH, output)
        self.assertNotIn(FAKE_TOKEN, output)
        self.assertEqual(value.redacted()["api_hash"], {"length": 32})


class CLITests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("tg_triage.__main__.validate_data_directories"))
        self.enterContext(patch("tg_triage.__main__.ProcessLock", return_value=contextlib.nullcontext()))

    def test_check_config_never_prints_credentials(self):
        env = {"TG_API_ID": "123", "TG_API_HASH": FAKE_HASH, "GH_TOKEN": FAKE_TOKEN}
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["check-config", "--config", str(EXAMPLE)])
        self.assertEqual(code, 0)
        self.assertNotIn(FAKE_HASH, output.getvalue() + errors.getvalue())
        self.assertNotIn(FAKE_TOKEN, output.getvalue() + errors.getvalue())
        self.assertEqual(json.loads(output.getvalue())["api_hash"], {"length": 32})

    def test_check_config_invalid_returns_nonzero_and_safe_field_message(self):
        errors = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(errors):
            code = main(["check-config", "--config", str(EXAMPLE)])
        self.assertEqual(code, 2)
        self.assertIn("TG_API_ID", errors.getvalue())

    def test_export_command_writes_owner_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path, out = Path(directory) / "db.sqlite3", Path(directory) / "review.csv"
            store = Store(path)
            store.claim(message())
            store.finish(message(), Decision("dry_run_issue", "model", classification()))
            store.close()
            self.assertEqual(main(["export-csv", "--db", str(path), "--out", str(out)]), 0)
            self.assertTrue(out.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn("owner_label", out.read_text(encoding="utf-8-sig"))

    def test_dry_run_cli_does_not_construct_github(self):
        with patch("tg_triage.__main__.load_config", return_value=config()), \
                patch("tg_triage.__main__.GitHubIssues") as github, \
                patch("tg_triage.telegram_io.run", new_callable=AsyncMock) as transport:
            self.assertEqual(main(["run"]), 0)
            github.assert_not_called()
            transport.assert_awaited_once()

    def test_export_command_does_not_recover_an_inflight_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path, out = Path(directory) / "db.sqlite3", Path(directory) / "review.csv"
            store = Store(path)
            try:
                store.claim(message())
                store.start_classifying(message())
                store.reserve_github_call(message(), 20)
                self.assertEqual(main(["export-csv", "--db", str(path), "--out", str(out)]), 0)
                self.assertEqual(store.get(-1001234567890, 1)["decision"], "pending")
            finally:
                store.close()

    def test_terminal_transport_failure_returns_nonzero_without_raw_exception(self):
        with patch("tg_triage.__main__.load_config", return_value=config()), \
                patch("tg_triage.telegram_io.run", new_callable=AsyncMock, side_effect=RuntimeError("ghp_TEST")), \
                self.assertLogs("tg_triage.__main__", level="ERROR") as logs:
            self.assertEqual(main(["run"]), 1)
        self.assertNotIn(FAKE_TOKEN, "\n".join(logs.output))
