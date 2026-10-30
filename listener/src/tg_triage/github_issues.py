"""Create an issue once, with fixed labels and safe error messages."""

import json
import hashlib
import logging
import re
from http import HTTPStatus
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import LABELS

LOG = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    """GitHub failure that never contains response bodies or credentials."""


class GitHubConflict(GitHubError):
    """A proven pre-write mismatch: preserve the remotely edited issue."""


class GitHubNotSent(GitHubError):
    """Refresh definitely did not send a PATCH."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubIssues:
    def __init__(self, repo: str, token: str, *, opener=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("GH_REPO: expected owner/repo")
        if not token.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in token):
            raise ValueError("GH_TOKEN: expected non-empty token without control characters")
        self.repo = repo
        self._token = token
        self.opener = build_opener(_NoRedirect()).open if opener is None else opener

    def create_issue(self, issue) -> tuple[int, str | None]:
        if not issue.labels or not set(issue.labels) <= LABELS:
            raise ValueError("Issue labels must come from the fixed label set")
        body = {"title": issue.title, "body": issue.body, "labels": list(issue.labels)}
        request = Request(
            f"https://api.github.com/repos/{self.repo}/issues",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self._token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with self.opener(request, timeout=30) as response:
                data = json.loads(response.read())
            if not isinstance(data, dict):
                raise ValueError
            number = data["number"]
            url = data.get("html_url")
            if type(number) is not int or number <= 0:
                raise ValueError
            expected = f"https://github.com/{self.repo}/issues/{number}"
            if not isinstance(url, str) or url.casefold() != expected.casefold():
                LOG.warning("GitHub URL mismatch issue_number=%s", number)
            # A usable number proves creation even if optional URL metadata is bad.
            # Never echo the returned URL in diagnostics.
            if not isinstance(url, str):
                url = None
            return number, url
        except HTTPError as error:
            code = error.code
            try:
                phrase = HTTPStatus(code).phrase
            except ValueError:
                phrase = "HTTP failure"
            error.close()
            raise GitHubError(f"GitHub HTTP {code}: {phrase}") from None
        except (URLError, OSError):
            raise GitHubError("GitHub connection failed") from None
        except (ValueError, TypeError, KeyError):
            raise GitHubError("GitHub returned an invalid response") from None

    def add_comment(self, issue_number, body):
        """Append once; the caller durably reserves this POST and never retries unknown outcomes."""
        if type(issue_number) is not int or issue_number <= 0 or not isinstance(body, str) or not body.strip():
            raise ValueError("Invalid issue comment")
        request = Request(
            f"https://api.github.com/repos/{self.repo}/issues/{issue_number}/comments",
            data=json.dumps({"body": body}, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self._token}",
                     "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with self.opener(request, timeout=30) as response:
                data = json.loads(response.read(1024 * 1024 + 1))
            comment_id = data.get("id") if isinstance(data, dict) else None
            if type(comment_id) is not int or comment_id <= 0:
                raise ValueError
            return comment_id
        except HTTPError as error:
            error.close()
            raise GitHubError("GitHub comment HTTP failure") from None
        except (URLError, OSError, ValueError, TypeError):
            raise GitHubError("GitHub comment result is unknown") from None

    def _issue_request(self, number, method='GET', payload=None, etag=None):
        if type(number) is not int or number<=0:
            raise ValueError('Invalid fixed issue target')
        from .thinking import CURRENT, BudgetExhausted
        turn=CURRENT.get()
        if turn and method=='GET':
            turn.lookup()
        if turn and turn.is_set():
            if method=='PATCH': raise GitHubNotSent('Refresh deadline before PATCH')
            raise BudgetExhausted('decision_deadline_exhausted')
        headers={"Authorization":f"Bearer {self._token}","Accept":"application/vnd.github+json",
                 "Content-Type":"application/json","X-GitHub-Api-Version":"2022-11-28"}
        request=Request(f'https://api.github.com/repos/{self.repo}/issues/{number}',
            data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None,
            method=method,headers=headers)
        try:
            with self.opener(request,timeout=min(15,max(.1,turn.remaining())) if turn else 15) as response:
                raw=response.read(1024*1024+1)
                if len(raw)>1024*1024: raise ValueError
                data=json.loads(raw)
                response_etag=getattr(response,'headers',{}).get('ETag')
            if (not isinstance(data,dict) or data.get('number')!=number or type(data.get('number')) is not int
                    or data.get('html_url','').casefold()!=f'https://github.com/{self.repo}/issues/{number}'.casefold()
                    or not isinstance(data.get('body'),str) or not isinstance(data.get('title'),str)):
                raise ValueError
            return data,response_etag
        except HTTPError as error:
            conflict=method=='PATCH' and error.code==412
            error.close()
            if conflict: raise GitHubConflict('Issue changed during refresh') from None
            raise GitHubError('GitHub refresh HTTP failure') from None
        except (URLError,OSError,ValueError,TypeError,AttributeError):
            raise GitHubError('GitHub refresh result is unknown') from None

    def refresh_issue(self, number, expected_sha, expected_title, marker, issue, *, expected_labels):
        """One guarded PATCH and readback; caller reserves the operation before invoking."""
        if not issue.labels or not set(issue.labels)<=LABELS:
            raise ValueError('Invalid managed issue labels')
        from .thinking import BudgetExhausted
        try:
            current,etag=self._issue_request(number)
        except (GitHubError,BudgetExhausted):
            raise GitHubNotSent('Refresh preflight failed before PATCH') from None
        digest=hashlib.sha256(current['body'].encode('utf-8')).hexdigest()
        if current.get('state')=='closed':
            raise GitHubConflict('Closed issue cannot be refreshed')
        if digest!=expected_sha or current['title']!=expected_title or marker not in current['body']:
            raise GitHubConflict('Issue has manual changes or wrong source')
        labels=[label.get('name') if isinstance(label,dict) else label for label in current.get('labels',[])]
        labels=[label for label in labels if isinstance(label,str)]
        if set(labels)&LABELS != set(expected_labels):
            raise GitHubConflict('Issue owned labels changed')
        preserved=[label for label in labels if label not in LABELS]
        desired=list(dict.fromkeys(preserved+list(issue.labels)))
        self._issue_request(number,'PATCH',dict(title=issue.title,body=issue.body,labels=desired))
        verified,_=self._issue_request(number)
        actual={label.get('name') if isinstance(label,dict) else label for label in verified.get('labels',[])}
        if (verified['body']!=issue.body or verified['title']!=issue.title or marker not in verified['body']
                or not set(desired)<=actual):
            raise GitHubError('GitHub refresh readback did not verify')
        return number

    def lifecycle_snapshot(self, number):
        """GET an exact host-selected issue; returned URLs never choose a destination."""
        from datetime import datetime, timezone
        data,_=self._issue_request(number)
        if data.get('state') not in ('open','closed') or 'pull_request' in data:
            raise GitHubError('Invalid issue lifecycle state')
        return dict(number=number,repo=self.repo,state=data['state'],
                    observed_at=datetime.now(timezone.utc).isoformat(),updated_at=data.get('updated_at'))

    def verify_created_issue(self, number, issue):
        """Verify a known successful POST result without ever repeating creation."""
        data,_=self._issue_request(number)
        labels={item.get('name') if isinstance(item,dict) else item for item in data.get('labels',[])}
        if (data['title']!=issue.title or data['body']!=issue.body or
                not set(issue.labels)<=labels or 'pull_request' in data):
            raise GitHubError('Created issue readback did not verify')
        return number
