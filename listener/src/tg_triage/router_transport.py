"""Bounded OpenAI function-calling transport for the local 9router only.

No proxy, redirect, hosted-provider defaults, or output-token limit is used.
The default transport owns its socket through completion; a deadline watchdog
interrupts stalled headers and trickling bodies without leaving HTTP workers.
"""
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
import http.client
import json
import math
import re
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request


_ENDPOINT = re.compile(r"http://(127\.0\.0\.1|localhost):([0-9]{1,5})/v1", re.ASCII)
_RESPONSE_LIMIT = 4 * 1024 * 1024
CAPTURE_TEXT_BYTES = 65536
CAPTURE_TRUNCATION = '…[truncated]'
_TRUNCATED = CAPTURE_TRUNCATION.encode('utf-8')
_KINDS = frozenset({"transient", "auth", "schema", "truncated", "too_large",
                    "invalid_endpoint", "rate_limited"})


class RouterError(Exception):
    """Only a fixed error class is exposed, never response or credential text."""

    def __init__(self, kind: str):
        self.kind = kind if kind in _KINDS else "schema"
        super().__init__(self.kind)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class RouterReply:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int | None
    cached_tokens: int
    model: str | None
    latency_ms: int
    reasoning_text: str | None = field(default=None, repr=False)


def bounded_text(value: str | None, limit: int = CAPTURE_TEXT_BYTES) -> str | None:
    if value is None:
        return None
    encoded = value[:limit].encode('utf-8', errors='replace')
    if len(value) > limit or len(encoded) > limit:
        return encoded[:limit - len(_TRUNCATED)].decode('utf-8', errors='ignore') + CAPTURE_TRUNCATION
    return encoded.decode('utf-8')


def _parse(raw: bytes, latency_ms: int) -> RouterReply:
    try:
        envelope = json.loads(raw)
        choice = envelope["choices"][0]
        finish = choice.get("finish_reason")
        if finish == "length":
            raise RouterError("truncated")
        message = choice["message"]
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError
        calls = []
        for call in message.get("tool_calls") or ():
            if call.get("type") != "function":
                raise ValueError
            function = call["function"]
            values = (call["id"], function["name"], function["arguments"])
            if any(not isinstance(value, str) for value in values):
                raise ValueError
            calls.append(ToolCall(*values))
        if content is None and not calls:
            raise ValueError
        model = envelope.get("model")
        if ((finish is not None and not isinstance(finish, str))
                or (model is not None and not isinstance(model, str))):
            raise ValueError
        usage = envelope.get("usage") or {}
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        if any(type(value) is not int or value < 0 for value in (prompt, completion, cached)):
            raise ValueError
        if reasoning is not None and (type(reasoning) is not int or reasoning < 0):
            raise ValueError
        reasoning_text = message.get('reasoning_content')
        return RouterReply(content, tuple(calls), finish, prompt, completion,
                           reasoning, cached, model, latency_ms,
                           bounded_text(reasoning_text) if isinstance(reasoning_text, str) else None)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError):
        raise RouterError("schema") from None


def _status_error(status: int, raw: bytes) -> None:
    if status == 200:
        return
    if status in (401, 403):
        kind = "auth"
    elif status == 429:
        kind = "rate_limited"
    elif status == 413:
        kind = "too_large"
    elif status == 408 or 500 <= status <= 599:
        kind = "transient"
    else:
        # Inspect only the bounded error body; never include it in an exception.
        lower = raw.lower()
        kind = "too_large" if any(marker in lower for marker in (
            b"context_length_exceeded", b"context window", b"maximum context length",
            b"context length", b"context_length", b"request_too_large")) else "schema"
    raise RouterError(kind)


def validate_base_url(base_url):
    """Validate configuration with exactly the wire transport's endpoint rules."""
    match = _ENDPOINT.fullmatch(base_url) if isinstance(base_url, str) else None
    if not match or not 1 <= int(match[2]) <= 65535:
        raise RouterError("invalid_endpoint")
    return base_url


def chat(base_url: str, key: str, model: str, messages: list[dict], *,
         tools: Sequence[dict] = (), timeout: float, opener=None,
         clock=time.monotonic) -> RouterReply:
    """POST one request. Injected openers follow urllib's (request, timeout=) API.

    An opener can be a callable or an object with ``open``. It must honour the
    supplied timeout; production uses a pinned socket and a total-deadline timer.
    """
    validate_base_url(base_url)
    match = _ENDPOINT.fullmatch(base_url)
    started = clock()
    if not math.isfinite(timeout) or timeout <= 0:
        raise RouterError("transient")
    deadline = started + timeout

    def remaining():
        seconds = deadline - clock()
        if seconds <= 0:
            raise RouterError("transient")
        return seconds

    connection = response = watchdog = None
    expired = threading.Event()
    try:
        # 9router streams SSE for some providers (ag/gemini-*) unless told not to.
        body = {"model": model, "messages": messages, "stream": False}
        if tools:
            body.update(tools=list(tools), tool_choice="auto")
        request = Request(base_url + "/chat/completions",
                          data=json.dumps(body).encode("utf-8"), method="POST",
                          headers={"Content-Type": "application/json",
                                   "Authorization": "Bearer " + key})
        if opener is not None:
            try:
                response = getattr(opener, "open", opener)(request, timeout=remaining())
            except HTTPError as error:
                response = error
        else:
            # Pin localhost to IPv4 loopback: no DNS, proxies, or redirects.
            connection = http.client.HTTPConnection("127.0.0.1", int(match[2]),
                                                    timeout=remaining())
            connection.connect()
            sock = connection.sock

            def expire():
                expired.set()
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            watchdog = threading.Timer(remaining(), expire)
            watchdog.daemon = True
            watchdog.start()
            sock.settimeout(remaining())
            connection.request("POST", "/v1/chat/completions", request.data,
                               dict(request.header_items()))
            response = connection.getresponse()
        remaining()
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        # Classify these without waiting for a possibly stalled error body.
        if status in (401, 403, 408, 413, 429) or 500 <= status <= 599:
            _status_error(status, b"")
        chunks = []
        size = 0
        read = getattr(response, "read1", response.read)
        while True:
            remaining()
            chunk = read(min(65536, _RESPONSE_LIMIT + 1 - size))
            remaining()
            if expired.is_set():
                raise RouterError("transient")
            if not chunk:
                break
            size += len(chunk)
            if size > _RESPONSE_LIMIT:
                raise RouterError("too_large")
            chunks.append(chunk)
        raw = b"".join(chunks)
        _status_error(status, raw)
        result = _parse(raw, max(0, int((clock() - started) * 1000)))
        remaining()
        return result
    except RouterError:
        raise
    except (OSError, URLError, http.client.HTTPException):
        raise RouterError("transient") from None
    except Exception:
        raise RouterError("schema") from None
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if response is not None:
            with suppress(Exception):
                response.close()
        if connection is not None:
            with suppress(Exception):
                connection.close()
