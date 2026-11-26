import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "tg_triage" / "admin_static"


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.text = []
        self.inline_content = []
        self.raw_element = None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag in {"script", "style"}:
            self.raw_element = tag

    def handle_endtag(self, tag):
        if tag == self.raw_element:
            self.raw_element = None

    def handle_data(self, data):
        if self.raw_element and data.strip():
            self.inline_content.append(self.raw_element)
        elif data.strip():
            self.text.append(data.strip())


class AdminStaticTests(unittest.TestCase):
    def read(self, name):
        return (STATIC_DIR / name).read_text(encoding="utf-8")

    def test_files_exist_in_listener_package(self):
        for name in ("index.html", "app.css", "app.js"):
            with self.subTest(name=name):
                self.assertTrue((STATIC_DIR / name).is_file())
                self.assertTrue(self.read(name).strip())

    def test_html_uses_only_external_same_origin_script_and_style(self):
        markup = Markup(self.read("index.html"))
        self.assertEqual(markup.inline_content, [])
        self.assertEqual(
            [attrs.get("src") for tag, attrs in markup.elements if tag == "script"],
            ["/static/app.js"],
        )
        self.assertEqual(
            [attrs.get("href") for tag, attrs in markup.elements
             if tag == "link" and attrs.get("rel") == "stylesheet"],
            ["/static/app.css"],
        )
        for tag, attrs in markup.elements:
            with self.subTest(tag=tag, attrs=attrs):
                self.assertNotIn("style", attrs)
                self.assertNotIn(tag, {"style"})
                self.assertFalse(any(attr.startswith("on") for attr in attrs))

    def test_javascript_avoids_parser_and_execution_sinks(self):
        script = self.read("app.js")
        self.assertNotRegex(
            script,
            r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|eval)\b"
            r"|\bdocument\s*\.\s*write(?:ln)?\b|\bnew\s+Function\b",
        )

    def test_assets_have_no_external_references(self):
        for name in ("index.html", "app.css", "app.js"):
            with self.subTest(name=name):
                self.assertNotRegex(self.read(name), re.compile(r"https?://", re.IGNORECASE))

    def test_superseded_run_uses_neutral_badge_and_action_label(self):
        script = self.read("app.js")
        self.assertIn('["SUPERSEDED", "CHAT"]', script)
        self.assertIn('["SUPERSEDED", "Đã thay thế"]', script)

    def test_required_controls_and_labels(self):
        markup = Markup(self.read("index.html"))
        ids = {attrs["id"]: (tag, attrs) for tag, attrs in markup.elements if "id" in attrs}
        self.assertEqual(len(ids), sum("id" in attrs for _, attrs in markup.elements))
        for ident, expected in {
            "login-form": "form", "password": "input", "run-inputs": "div",
            "run-steps": "div", "run-proposal": "div", "run-decision": "div",
            "back-button": "button", "pause-button": "button", "follow-button": "button",
            "runs-list": "ol", "load-more": "button",
            "batch-dashboard": "main", "batches-list": "ol", "batch-messages": "ol",
            "batch-trace": "section", "batch-scope": "select", "batch-rounds": "div",
            "batch-trace-jump": "button", "choose-batch": "button", "run-view-link": "a",
        }.items():
            with self.subTest(ident=ident):
                self.assertEqual(ids[ident][0], expected)
        self.assertEqual(ids["password"][1]["type"], "password")
        self.assertIn(("label", {"for": "password"}), markup.elements)
        self.assertIn(("label", {"for": "batch-scope"}), markup.elements)
        self.assertEqual(ids["batch-trace"][1]["aria-labelledby"], "batch-trace-title")
        self.assertEqual(ids["run-view-link"][1]["href"], "#runs")
        self.assertEqual(ids["pause-button"][1]["aria-label"], "Tạm dừng cập nhật realtime")
        for label in (
            "Mật khẩu", "Đầu vào", "Thinking & công cụ", "Đầu ra của trợ lý",
            "Code quyết định", "Danh sách", "Theo lượt mới nhất", "Tải thêm",
        ):
            with self.subTest(label=label):
                self.assertIn(label, markup.text)


if __name__ == "__main__":
    unittest.main()
