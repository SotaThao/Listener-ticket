"""Offline cut-over and secret storage acceptance tests."""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tg_triage import admin, secrets_store as secrets
from tg_triage.__main__ import main
from tg_triage.config import ConfigError, _validate_admin_secret_stat, load_config
from tests.helpers import (EXAMPLE, FAKE_HASH, FAKE_MODEL_KEY, FAKE_READ_TOKEN, FAKE_ROUTER_KEY,
                           FAKE_SECRET_TOKEN, FAKE_TG_SECRETS, put_test_secret, write_test_config)


SCOPE = "deepseek|https://api.deepseek.com/"
NEXT = "GH_ROTATED_FAKE_FOR_TEST_ONLY"


class SecretFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "listener.toml"
        self.directory = self.root / "secrets"
        self.audit = self.root / "logs" / "secrets-audit.log"
        self.env_file = self.root / "listener.env"
        self.source = EXAMPLE.read_text(encoding="utf-8")
        self.enterContext(patch.object(secrets, "PLATFORM", secrets.Platform(unix=False, euid=lambda: 0)))
        self.enterContext(patch.object(secrets, "DEFAULT_ENV_FILE", self.env_file))
        if os.name == "posix":
            # Platform(unix=False) leaves test files in the runner's own group, while the real
            # readers (agentfleet_common read_key, config._assistant_token) still resolve
            # "tglistener" on POSIX. Map that name to the runner's group so the suite behaves
            # the same on a host that has a real tglistener group (gw-01) and one that has none.
            import grp

            real_getgrnam = grp.getgrnam
            self.enterContext(patch("grp.getgrnam", lambda name: SimpleNamespace(gr_gid=os.getegid())
                                    if name == "tglistener" else real_getgrnam(name)))
        self.store = secrets.SecretsStore(secrets.SecretPaths(self.directory, self.audit))
        self.configure()

    def configure(self, source=None, *, credentials=None):
        write_test_config(self.config, self.source if source is None else source, credentials=credentials)

    def put(self, name, value):
        return put_test_secret(self.directory, name, value)

    def invoke(self, *args, value="", env=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"SUDO_USER": "owner-test", **(env or {})}, clear=True), \
                patch("sys.stdin", io.StringIO(value)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main([*args, "--config", str(self.config)])
        return rc, out.getvalue(), err.getvalue()

    def install(self, name, value, *args):
        return self.invoke("secrets", "set", name, "--stdin", *args, value=value + "\n")

    def manifest(self):
        return json.loads((self.directory / "manifest.json").read_bytes())

    def audit_lines(self):
        return [json.loads(line) for line in self.audit.read_bytes().splitlines()]

    def cloud_source(self):
        return (self.source.replace('provider = "ollama"', 'provider = "deepseek"')
                .replace("think = true", "think = false")
                .replace("cloud_consent = false",
                         'cloud_consent = true\nbase_url = "https://api.deepseek.com/"'))


class CutoverTests(SecretFixture):
    def test_ac1_new_files_only_and_every_old_declaration_warns(self):
        old_path = self.root / "must-not-read"
        source = (self.cloud_source().replace("[model]\n", f"[model]\nkey_file = {json.dumps(old_path.as_posix())}\n")
                  .replace("[assistant]\n", "[assistant]\nenabled = true\nmode = \"live\"\n"
                           "router_key_file = false\ngithub_token_file = 12\n")
                  .replace('host = ""', 'host = "proxy.invalid"') + "\n[admin]\npassword_hash_file = false\n")
        self.configure(source, credentials={
            **FAKE_TG_SECRETS, "gh-token": FAKE_SECRET_TOKEN, "tg-proxy-secret": "PROXY_FAKE_TEST_ONLY",
            "router-key": FAKE_ROUTER_KEY, "assistant-github-token": FAKE_READ_TOKEN,
            "model-key.json": json.dumps({"scope": SCOPE, "key": FAKE_MODEL_KEY}),
            "admin-password": admin._encode_password_hash("ADMIN_FAKE_TEST_ONLY"),
        })
        env = {"GH_REPO": "sandbox/triage", "TG_TRIAGE_MODE": "live", "GH_TOKEN": "OLD_TOKEN_FAKE_ONLY",
               "TG_API_ID": "678901", "TG_API_HASH": "f" * 32, "TG_PROXY_SECRET": "OLD_PROXY_FAKE_ONLY",
               "AGENTFLEET_KEY_SCOPE": "LEGACY_SCOPE_FAKE_ONLY", "AGENTFLEET_API_KEY": "LEGACY_MODEL_FAKE_ONLY"}
        settings = load_config(self.config, env, include_admin=True)
        self.assertEqual((settings.api_id, settings.api_hash, settings.telegram.proxy.secret),
                         (123, FAKE_HASH, "PROXY_FAKE_TEST_ONLY"))
        self.assertEqual((settings.mode, settings.gh_repo, settings.gh_token),
                         ("live", "sandbox/triage", FAKE_SECRET_TOKEN))
        self.assertEqual((settings.assistant.router_key, settings.assistant.github_token),
                         (FAKE_ROUTER_KEY, FAKE_READ_TOKEN))
        self.assertEqual(Path(settings.model.key_file), self.directory / "model-key.json")
        self.assertEqual(settings.admin.password_hash_file, self.directory / "admin-password")
        rc, out, err = self.invoke("check-config", env=env)
        self.assertEqual(rc, 0, err)
        warnings = {line.split(": ")[1] for line in err.splitlines() if line.startswith("WARNING: ")}
        self.assertEqual(warnings, {*secrets.LEGACY_ENV, *secrets.MODEL_ENV_OVERRIDES,
                                   *(f"{table}.{key}" for table, key in secrets.LEGACY_TOML)})
        self.assertEqual(json.loads(out)["api_id"], {"length": 3})
        self.assertNotIn("key_file", json.loads(out)["model"])
        for value in (FAKE_HASH, FAKE_SECRET_TOKEN, FAKE_READ_TOKEN, FAKE_ROUTER_KEY, FAKE_MODEL_KEY,
                      env["GH_TOKEN"], env["TG_PROXY_SECRET"], env["AGENTFLEET_KEY_SCOPE"],
                      env["AGENTFLEET_API_KEY"], old_path.as_posix()):
            self.assertNotIn(value, out + err)

    def test_ac2_missing_and_bad_format_return_safe_exit_2(self):
        (self.directory / "tg-api-id").unlink()
        rc, out, err = self.invoke("check-config", env={"TG_API_ID": "123"})
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("tg-api-id: missing", err)
        self.assertIn("secrets migrate", err)
        self.put("tg-api-id", "123")
        self.put("tg-api-hash", "HASH_FAKE_BUT_MALFORMED")
        self.assertEqual(self.invoke("check-config"), (2, "", "ConfigError: tg-api-hash: bad_format\n"))

    def test_validate_only_checks_schema_without_touching_keys(self):
        self.configure(self.cloud_source() + "\n[admin]\n", credentials={})
        with patch.object(secrets.SecretsStore, "read", side_effect=AssertionError("credential read")):
            settings = load_config(self.config, {"TG_TRIAGE_MODE": "bad"}, validate_only=True, include_admin=True)
        self.assertEqual(settings.mode, "dry-run")
        self.assertTrue({"model-key.json", "admin-password"} <= settings.required_secrets)

    def test_model_scope_is_exact_and_an_env_key_cannot_rescue_a_mismatch(self):
        self.configure(self.cloud_source())
        self.put("model-key.json", json.dumps({"scope": SCOPE.rstrip("/"), "key": FAKE_MODEL_KEY}))
        with patch.dict(os.environ, {"AGENTFLEET_KEY_SCOPE": SCOPE, "AGENTFLEET_API_KEY": "MODEL_ENV_FAKE_ONLY"}):
            with self.assertRaisesRegex(ConfigError, "^model-key.json: bad_format$"):
                load_config(self.config)
        self.assertEqual(self.install("model-key.json", FAKE_MODEL_KEY)[0], 0)
        self.assertEqual(json.loads((self.directory / "model-key.json").read_bytes()),
                         {"scope": SCOPE, "key": FAKE_MODEL_KEY})

    def test_ac5_plan_read_only_apply_copies_eight_without_deleting_legacy(self):
        env_path = self.root / "legacy.env"
        env_path.write_text(f'GH_TOKEN="{FAKE_SECRET_TOKEN}"\nTG_API_ID=123\nTG_API_HASH={FAKE_HASH}\n'
                            "TG_PROXY_SECRET=PROXY_FAKE_TEST_ONLY\nGH_REPO=sandbox/triage\n", encoding="utf-8")
        old_values = {
            "model-key.json": json.dumps({"scope": SCOPE, "key": FAKE_MODEL_KEY}),
            "router-key": FAKE_ROUTER_KEY, "assistant-github-token": FAKE_READ_TOKEN,
            "admin-password": admin._encode_password_hash("ADMIN_FAKE_TEST_ONLY"),
        }
        old = {name: put_test_secret(self.root / "legacy", name, value) for name, value in old_values.items()}
        source = self.cloud_source() + "\n[admin]\n"
        for (table, key), name in secrets.LEGACY_TOML.items():
            source = source.replace(f"[{table}]\n", f"[{table}]\n{key} = {json.dumps(old[name].as_posix())}\n")
        for file in self.directory.iterdir():
            file.unlink()
        self.directory.rmdir()
        self.configure(source, credentials={})
        before = {file: file.read_bytes() for file in (env_path, *old.values())}
        args = ("secrets", "migrate", "--env-file", str(env_path))
        rc, plan, err = self.invoke(*args)
        self.assertEqual((rc, err), (0, ""))
        self.assertFalse(self.directory.exists())
        self.assertFalse(self.audit.exists())
        for name in secrets.CATALOG:
            self.assertRegex(plan, rf"(?m)^  {name} [0-9]+$")
        for file in old.values():
            self.assertIn(str(file), plan)
        self.assertIn("GH_TOKEN", plan)
        self.assertNotIn("GH_REPO", plan)
        for value in (FAKE_HASH, FAKE_SECRET_TOKEN, FAKE_READ_TOKEN, FAKE_MODEL_KEY, FAKE_ROUTER_KEY,
                      "PROXY_FAKE_TEST_ONLY", "scrypt$"):
            self.assertNotIn(value, plan + err)
        self.assertEqual(self.invoke(*args, "--apply")[0], 0)
        self.assertEqual(set(self.manifest()), set(secrets.CATALOG))
        self.assertEqual({row["name"] for row in self.audit_lines()}, set(secrets.CATALOG))
        self.assertEqual({row["source"] for row in self.audit_lines()}, {"migrate"})
        self.assertEqual((self.directory / "admin-password").read_bytes(), before[old["admin-password"]])
        self.assertEqual({file: file.read_bytes() for file in before}, before)
        self.put("gh-token", NEXT)
        self.assertEqual(self.invoke(*args, "--apply")[0], 0)
        self.assertEqual(self.store.read("gh-token"), NEXT)

    def test_migrate_skips_mismatched_model_scope_only_when_optional(self):
        old_model = put_test_secret(self.root / "legacy", "model-key.json",
                                    json.dumps({"scope": "openai|https://api.openai.com/v1",
                                                "key": FAKE_MODEL_KEY}))
        before = old_model.read_bytes()
        self.env_file.write_text(f"TG_API_ID=321\nTG_API_HASH={FAKE_HASH}\n"
                                 "TG_PROXY_SECRET='UNTERMINATED_PROXY_FAKE_ONLY\n", encoding="utf-8")
        for name in FAKE_TG_SECRETS:
            (self.directory / name).unlink()
        legacy = f"[model]\nkey_file = {json.dumps(old_model.as_posix())}\n"
        self.configure(self.source.replace("[model]\n", legacy), credentials={})
        rc, plan, err = self.invoke("secrets", "migrate")
        self.assertEqual((rc, err), (0, ""))
        for name in ("model-key.json", "tg-proxy-secret"):
            self.assertIn(f"WARNING: {name}: bad_format", plan)
        self.assertIn("tg-api-id 3", plan)
        self.assertNotIn(FAKE_MODEL_KEY, plan)
        self.assertNotIn("UNTERMINATED_PROXY_FAKE_ONLY", plan)
        self.assertFalse((self.directory / "tg-api-id").exists())
        self.assertEqual(self.invoke("secrets", "migrate", "--apply")[0], 0)
        self.assertEqual(self.store.read("tg-api-id"), "321")
        self.assertFalse((self.directory / "model-key.json").exists())
        self.assertEqual(old_model.read_bytes(), before)

        self.configure(self.cloud_source().replace("[model]\n", legacy), credentials={})
        audit_before = self.audit.read_bytes()
        self.assertEqual(self.invoke("secrets", "migrate", "--apply"),
                         (2, "", "ConfigError: model-key.json: bad_format\n"))
        self.assertEqual(self.audit.read_bytes(), audit_before)

    def test_migrate_uses_env_file_mode_to_classify_an_unusable_gh_source(self):
        self.env_file.write_text("GH_REPO=sandbox/triage\nTG_TRIAGE_MODE=live\nGH_TOKEN=FAKE\n",
                                 encoding="utf-8")
        self.assertEqual(self.invoke("secrets", "migrate", "--apply"),
                         (2, "", "ConfigError: gh-token: bad_format\n"))
        self.assertFalse(self.audit.exists())
        rc, plan, err = self.invoke("secrets", "migrate", env={"TG_TRIAGE_MODE": "dry-run"})
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("WARNING: gh-token: bad_format", plan)


class StorageTests(SecretFixture):
    def test_ac3_set_audits_only_metadata_and_rollback_swaps_one_previous_copy(self):
        self.assertEqual(self.install("gh-token", FAKE_SECRET_TOKEN, "--expires", "2026-12-03")[0], 0)
        self.assertEqual(self.install("gh-token", NEXT + "\nline ignored")[0], 0)
        previous = self.directory / ".prev" / "gh-token"
        self.assertEqual(self.store.read("gh-token"), NEXT)
        self.assertEqual(previous.read_text().strip(), FAKE_SECRET_TOKEN)
        fields = self.manifest()["gh-token"]
        self.assertEqual(set(fields), {"updated_at", "updated_by", "length", "source", "expires_at"})
        self.assertEqual((fields["updated_by"], fields["length"], fields["source"]), ("owner-test", len(NEXT), "cli"))
        self.assertEqual(self.invoke("secrets", "rollback", "gh-token")[0], 0)
        self.assertEqual(self.store.read("gh-token"), FAKE_SECRET_TOKEN)
        self.assertEqual(previous.read_text().strip(), NEXT)
        self.assertEqual(self.manifest()["gh-token"]["expires_at"], "2026-12-03")
        rows = self.audit_lines()
        self.assertEqual([row["action"] for row in rows], ["set", "set", "rollback"])
        self.assertEqual(set(rows[0]), {"ts", "action", "name", "actor", "source", "length", "restart_result"})
        self.assertEqual({row["actor"] for row in rows}, {"owner-test"})
        combined = (self.directory / "manifest.json").read_bytes() + self.audit.read_bytes()
        self.assertNotIn(NEXT.encode(), combined)
        self.assertNotIn(FAKE_SECRET_TOKEN.encode(), combined)
        self.assertFalse(list(self.directory.rglob(".tmp-*")))

    def test_ac3_fsync_chown_chmod_same_directory_before_replace(self):
        events = []
        target = self.put("gh-token", FAKE_SECRET_TOKEN)
        old = target.read_bytes()
        real_replace = os.replace

        def chown(path, uid, gid):
            self.assertEqual((path.parent, uid, gid), (target.parent, 0, 314))
            self.assertNotEqual(path, target)
            self.assertEqual(target.read_bytes(), old)
            events.append("chown")

        def chmod(fd, mode):
            self.assertEqual(mode, 0o640)
            events.append("chmod")

        def rename(path, destination):
            self.assertEqual((path.parent, destination), (target.parent, target))
            events.append("replace")
            real_replace(path, destination)

        platform = secrets.Platform(unix=True, group_id=lambda: 314, chown=chown, fchmod=chmod)
        with patch.object(secrets.os, "fsync", side_effect=lambda fd: events.append("fsync")), \
                patch.object(secrets.os, "replace", side_effect=rename):
            secrets.SecretsStore(self.store.paths, platform=platform)._atomic(target, NEXT.encode(), private=False)
        self.assertEqual(events[:5], ["fsync", "chown", "chmod", "fsync", "replace"])
        self.assertEqual(target.read_bytes(), NEXT.encode())

    def test_permission_policy_and_type_errors_keep_safe_kinds(self):
        platform = secrets.Platform(unix=True, group_id=lambda: 42)
        for mode, gid, error in ((stat.S_IFREG | 0o600, 9, None), (stat.S_IFREG | 0o640, 42, None),
                                 (stat.S_IFREG | 0o640, 9, "bad_owner"), (stat.S_IFREG | 0o644, 42, "bad_mode"),
                                 (stat.S_IFREG | 0o660, 42, "bad_mode"), (stat.S_IFDIR | 0o640, 42, "bad_mode")):
            with self.subTest(mode=mode, gid=gid):
                info = SimpleNamespace(st_mode=mode, st_gid=gid)
                if error:
                    with self.assertRaisesRegex(secrets.SecretError, f"^gh-token: {error}$"):
                        _validate_admin_secret_stat(info, name="gh-token", platform=platform)
                else:
                    _validate_admin_secret_stat(info, name="gh-token", platform=platform)

    def test_admin_password_overwrite_reuses_scrypt(self):
        name = "admin-password"
        for password in ("ADMIN_OLD_FAKE_TEST_ONLY", "ADMIN_NEW_FAKE_TEST_ONLY"):
            self.assertEqual(self.install(name, password)[0], 0)
        for path, password in ((self.directory / name, "ADMIN_NEW_FAKE_TEST_ONLY"),
                               (self.directory / ".prev" / name, "ADMIN_OLD_FAKE_TEST_ONLY")):
            salt, n, r, p, digest = admin._read_password_hash(path)
            self.assertEqual(admin._scrypt(password.encode(), salt, n, r, p), digest)
            self.assertNotIn(password.encode(), path.read_bytes() + self.audit.read_bytes())

    def test_rollback_cannot_reuse_the_other_github_token(self):
        self.assertEqual(self.install("gh-token", FAKE_SECRET_TOKEN)[0], 0)
        self.assertEqual(self.install("gh-token", NEXT)[0], 0)
        self.assertEqual(self.install("assistant-github-token", FAKE_SECRET_TOKEN)[0], 0)
        self.assertEqual(self.invoke("secrets", "rollback", "gh-token"),
                         (2, "", "ConfigError: gh-token: bad_format\n"))
        self.assertEqual(self.store.read("gh-token"), NEXT)

    def test_github_tokens_must_be_distinct_and_invalid_shapes_are_rejected(self):
        self.put("gh-token", FAKE_SECRET_TOKEN)
        self.assertEqual(self.install("assistant-github-token", FAKE_SECRET_TOKEN),
                         (2, "", "ConfigError: assistant-github-token: bad_format\n"))
        self.put("assistant-github-token", FAKE_SECRET_TOKEN)
        with self.assertRaisesRegex(ConfigError, "^assistant-github-token: bad_format$"):
            load_config(self.config, {})
        for name, bad in (("gh-token", "NO"), ("assistant-github-token", "FAKE TOKEN WITH SPACES HERE"),
                          ("tg-api-id", "-1"), ("tg-api-id", "0"), ("tg-api-hash", "g" * 32),
                          ("tg-proxy-secret", "FAKE\tPROXY"), ("router-key", "\x00FAKE"),
                          ("model-key.json", ""), ("admin-password", "")):
            with self.subTest(name=name), self.assertRaisesRegex(secrets.SecretError, f"^{name}: bad_format$"):
                secrets.format_value(name, bad, SCOPE)
