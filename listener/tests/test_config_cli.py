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
from tests.helpers import (EXAMPLE, FAKE_HASH, FAKE_SECRET_TOKEN, FAKE_TG_SECRETS,
                           FAKE_TOKEN, classification, config, load_with_test_secrets,
                           message, write_test_config)


class ConfigTests(unittest.TestCase):
    def load_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = write_test_config(Path(directory) / "config.toml", text)
            return load_config(path, {})

    def test_example_defaults_load_without_github_credentials(self):
        value = load_with_test_secrets(EXAMPLE)
        self.assertEqual(value.mode, "dry-run")
        self.assertEqual(value.gh_token, "")
        self.assertEqual(value.telegram.chat_ids, (-1001234567890,))
        self.assertEqual(value.ollama.model, "qwen3.5:9b")

    def test_missing_and_invalid_required_environment(self):
        for values, field in (({}, "tg-api-id"), ({"tg-api-id": "123"}, "tg-api-hash"),
                              ({**FAKE_TG_SECRETS, "tg-api-id": "0"}, "tg-api-id"),
                              ({**FAKE_TG_SECRETS, "tg-api-id": "no"}, "tg-api-id"),
                              ({**FAKE_TG_SECRETS, "tg-api-hash": "fake"}, "tg-api-hash")):
            with self.subTest(field=field), self.assertRaisesRegex(ConfigError, field):
                load_with_test_secrets(EXAMPLE, credentials=values)

    def test_live_requires_token_and_repository(self):
        for values, field in ((FAKE_TG_SECRETS, "gh-token"),
                              ({**FAKE_TG_SECRETS, "gh-token": FAKE_SECRET_TOKEN}, "GH_REPO")):
            with self.assertRaisesRegex(ConfigError, field):
                load_with_test_secrets(EXAMPLE, {"TG_TRIAGE_MODE": "live"}, credentials=values)

    def test_bad_repository_formats_are_rejected_without_echo(self):
        for repo in ("owner", "owner/repo/extra", "https://github.com/owner/repo", "owner/repo?token=ghp_TEST", "owner/repo\n"):
            with self.assertRaises(ConfigError) as error:
                load_with_test_secrets(EXAMPLE, {"TG_TRIAGE_MODE": "live", "GH_REPO": repo},
                                       credentials={**FAKE_TG_SECRETS, "gh-token": FAKE_SECRET_TOKEN})
            self.assertEqual(str(error.exception), "GH_REPO: expected owner/repo")

    def test_mode_override_and_invalid_override(self):
        env = {"TG_TRIAGE_MODE": "live", "GH_REPO": "sandbox/triage"}
        values = {**FAKE_TG_SECRETS, "gh-token": FAKE_SECRET_TOKEN}
        self.assertEqual(load_with_test_secrets(EXAMPLE, env, credentials=values).mode, "live")
        with self.assertRaisesRegex(ConfigError, "mode"):
            load_with_test_secrets(EXAMPLE, {"TG_TRIAGE_MODE": "other"})

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
        value = load_with_test_secrets(EXAMPLE, credentials={**FAKE_TG_SECRETS, "gh-token": FAKE_SECRET_TOKEN})
        output = repr(value) + json.dumps(value.redacted())
        self.assertNotIn(FAKE_HASH, output)
        self.assertNotIn(FAKE_SECRET_TOKEN, output)
        self.assertEqual(value.redacted()["api_hash"], {"length": 32})


class CLITests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("tg_triage.__main__.validate_data_directories"))
        self.enterContext(patch("tg_triage.__main__.ProcessLock", return_value=contextlib.nullcontext()))

    def test_check_config_never_prints_credentials(self):
        output, errors = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = write_test_config(Path(directory) / "config.toml", EXAMPLE.read_text(encoding="utf-8"),
                                     credentials={**FAKE_TG_SECRETS, "gh-token": FAKE_SECRET_TOKEN})
            with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = main(["check-config", "--config", str(path)])
        self.assertEqual(code, 0)
        self.assertNotIn(FAKE_HASH, output.getvalue() + errors.getvalue())
        self.assertNotIn(FAKE_SECRET_TOKEN, output.getvalue() + errors.getvalue())
        self.assertEqual(json.loads(output.getvalue())["api_hash"], {"length": 32})
        self.assertNotIn("key_file", json.loads(output.getvalue())["model"])

    def test_check_config_invalid_returns_nonzero_and_safe_field_message(self):
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = write_test_config(Path(directory) / "config.toml", EXAMPLE.read_text(encoding="utf-8"),
                                     credentials={})
            with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(errors):
                code = main(["check-config", "--config", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("tg-api-id: missing", errors.getvalue())

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
