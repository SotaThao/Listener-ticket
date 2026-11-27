"""Fake process, file and clock checks. Never call a real external service or systemd."""

import contextlib
import json
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from tg_triage import __main__ as cli, secrets_store as secrets
from tg_triage.store import Store
from tests.helpers import FAKE_MODEL_KEY, FAKE_ROUTER_KEY, FAKE_SECRET_TOKEN
from tests.test_secrets_store import NEXT, SCOPE, SecretFixture


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 26)


def unit(pid, restarts=0, active="active"):
    return f"ActiveState={active}\nMainPID={pid}\nNRestarts={restarts}\n"


def restart_outputs(old_pid, new_pid, *, restarts=0, journal=""):
    return [unit(old_pid), "", "", unit(new_pid, restarts), unit(new_pid, restarts), journal]


class SecretCommands(SecretFixture):
    def test_nonroot_write_refuses_before_loading_config_or_prompting(self):
        for euid in (1234, None):
            platform = secrets.Platform(unix=False, euid=lambda: euid)
            for action in (("set", "gh-token"), ("rollback", "gh-token"), ("migrate", "--apply")):
                with self.subTest(action=action, euid=euid), patch.object(secrets, "PLATFORM", platform), \
                        patch.object(cli, "load_secrets_setup") as setup, patch.object(cli.getpass, "getpass") as prompt:
                    rc, out, err = self.invoke("secrets", *action)
                    self.assertEqual((rc, out), (2, ""))
                    self.assertIn("sudo", err)
                    setup.assert_not_called()
                    prompt.assert_not_called()

    def test_prompt_twice_and_refuse_mismatch_without_writing(self):
        with patch.object(cli.getpass, "getpass", side_effect=[FAKE_SECRET_TOKEN, NEXT]) as prompt:
            self.assertEqual(self.invoke("secrets", "set", "gh-token"),
                             (2, "", "ConfigError: gh-token: bad_format\n"))
        self.assertEqual(prompt.call_count, 2)
        self.assertFalse(self.audit.exists())

    def test_ac1b_status_warns_at_seven_days_today_and_after_expiry(self):
        for expires, state in (("2026-12-04", "ok"), ("2026-12-03", "expiring"),
                               ("2026-11-26", "expiring"), ("2026-11-25", "expired")):
            with self.subTest(expires=expires):
                self.assertEqual(self.install("gh-token", NEXT, "--expires", expires)[0], 0)
                with patch.object(secrets, "date", FixedDate):
                    rc, out, err = self.invoke("secrets", "status", "--json")
                    table_rc, table, table_err = self.invoke("secrets", "status")
                self.assertEqual((rc, err, table_rc, table_err), (0, "", 0, ""))
                row = next(item for item in json.loads(out) if item["name"] == "gh-token")
                self.assertEqual((row["state"], row["expires_at"]), (state, expires))
                self.assertIn(secrets.STATUS_LABELS[state], table)
                self.assertNotIn(NEXT, out + table)
        (self.directory / "tg-api-hash").unlink()
        self.assertEqual(self.invoke("secrets", "status")[0], 2)

    def test_status_marks_gh_token_required_using_the_env_file_without_loading_old_secrets(self):
        self.env_file.write_bytes(b'\xef\xbb\xbfGH_REPO="sandbox/triage"\r\nTG_TRIAGE_MODE=live\r\n'
                                  b"GH_TOKEN=LEGACY_FAKE_TEST_ONLY\r\nTG_API_HASH=\xff\r\n")
        rc, out, err = self.invoke("secrets", "status", "--json")
        self.assertEqual((rc, err), (2, ""))
        gh = next(row for row in json.loads(out) if row["name"] == "gh-token")
        self.assertTrue(gh["required"])
        self.assertEqual(gh["state"], "missing")
        self.assertNotIn("LEGACY_FAKE_TEST_ONLY", out)
        rc, out, err = self.invoke("secrets", "status", "--json", env={"TG_TRIAGE_MODE": "dry-run"})
        self.assertEqual((rc, err), (0, ""))
        self.assertFalse(next(row for row in json.loads(out) if row["name"] == "gh-token")["required"])

    def test_status_reports_one_unreadable_state_when_directory_search_is_denied(self):
        native_stat = os.stat
        denied_path = os.path.join(self.directory, ".")

        def stat(path, *args, **kwargs):
            if os.fspath(path) == denied_path:
                raise PermissionError("PRIVATE_FAKE_DIRECTORY_ERROR")
            return native_stat(path, *args, **kwargs)

        nonroot = secrets.Platform(unix=False, euid=lambda: 1234)
        with patch.object(secrets, "PLATFORM", nonroot), patch.object(secrets.os, "stat", side_effect=stat), \
                patch.object(secrets.SecretsStore, "status") as read_status:
            label = "Không đọc được — chạy bằng sudo"
            self.assertEqual(self.invoke("secrets", "status"), (2, label + "\n", ""))
            rc, out, err = self.invoke("secrets", "status", "--json")
            self.assertEqual((rc, json.loads(out), err), (2, {"state": "unreadable", "status": label}, ""))
            read_status.assert_not_called()

    def test_status_does_not_guess_mode_when_the_env_file_is_private(self):
        native_open = Path.open

        def open_path(path, *args, **kwargs):
            if path == self.env_file:
                raise PermissionError("PRIVATE_FAKE_ENV_ERROR")
            return native_open(path, *args, **kwargs)

        with patch.object(Path, "open", open_path):
            rc, out, err = self.invoke("secrets", "status", "--json")
            self.assertEqual((rc, json.loads(out)["state"], err), (2, "unreadable", ""))
            rc, _, err = self.invoke("secrets", "status",
                                     env={"GH_REPO": "sandbox/triage", "TG_TRIAGE_MODE": "dry-run"})
            self.assertEqual((rc, err), (0, ""))

    def test_verify_uses_env_file_repo_with_per_variable_process_precedence(self):
        override = self.root / "stage.env"
        override.write_text("GH_REPO=staged/repo\nTG_TRIAGE_MODE=live\nGH_TOKEN='ignored\n",
                            encoding="utf-8")
        self.put("gh-token", FAKE_SECRET_TOKEN)
        for env, repo in (({}, "staged/repo"), ({"GH_REPO": "shell/repo"}, "shell/repo")):
            with self.subTest(env=env), patch.object(cli, "_github_verify", return_value="ok") as verify:
                self.assertEqual(self.invoke("secrets", "--env-file", str(override), "verify", "gh-token", env=env),
                                 (0, "gh-token: ok\n", ""))
            name, value, settings = verify.call_args.args
            self.assertEqual((name, value, settings.gh_repo, settings.mode),
                             ("gh-token", FAKE_SECRET_TOKEN, repo, "live"))

    def test_check_config_can_read_staged_env_without_using_legacy_keys(self):
        self.env_file.write_text("GH_REPO=staged/repo\nTG_TRIAGE_MODE=live\nGH_TOKEN='ignored\n",
                                 encoding="utf-8")
        self.put("gh-token", FAKE_SECRET_TOKEN)
        rc, out, err = self.invoke("check-config", "--env-file", str(self.env_file),
                                   env={"AGENTFLEET_API_KEY": "LEGACY_MODEL_FAKE_ONLY"})
        self.assertEqual(rc, 0, err)
        config = json.loads(out)
        self.assertEqual((config["mode"], config["gh_repo"]), ("live", "staged/repo"))
        self.assertIn("WARNING: AGENTFLEET_API_KEY:", err)
        self.assertNotIn("LEGACY_MODEL_FAKE_ONLY", out + err)

    def test_daemon_removes_shared_transport_ambient_override(self):
        from agentfleet_common.ai_transport import read_key

        self.configure(self.cloud_source())
        self.put("model-key.json", json.dumps({"scope": SCOPE, "key": FAKE_MODEL_KEY}))

        def provider(config, store):
            self.assertEqual(read_key(config.model.key_file, SCOPE, group_name="tglistener"), FAKE_MODEL_KEY)
            return Mock()

        with patch.object(cli, "_build_classifier", side_effect=provider) as build, \
                patch.object(cli, "Store", side_effect=lambda path: Store(":memory:")), \
                patch.object(cli, "Pipeline", return_value=Mock(assistant_runtime=None)), \
                patch.object(cli, "ProcessLock", return_value=contextlib.nullcontext()), \
                patch.object(cli, "validate_data_directories"), \
                patch("tg_triage.telegram_io.run", new_callable=AsyncMock):
            rc, _, err = self.invoke("run", env={"AGENTFLEET_KEY_SCOPE": SCOPE,
                                                 "AGENTFLEET_API_KEY": "MODEL_ENV_FAKE_ONLY"})
        self.assertEqual((rc, err), (0, ""))
        build.assert_called_once()

    def test_external_verify_is_opt_in_and_failure_does_not_install_candidate(self):
        self.put("gh-token", FAKE_SECRET_TOKEN)
        with patch.object(cli, "_github_verify", side_effect=OSError("private upstream")) as probe:
            self.assertEqual(self.invoke("secrets", "status")[0], 0)
            self.assertEqual(self.invoke("check-config")[0], 0)
            probe.assert_not_called()
            self.assertEqual(self.install("gh-token", NEXT, "--verify"),
                             (2, "", "ConfigError: gh-token: verify_failed\n"))
            self.assertEqual(self.store.read("gh-token"), FAKE_SECRET_TOKEN)
            self.assertFalse(self.audit.exists())
            self.assertEqual(self.invoke("secrets", "verify", "gh-token")[0], 2)
            self.assertEqual(probe.call_count, 2)

    def test_github_probe_never_mutates_issues_or_guesses_fine_grained_write(self):
        connections = []
        for metadata in (None, None, {"has_issues": True, "permissions": {"push": True}}):
            conn = MagicMock()
            response = conn.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.getheader.return_value = ""
            response.read.return_value = json.dumps(metadata).encode()
            connections.append(conn)
        self.put("gh-token", FAKE_SECRET_TOKEN)
        with patch.object(cli.http.client, "HTTPSConnection", side_effect=connections):
            rc, out, err = self.invoke("secrets", "verify", "gh-token", env={"GH_REPO": "sandbox/triage"})
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("Issues write unconfirmed", err)
        self.assertNotIn(FAKE_SECRET_TOKEN, err)
        self.assertEqual([wire.request.call_args.args for wire in connections],
                         [("GET", "/user"), ("GET", "/repos/sandbox/triage/issues?per_page=1"),
                          ("GET", "/repos/sandbox/triage")])

    def test_router_probe_uses_endpoint_scheme_host_port_and_path(self):
        for url, kind, host, port, path in (
                ("http://localhost:2828/nested/v1/", "HTTPConnection", "localhost", 2828,
                 "/nested/v1/chat/completions"),
                ("http://127.0.0.1/v1", "HTTPConnection", "127.0.0.1", 80, "/v1/chat/completions"),
                ("https://localhost/api", "HTTPSConnection", "localhost", 443, "/api/chat/completions")):
            conn = MagicMock()
            conn.getresponse.return_value.__enter__.return_value.status = 200
            settings = SimpleNamespace(assistant=SimpleNamespace(router_base_url=url, models=("fake-test-model",)))
            with self.subTest(url=url), patch.object(cli.http.client, "HTTPConnection") as http, \
                    patch.object(cli.http.client, "HTTPSConnection") as https:
                chosen, unused = (http, https) if kind == "HTTPConnection" else (https, http)
                chosen.return_value = conn
                cli._verify_value("router-key", FAKE_ROUTER_KEY, settings)
                chosen.assert_called_once_with(host, port, timeout=10)
                unused.assert_not_called()
                self.assertEqual(conn.request.call_args.args[:2], ("POST", path))


class RestartTests(SecretFixture):
    def test_ac4_unhealthy_restart_restores_previous_value_and_metadata_then_restarts_again(self):
        self.assertEqual(self.install("gh-token", FAKE_SECRET_TOKEN, "--expires", "2026-12-07")[0], 0)
        before = self.manifest()
        commands = []

        def runner(argv):
            commands.append(argv)
            n = len(commands)
            if argv[:2] == ["systemctl", "start"]:
                self.assertEqual(self.store.read("gh-token"), NEXT if n == 3 else FAKE_SECRET_TOKEN)
            return {1: unit(1), 4: unit(2), 5: unit(2), 6: "prefix ERROR " + NEXT,
                    7: unit(2), 10: unit(3), 11: unit(3), 12: "started clean"}.get(n, "")

        with patch.object(cli, "_health_command", side_effect=runner), patch.object(cli.time, "sleep") as wait:
            rc, out, err = self.install("gh-token", NEXT, "--restart")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("rollback", err)
        self.assertNotIn(NEXT, err)
        self.assertEqual(self.manifest(), before)
        self.assertEqual(self.store.read("gh-token"), FAKE_SECRET_TOKEN)
        self.assertEqual([argv for argv in commands if argv[1] in ("stop", "start")],
                         [["systemctl", "stop", "tg-triage"], ["systemctl", "start", "tg-triage"]] * 2)
        for journal, pid in ((commands[5], 2), (commands[11], 3)):
            self.assertEqual(journal[:4], ["journalctl", "-u", "tg-triage", "--since"])
            self.assertTrue(journal[4].endswith(" UTC"))
            self.assertEqual(journal[5], f"_PID={pid}")
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(self.audit_lines()[-1]["restart_result"], "unhealthy_rolled_back")

    def test_second_ctrl_c_reports_restored_key_and_audits_interrupted_rollback_restart(self):
        self.assertEqual(self.install("gh-token", FAKE_SECRET_TOKEN, "--expires", "2026-12-07")[0], 0)
        before = self.manifest()
        seen = []

        def restart(unit_name):
            seen.append((unit_name, self.store.read("gh-token")))
            raise KeyboardInterrupt("RAW_INTERRUPT_FAKE_ONLY")

        with patch.object(cli, "_restart_secret_unit", side_effect=restart):
            rc, out, err = self.install("gh-token", NEXT, "--restart")
        self.assertEqual((rc, out), (130, ""))
        self.assertEqual(seen, [("tg-triage", NEXT), ("tg-triage", FAKE_SECRET_TOKEN)])
        self.assertEqual(self.store.read("gh-token"), FAKE_SECRET_TOKEN)
        self.assertEqual(self.manifest(), before)
        self.assertIn("đã khôi phục key trước", err)
        self.assertIn("restart tg-triage bị gián đoạn", err)
        self.assertIn("systemctl status tg-triage", err)
        self.assertIn("systemctl start tg-triage", err)
        entry = self.audit_lines()[-1]
        self.assertEqual((entry["name"], entry["actor"], entry["restart_result"], entry["error_type"]),
                         ("gh-token", "owner-test", "unhealthy_rollback_interrupted", "KeyboardInterrupt"))
        self.assertNotIn("RAW_INTERRUPT_FAKE_ONLY", err + json.dumps(entry))

    def test_rollback_reports_specific_restart_outcome_before_audit_write_error(self):
        cases = (
            (None, "unhealthy_rollback_restart_failed", "đã khôi phục key, tg-triage chưa qua health check", 2),
            (27, "unhealthy_rolled_back", "đã rollback về key trước và restart tg-triage", 2),
            (KeyboardInterrupt(), "unhealthy_rollback_interrupted", "restart tg-triage bị gián đoạn", 130),
        )
        for result, audit_result, message, expected_rc in cases:
            self.put("gh-token", FAKE_SECRET_TOKEN)
            with self.subTest(audit_result=audit_result), \
                    patch.object(cli, "_restart_secret_unit", side_effect=[None, result]) as restart, \
                    patch.object(secrets.SecretsStore, "audit", side_effect=OSError("RAW_AUDIT_FAKE_ONLY")) as audit:
                rc, out, err = self.install("gh-token", NEXT, "--restart")
                self.assertEqual((rc, out), (expected_rc, ""))
                first, last = err.splitlines()
                self.assertIn(message, first)
                self.assertEqual(last, "ConfigError: secrets: local file operation failed")
                self.assertNotIn("RAW_AUDIT_FAKE_ONLY", err)
                self.assertEqual(self.store.read("gh-token"), FAKE_SECRET_TOKEN)
                self.assertEqual(restart.call_count, 2)
                audit.assert_called_once()
                self.assertEqual(audit.call_args.kwargs["restart_result"], audit_result)

    def test_config_validation_failure_reverts_and_no_systemd_command_runs(self):
        self.put("gh-token", FAKE_SECRET_TOKEN)
        (self.directory / "tg-api-id").unlink()
        with patch.object(cli, "_health_command", side_effect=AssertionError("systemd")) as health:
            rc, _, err = self.install("gh-token", NEXT, "--restart")
        self.assertEqual(rc, 2)
        self.assertIn("gh-token:", err)
        self.assertIn("ConfigError", err)
        self.assertNotIn("tg-api-id", err)
        self.assertEqual(self.store.read("gh-token"), FAKE_SECRET_TOKEN)
        self.assertFalse((self.directory / ".prev" / "gh-token").exists())
        self.assertNotIn("gh-token", self.manifest())
        self.assertEqual(self.audit_lines()[-1]["restart_result"], "validation_failed_rolled_back")
        self.assertEqual(self.audit_lines()[-1]["error_type"], "ConfigError")
        health.assert_not_called()

    def test_live_set_restart_uses_the_env_file_when_sudo_clears_process_variables(self):
        env_file = self.root / "rotation.env"
        env_file.write_text("GH_REPO=sandbox/triage\nTG_TRIAGE_MODE=live\nGH_TOKEN='IGNORED_FAKE\n",
                            encoding="utf-8")
        with patch.object(cli, "_checked_config", wraps=cli._checked_config) as check, \
                patch.object(cli, "_restart_secret_unit", return_value=27) as restart:
            rc, out, err = self.install("gh-token", NEXT, "--restart", "--env-file", str(env_file))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("PID 27", out)
        self.assertEqual(check.call_args.kwargs["environ"],
                         {"GH_REPO": "sandbox/triage", "TG_TRIAGE_MODE": "live"})
        restart.assert_called_once_with("tg-triage")
        self.assertEqual(self.store.read("gh-token"), NEXT)
        self.assertEqual(self.audit_lines()[-1]["restart_result"], "ok")

    def test_unexpected_validation_error_restores_and_audits_only_exception_type(self):
        self.put("gh-token", FAKE_SECRET_TOKEN)
        with patch.object(cli, "_checked_config", side_effect=RuntimeError("RAW_VALIDATION_FAKE_ONLY")), \
                patch.object(cli, "_restart_secret_unit") as restart:
            rc, out, err = self.install("gh-token", NEXT, "--restart")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("RuntimeError", err)
        self.assertIn("không restart", err)
        self.assertNotIn("RAW_VALIDATION_FAKE_ONLY", err + self.audit.read_text())
        self.assertEqual(self.store.read("gh-token"), FAKE_SECRET_TOKEN)
        self.assertEqual(self.audit_lines()[-1]["restart_result"], "validation_failed_rolled_back")
        self.assertEqual(self.audit_lines()[-1]["error_type"], "RuntimeError")
        restart.assert_not_called()

    def test_every_failed_restore_audits_and_reports_manual_action_without_restarting_again(self):
        native_audit = secrets.SecretsStore.audit
        for stage in ("validation", "health", "audit"):
            self.put("gh-token", FAKE_SECRET_TOKEN)

            def audit(store, fd, **kwargs):
                if stage == "audit" and kwargs["restart_result"] == "ok":
                    raise OSError("RAW_AUDIT_FAKE_ONLY")
                native_audit(store, fd, **kwargs)

            validate_error = RuntimeError("RAW_VALIDATION_FAKE_ONLY") if stage == "validation" else None
            with self.subTest(stage=stage), patch.object(cli, "_checked_config", side_effect=validate_error), \
                    patch.object(cli, "_restart_secret_unit", return_value=27 if stage == "audit" else None) as restart, \
                    patch.object(secrets.SecretsStore, "audit", autospec=True, side_effect=audit), \
                    patch.object(secrets.SecretsStore, "restore",
                                 side_effect=PermissionError("RAW_RESTORE_FAKE_ONLY")):
                rc, out, err = self.install("gh-token", NEXT, "--restart")
                self.assertEqual((rc, out), (2, ""))
                self.assertEqual(restart.call_count, 0 if stage == "validation" else 1)
                self.assertIn("gh-token:", err)
                self.assertIn("bản trước chưa được khôi phục", err)
                self.assertIn("cần xử lý thủ công", err)
                self.assertEqual(self.store.read("gh-token"), NEXT)
                entry = self.audit_lines()[-1]
                self.assertEqual((entry["name"], entry["actor"], entry["restart_result"], entry["error_type"]),
                                 ("gh-token", "owner-test", "rollback_failed", "PermissionError"))
                self.assertNotIn("_FAKE_ONLY", err + json.dumps(entry))

    def test_admin_password_restarts_only_admin(self):
        self.configure(self.source + "\n[admin]\n")
        run = Mock(side_effect=restart_outputs(15, 27))
        with patch.object(cli, "_health_command", run), patch.object(cli.time, "sleep"):
            rc, out, err = self.install("admin-password", "ADMIN_FAKE_TEST_RESTART", "--restart")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("PID 27", out)
        self.assertEqual(run.call_args_list[1].args[0], ["systemctl", "stop", "tg-triage-admin"])
        self.assertEqual(run.call_args_list[2].args[0], ["systemctl", "start", "tg-triage-admin"])

    def test_health_reads_after_graceful_stop_and_excludes_old_pid_journal(self):
        calls = []
        wait = Mock()

        def clock():
            self.assertEqual(calls[-1][0], ["systemctl", "stop", "tg-triage"])
            return datetime(2026, 11, 26, 12, tzinfo=timezone.utc)

        def run(argv, **kwargs):
            calls.append((argv, kwargs["timeout"]))
            step = len(calls)
            if argv[:2] == ["systemctl", "stop"] and kwargs["timeout"] < 150:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if step == 5:
                wait.assert_called_once_with(cli._HEALTH_WAIT)
            if argv[0] == "journalctl":
                return SimpleNamespace(stdout="healthy" if "_PID=21" in argv else "old shutdown ERROR ignored")
            return SimpleNamespace(stdout={1: unit(20), 4: unit(21, 3), 5: unit(21, 3)}.get(step, ""))

        with patch.object(cli.subprocess, "run", side_effect=run):
            self.assertEqual(cli._restart_secret_unit("tg-triage", wait=wait, now=clock), 21)
        self.assertGreaterEqual(calls[1][1], 180)
        self.assertEqual([call[0][1] for call in calls[:5]], ["show", "stop", "start", "show", "show"])
        self.assertEqual(calls[5][0], ["journalctl", "-u", "tg-triage", "--since",
                                       "2026-11-26 12:00:00.000000 UTC", "_PID=21", "--no-pager", "--output=cat"])

    def test_health_requires_stable_new_pid_no_restart_increase_and_clean_journal(self):
        for start, after, journal, expected in (
                (unit(2, 5), unit(2, 5), "", 2), (unit(2, 5), unit(2, 4), "", 2),
                (unit(1), unit(1), "", None), (unit(2), unit(0), "", None),
                (unit(2), unit(3), "", None), (unit(2, 5), unit(2, 6), "", None),
                (unit(2), unit(2, active="failed"), "", None),
                (unit(2), unit(2), " ERROR detail", None), (unit(2), unit(2), "Traceback detail", None),
                (unit(2), unit(2), "RulesHalt detail", None)):
            with self.subTest(start=start, after=after, journal=journal):
                runner = Mock(side_effect=[unit(1), "", "", start, after, journal])
                clock = lambda: datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
                self.assertEqual(cli._restart_secret_unit("tg-triage", run=runner, wait=Mock(), now=clock), expected)
        failure = subprocess.CalledProcessError(1, ["systemctl"], output="private fake")
        self.assertIsNone(cli._restart_secret_unit("tg-triage", run=Mock(side_effect=failure), wait=Mock()))
