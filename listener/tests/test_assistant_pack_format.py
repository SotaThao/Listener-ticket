"""Canonical pack format and builder tests; all writes stay in temp dirs."""
import hashlib
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_triage import decision_rules, rules_extensions
from tg_triage.assistant_pack import (
    AssistantPackError, PACK_FILE, PRINCIPLES_FILE, SOURCES, load, parse, render,
)

ROOT = Path(__file__).resolve().parents[2]


class AssistantPackFormatTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.rules = self.directory / "rules"
        self.rules.mkdir()
        self.principles = "# Principles\nKeep evidence explicit.\n"
        self.sources = [(name, f"# {name}\nRule {i}.\n") for i, name in enumerate(SOURCES)]
        self.text = render(self.principles, self.sources)
        (self.directory / PRINCIPLES_FILE).write_bytes(self.principles.encode("utf-8"))
        (self.directory / PACK_FILE).write_bytes(self.text.encode("utf-8"))
        for name, body in self.sources:
            (self.rules / name).write_bytes(body.encode("utf-8"))

    def test_render_is_deterministic_and_fingerprint_uses_zeroed_header(self):
        again = render(self.principles, self.sources)
        self.assertEqual(self.text, again)
        self.assertEqual(parse(self.text).fingerprint, parse(again).fingerprint)
        fingerprint = parse(self.text).fingerprint
        zeroed = self.text.replace(f"pack_fingerprint: {fingerprint}",
                                   "pack_fingerprint: " + "0" * 64, 1)
        self.assertEqual(fingerprint, hashlib.sha256(zeroed.encode("utf-8")).hexdigest())
        self.assertNotIn("\r", self.text)
        self.assertTrue(self.text.endswith("\n"))

    def test_bom_crlf_and_cr_inputs_normalize_identically(self):
        for ending in ("\r\n", "\r"):
            with self.subTest(ending=repr(ending)):
                principles = "\ufeff" + self.principles.replace("\n", ending)
                sources = [(name, "\ufeff" + body.replace("\n", ending))
                           for name, body in self.sources]
                self.assertEqual(self.text, render(principles, sources))

    def test_one_byte_change_in_each_input_changes_fingerprint(self):
        fingerprint = parse(self.text).fingerprint
        self.assertNotEqual(fingerprint, parse(render(self.principles + "x", self.sources)).fingerprint)
        for i, (name, body) in enumerate(self.sources):
            with self.subTest(source=name):
                changed = self.sources.copy()
                changed[i] = (name, body + "x")
                self.assertNotEqual(fingerprint, parse(render(self.principles, changed)).fingerprint)

    def test_parse_round_trip_and_source_hashes(self):
        pack = parse(self.text)
        self.assertEqual(pack.text, self.text)
        self.assertEqual(tuple(pack.sources), (PRINCIPLES_FILE, *SOURCES))
        for name, body in [(PRINCIPLES_FILE, self.principles), *self.sources]:
            self.assertEqual(pack.sources[name], hashlib.sha256(body.encode("utf-8")).hexdigest())
        with self.assertRaises(TypeError):
            pack.sources[PRINCIPLES_FILE] = "changed"

    def test_empty_and_unterminated_source_text_round_trips(self):
        for body in ("", "no trailing newline", "\n\n", "Tiếng Việt\n"):
            with self.subTest(body=body):
                text = render(body, [(name, body) for name in SOURCES])
                self.assertEqual(parse(text).text, text)

    def test_parse_rejects_edited_body_with_stale_fingerprint(self):
        with self.assertRaises(AssistantPackError):
            parse(self.text.replace("Keep evidence explicit.", "Keep evidence implicit."))
        # Even a matching edited source hash cannot bypass the pack fingerprint.
        old = hashlib.sha256(self.principles.encode()).hexdigest()
        new_body = self.principles + "x"
        changed = self.text.replace(self.principles, new_body).replace(
            old, hashlib.sha256(new_body.encode()).hexdigest())
        with self.assertRaisesRegex(AssistantPackError, "pack fingerprint mismatch"):
            parse(changed)

    def test_parse_rejects_malformed_headers(self):
        for text in ("", self.text.replace("pack v1", "pack v2", 1),
                     self.text.replace("pack_fingerprint:", "fingerprint:", 1),
                     self.text.replace("sha256", "sha512", 1),
                     self.text.replace("-->\n", "-- >\n", 1),
                     self.text + "extra\n"):
            with self.subTest(header=text[:80]), self.assertRaises(AssistantPackError):
                parse(text)

    def test_parse_rejects_missing_extra_duplicate_or_unordered_sources(self):
        lines = self.text.splitlines(keepends=True)
        variants = []
        variants.append("".join(lines[:3] + lines[4:]))
        variants.append("".join(lines[:3] + ["source extra.md sha256 " + "0" * 64 + "\n"] + lines[3:]))
        variants.append("".join(lines[:3] + [lines[3]] + lines[3:]))
        swapped = lines.copy()
        swapped[3], swapped[4] = swapped[4], swapped[3]
        variants.append("".join(swapped))
        variants.append(self.text.replace(f"<!-- source: {SOURCES[0]} -->",
                                          f"<!-- source: {SOURCES[1]} -->", 1))
        for text in variants:
            with self.subTest(text=text[:350]), self.assertRaises(AssistantPackError):
                parse(text)

    def test_render_rejects_wrong_source_names_order_and_reserved_markers(self):
        for sources in (self.sources[:-1], self.sources + [("extra.md", "")],
                        list(reversed(self.sources)), self.sources + [self.sources[0]]):
            with self.subTest(sources=sources), self.assertRaises(AssistantPackError):
                render(self.principles, sources)
        for marker in ("<!-- source: fake.md -->", "<!-- end-source: fake.md -->"):
            with self.subTest(marker=marker), self.assertRaises(AssistantPackError):
                render(marker, self.sources)

    def test_load_matches_and_normalizes_source_files(self):
        self.assertEqual(load(self.directory, self.rules), parse(self.text))
        for name, body in [(PRINCIPLES_FILE, self.principles), *self.sources]:
            path = (self.directory if name == PRINCIPLES_FILE else self.rules) / name
            path.write_bytes(("\ufeff" + body.replace("\n", "\r\n")).encode("utf-8"))
        self.assertEqual(load(self.directory, self.rules).text, self.text)

    def test_load_rejects_missing_pack_principles_or_rule(self):
        for path in (self.directory / PACK_FILE, self.directory / PRINCIPLES_FILE,
                     self.rules / SOURCES[0]):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(AssistantPackError, "cannot read UTF-8 file"):
                    load(self.directory, self.rules)
                path.write_bytes(original)

    def test_load_rejects_rule_and_principles_drift(self):
        for name in (PRINCIPLES_FILE, *SOURCES):
            with self.subTest(name=name):
                path = (self.directory if name == PRINCIPLES_FILE else self.rules) / name
                original = path.read_bytes()
                path.write_bytes(original + b"x")
                with self.assertRaisesRegex(AssistantPackError, "source drift"):
                    load(self.directory, self.rules)
                path.write_bytes(original)

    def test_load_rejects_unreadable_or_non_utf8_pack(self):
        pack_path = self.directory / PACK_FILE
        pack_path.write_bytes(b"\xff\xfe")
        with self.assertRaises(AssistantPackError):
            load(self.directory, self.rules)
        # Permission bits are not portable across Windows/Linux or root users.
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(AssistantPackError, "denied"):
                load(self.directory, self.rules)

    def test_sources_equal_shipped_and_actually_loaded_rule_files(self):
        project = tomllib.loads((ROOT / "listener/pyproject.toml").read_text(encoding="utf-8"))
        shipped = project["tool"]["setuptools"]["data-files"]["share/tg-triage/rules"]
        self.assertEqual(SOURCES, tuple(Path(name).name for name in shipped))
        # Observe real file reads, so the pack list cannot diverge from the loader.
        with patch.object(rules_extensions.ExtensionRules, '_table',
                          wraps=rules_extensions.ExtensionRules._table) as read:
            rules_extensions.ExtensionRules(ROOT / 'listener/rules')
        extension_files = tuple(call.args[0].name for call in read.call_args_list)
        self.assertEqual(extension_files, tuple(f"{name}.md" for name in rules_extensions.ExtensionRules.FILES))
        self.assertEqual(SOURCES, tuple(f"{name}.md" for name in decision_rules.FILES) + extension_files)

    def test_pack_bom_crlf_and_cr_parse_and_load_with_same_fingerprint(self):
        expected = parse(self.text)
        for ending in ("\n", "\r\n", "\r"):
            for bom in ("", "\ufeff"):
                with self.subTest(ending=repr(ending), bom=bool(bom)):
                    changed = bom + self.text.replace("\n", ending)
                    (self.directory / PACK_FILE).write_bytes(changed.encode('utf-8'))
                    self.assertEqual(parse(changed), expected)
                    self.assertEqual(load(self.directory, self.rules), expected)
                    edited = changed.replace('Keep evidence explicit.', 'Keep evidence implicit.')
                    (self.directory / PACK_FILE).write_bytes(edited.encode('utf-8'))
                    with self.assertRaises(AssistantPackError):
                        load(self.directory, self.rules)

    def test_builder_subprocess_build_check_and_drift(self):
        out = self.directory / "generated" / PACK_FILE
        command = [sys.executable, str(ROOT / "scripts/build-assistant-pack.py"),
                   "--rules-dir", str(self.rules), "--principles",
                   str(self.directory / PRINCIPLES_FILE), "--out", str(out)]
        def run(*args):
            return subprocess.run([*command, *args], capture_output=True, text=True,
                                  encoding="utf-8", cwd=self.directory)
        built = run()
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual(out.read_bytes(), self.text.encode("utf-8"))
        checked = run("--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        for ending in ("\r\n", "\r"):
            for bom in ("", "\ufeff"):
                with self.subTest(ending=repr(ending), bom=bool(bom)):
                    on_disk = (bom + self.text.replace("\n", ending)).encode('utf-8')
                    out.write_bytes(on_disk)
                    checked = run("--check")
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                    self.assertEqual(out.read_bytes(), on_disk)
        out.write_bytes(self.text.encode('utf-8'))
        (self.rules / SOURCES[0]).write_bytes(b"changed rule\n")
        stale = run("--check")
        self.assertEqual(stale.returncode, 1)
        self.assertIn("differs from rebuild", stale.stderr)
        self.assertEqual(out.read_bytes(), self.text.encode("utf-8"))
        out.unlink()
        missing = run("--check")
        self.assertEqual(missing.returncode, 1)
        self.assertTrue(missing.stderr)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
