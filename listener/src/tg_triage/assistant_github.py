"""Read-only GitHub lookups; ticket text is untrusted data, never instructions."""

import json
import re
from collections.abc import Sequence
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .assistant_errors import ToolDenied, ToolUnavailable


class GitHubReader:
    def __init__(self, token: str | None, repos: Sequence[str], *,
                 timeout: float = 10.0, opener=None,
                 api_url: str = "https://api.github.com"):
        self._token = token
        self._repos = {}
        for repo in repos:
            if not isinstance(repo, str) or not re.fullmatch(
                    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                raise ToolDenied("Invalid GitHub repository allowlist")
            self._repos.setdefault(repo.casefold(), repo)
        self._timeout = timeout
        self._opener = urlopen if opener is None else opener
        self._api_url = api_url.rstrip("/")

    def _repo(self, repo):
        if not isinstance(repo, str) or repo.casefold() not in self._repos:
            raise ToolDenied("GitHub repository is not allowed")
        return self._repos[repo.casefold()]

    def _get(self, path):
        if not self._token or not self._token.strip():
            raise ToolUnavailable("GitHub token not configured")
        if not self._repos:
            raise ToolUnavailable("GitHub repositories not configured")
        try:
            request = Request(
                self._api_url + path, method="GET",
                headers={"Authorization": f"Bearer {self._token}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"})
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except HTTPError as error:
            error.close()
            raise ToolUnavailable("GitHub API request failed") from None
        except (URLError, OSError, HTTPException):
            raise ToolUnavailable("GitHub connection failed") from None
        except (ValueError, TypeError):
            raise ToolUnavailable("GitHub returned an invalid response") from None

    @staticmethod
    def _summary(item, repo):
        if (not isinstance(item, dict) or type(item.get("number")) is not int
                or item["number"] <= 0 or not isinstance(item.get("title"), str)
                or item.get("state") not in ("open", "closed")):
            raise ToolUnavailable("GitHub returned an invalid ticket")
        return dict(repo=repo, number=item["number"], title=item["title"],
                    state=item["state"])

    def search_tickets(self, query: str, repo: str | None = None) -> list[dict]:
        """Search a literal phrase; reject caller-supplied scope qualifiers."""
        repos = [self._repo(repo)] if repo is not None else list(self._repos.values())
        if not isinstance(query, str) or re.search(r"(?:repo|org|user)\s*:", query, re.I):
            raise ToolDenied("GitHub search scope qualifiers are not allowed")
        # Quotes prevent OR, negation and parentheses from changing the repo scope.
        phrase = query.replace("\\", "\\\\").replace('"', '\\"')
        scope = " ".join(f"repo:{name}" for name in repos)
        search = f'"{phrase}" {scope} is:issue'
        data = self._get("/search/issues?" + urlencode({"q": search, "per_page": 10}))
        items = data.get("items")
        if not isinstance(items, list):
            raise ToolUnavailable("GitHub returned an invalid search response")
        result = []
        allowed = {name.casefold() for name in repos}
        for item in items:
            if not isinstance(item, dict):
                raise ToolUnavailable("GitHub returned an invalid ticket")
            url = item.get("repository_url", "")
            prefix = self._api_url + "/repos/"
            if not isinstance(url, str) or not url.startswith(prefix):
                raise ToolUnavailable("GitHub returned an invalid repository")
            name = url[len(prefix):]
            if name.casefold() not in allowed:
                continue
            if "pull_request" in item:
                continue
            result.append(self._summary(item, self._repo(name)))
            if len(result) == 10:
                break
        return result

    def get_ticket(self, repo: str, number: int) -> dict:
        repo = self._repo(repo)
        if type(number) is not int or number <= 0:
            raise ToolDenied("GitHub ticket number must be a positive integer")
        data = self._get(f"/repos/{repo}/issues/{number}")
        result = self._summary(data, repo)
        repository_url = data.get("repository_url")
        if (not isinstance(repository_url, str)
                or repository_url.casefold() != (self._api_url + "/repos/" + repo).casefold()):
            raise ToolDenied("GitHub ticket repository does not match request")
        body = data.get("body")
        labels = data.get("labels", [])
        if (result["number"] != number or "pull_request" in data
                or (body is not None and not isinstance(body, str))
                or not isinstance(labels, list)):
            raise ToolUnavailable("GitHub returned an invalid ticket")
        names = []
        for label in labels:
            name = label.get("name") if isinstance(label, dict) else label
            if not isinstance(name, str):
                raise ToolUnavailable("GitHub returned invalid ticket labels")
            names.append(name)
        try:
            encoded = (body or "").encode("utf-8")
        except UnicodeError:
            raise ToolUnavailable("GitHub returned invalid ticket text") from None
        result.update(labels=names, body=encoded[:4096].decode("utf-8", errors="ignore"),
                      body_truncated=len(encoded) > 4096)
        return result
