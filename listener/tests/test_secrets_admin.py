"""The authenticated Admin status route reads the manifest and stat only."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tg_triage import admin
from tests.helpers import FAKE_ROUTER_KEY, FAKE_SECRET_TOKEN
from tests.test_secrets_store import SecretFixture


class AdminSecretStatusTests(SecretFixture):
    def handler(self, logged_in, session_path=""):
        handler = object.__new__(admin._Handler)
        handler.path = "/api/secrets"
        handler.headers = {"Cookie": "la_sid=fake"}
        handler.server = SimpleNamespace(
            sessions=SimpleNamespace(valid=lambda sid: logged_in and sid == "fake"),
            secret_store=self.store, required_secrets={"gh-token", "tg-api-id", "tg-api-hash"},
            telegram_session_path=session_path, clock=lambda: datetime(2026, 11, 26, 12).timestamp(),
        )
        handler._json = Mock()
        return handler

    def test_ac6_requires_auth_and_has_no_post_write_route(self):
        handler = self.handler(False)
        with patch.object(self.store, "status", side_effect=AssertionError("unauthorized read")):
            handler.do_GET()
            handler._json.assert_called_once_with(401, {"error": "unauthorized"})
        handler = self.handler(True)
        handler.do_POST()
        handler._json.assert_called_once_with(404, {"error": "not_found"})

    def test_ac1b_and_ac6_expiry_and_session_status_never_open_secret_files(self):
        self.assertEqual(self.install("gh-token", FAKE_SECRET_TOKEN, "--expires", "2026-12-03")[0], 0)
        self.assertEqual(self.install("router-key", FAKE_ROUTER_KEY, "--expires", "2026-11-25")[0], 0)
        session = self.root / "gateway.session"
        session.write_text("SESSION_FAKE_NOT_READ", encoding="utf-8")
        manifest = self.directory / "manifest.json"
        metadata = self.manifest()
        metadata["gh-token"].update({"value": "MANIFEST_FAKE_VALUE", "hash": "MANIFEST_FAKE_HASH"})
        manifest.write_text(json.dumps(metadata), encoding="utf-8")
        handler = self.handler(True, str(session))
        original_open = Path.open
        opened = []

        def open_manifest(path, *args, **kwargs):
            self.assertEqual(path, manifest)
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", new=open_manifest):
            handler.do_GET()
        self.assertEqual(opened, [manifest])
        status, payload = handler._json.call_args.args
        self.assertEqual(status, 200)
        rows = {row["name"]: row for row in payload["secrets"]}
        self.assertEqual(rows["gh-token"]["expiry_state"], "expiring")
        self.assertEqual(rows["router-key"]["expiry_state"], "expired")
        self.assertEqual(rows["gh-token"]["command"], "sudo tg-triage secrets set gh-token --restart")
        self.assertEqual(payload["telegram_session"]["state"], "ok")
        self.assertEqual(payload["telegram_session"]["command"], "sudo -u tglistener tg-triage login")
        body = json.dumps(payload)
        for value in (FAKE_SECRET_TOKEN, FAKE_ROUTER_KEY, "MANIFEST_FAKE_VALUE", "MANIFEST_FAKE_HASH",
                      "SESSION_FAKE_NOT_READ", '"length"', '"source"'):
            self.assertNotIn(value, body)
