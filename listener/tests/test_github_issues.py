import io
import json
import unittest
from urllib.error import HTTPError, URLError

from tg_triage.github_issues import GitHubError, GitHubIssues, _NoRedirect
from tg_triage.issue_format import Issue
from tests.helpers import FAKE_TOKEN, FakeHTTP


class GitHubTests(unittest.TestCase):
    def issue(self):
        return Issue("[bug] Lỗi thử nghiệm", "Nội dung thử nghiệm", ("bug", "triage", "source:telegram"))

    def test_request_headers_body_timeout_and_result(self):
        http = FakeHTTP({"number": 42, "html_url": "https://github.com/sandbox/triage/issues/42"})
        client = GitHubIssues("sandbox/triage", FAKE_TOKEN, opener=http)
        self.assertEqual(client.create_issue(self.issue()), (42, "https://github.com/sandbox/triage/issues/42"))
        request, timeout = http.calls[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/sandbox/triage/issues")
        self.assertEqual(request.method, "POST")
        self.assertEqual(timeout, 30)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["authorization"], "Bearer ghp_TEST")
        self.assertEqual(headers["accept"], "application/vnd.github+json")
        self.assertEqual(headers["x-github-api-version"], "2022-11-28")
        self.assertEqual(json.loads(request.data)["labels"], list(self.issue().labels))

    def test_http_failure_has_status_and_does_not_echo_body_headers_or_reason(self):
        http = FakeHTTP(HTTPError("https://api.github.com/", 403, "ghp_TEST", {"secret": FAKE_TOKEN},
                                  io.BytesIO(FAKE_TOKEN.encode())))
        with self.assertRaises(GitHubError) as error:
            GitHubIssues("sandbox/triage", FAKE_TOKEN, opener=http).create_issue(self.issue())
        self.assertEqual(str(error.exception), "GitHub HTTP 403: Forbidden")
        self.assertNotIn(FAKE_TOKEN, str(error.exception))
        self.assertEqual(len(http.calls), 1)

    def test_connection_failure_is_safe_and_not_retried(self):
        http = FakeHTTP(URLError(FAKE_TOKEN))
        with self.assertRaisesRegex(GitHubError, "connection failed"):
            GitHubIssues("sandbox/triage", FAKE_TOKEN, opener=http).create_issue(self.issue())
        self.assertEqual(len(http.calls), 1)

    def test_invalid_responses_are_safe(self):
        for response in ({}, b"bad", {"number": True, "html_url": "bad"}):
            http = FakeHTTP(response)
            with self.assertRaisesRegex(GitHubError, "invalid response"):
                GitHubIssues("sandbox/triage", FAKE_TOKEN, opener=http).create_issue(self.issue())

    def test_non_allowlisted_labels_are_rejected_before_http(self):
        http = FakeHTTP()
        with self.assertRaises(ValueError):
            GitHubIssues("sandbox/triage", FAKE_TOKEN, opener=http).create_issue(Issue("x", "x", ("admin",)))
        self.assertEqual(http.calls, [])

    def test_redirects_are_refused_to_protect_authorization(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://attacker.invalid"))
