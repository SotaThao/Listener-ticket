"""Offline transport contract tests, including real loopback deadline checks."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
import threading
import time
import unittest
from urllib.error import HTTPError, URLError

from tg_triage.router_transport import RouterError, ToolCall, chat


BASE = "http://127.0.0.1:20128/v1"
KEY = "private-test-key-never-log"


def envelope(**overrides):
    value = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    value.update(overrides)
    return value


class Response(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.status = status


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else Response(envelope())
        self.error = error
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


@contextmanager
def local_server(mode="normal"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.server.received = (self.path, self.headers,
                                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            try:
                if mode == "headers":
                    time.sleep(0.5)
                if mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://example.invalid/secret")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps(envelope()).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if mode == "body":
                    time.sleep(0.5)
                if mode == "trickle":
                    for byte in payload:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.03)
                else:
                    self.wfile.write(payload)
            except (OSError, ConnectionError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class RouterTransportTests(unittest.TestCase):
    def call(self, opener, **kwargs):
        return chat(BASE, KEY, "ag/test", [], timeout=kwargs.pop("timeout", 2),
                    opener=opener, **kwargs)

    def assert_kind(self, kind, function):
        with self.assertRaises(RouterError) as caught:
            function()
        self.assertEqual(caught.exception.kind, kind)
        self.assertEqual(str(caught.exception), kind)
        self.assertNotIn(KEY, str(caught.exception))

    def test_endpoint_validation_before_io(self):
        invalid = ["https://localhost:80/v1", "http://example.com:80/v1",
                   "http://127.0.0.2:80/v1", "http://[::1]:80/v1", "http://localhost/v1",
                   "http://localhost:0/v1", "http://localhost:65536/v1",
                   "http://user:password@localhost:80/v1", "http://localhost:80/v1?",
                   "http://localhost:80/v1#", "http://localhost:80/v1/",
                   "http://localhost:80/other", "http://localhost:80/v1\n",
                   "http://LOCALHOST:80/v1", None]
        opener = Opener()
        for base in invalid:
            with self.subTest(base=base):
                self.assert_kind("invalid_endpoint",
                                 lambda: chat(base, KEY, "test", [], timeout=2, opener=opener))
        self.assertEqual(opener.requests, [])

    def test_body_tools_images_headers_and_no_token_cap(self):
        opener = Opener()
        messages = [{"role": "system", "content": "pack"}, {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}]}]
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
        chat(BASE, KEY, "ag/test", messages, tools=tools, timeout=2, opener=opener)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, BASE + "/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {"model": "ag/test", "messages": messages,
                                                  "stream": False, "tools": tools, "tool_choice": "auto"})
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 2)
        self.assertTrue(opener.response.closed)

    def test_no_tools_omits_tool_fields_and_callable_opener(self):
        opener = Opener()
        self.call(opener.open)
        self.assertEqual(json.loads(opener.requests[0][0].data)["stream"], False)
        self.assertEqual(set(json.loads(opener.requests[0][0].data)), {"model", "messages", "stream"})

    def test_tool_calls_usage_and_immutable_records(self):
        arguments = '{ "query": "test" }'
        reply = envelope(choices=[{"message": {"content": None, "tool_calls": [
            {"type": "function", "id": "call-1", "function": {"name": "lookup", "arguments": arguments}},
            {"type": "function", "id": "call-2", "function": {"name": "lookup", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}], model="ag/actual", usage={"prompt_tokens": 30,
            "completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 7},
            "prompt_tokens_details": {"cached_tokens": 20}})
        result = self.call(Opener(Response(reply)), clock=lambda: 50)
        self.assertIsNone(result.content)
        self.assertEqual(result.tool_calls, (ToolCall("call-1", "lookup", arguments),
                                             ToolCall("call-2", "lookup", "{}")))
        self.assertEqual((result.prompt_tokens, result.completion_tokens, result.reasoning_tokens,
                          result.cached_tokens, result.model, result.finish_reason, result.latency_ms),
                         (30, 10, 7, 20, "ag/actual", "tool_calls", 0))
        with self.assertRaises(FrozenInstanceError):
            result.content = "changed"
        with self.assertRaises(FrozenInstanceError):
            result.tool_calls[0].name = "changed"

    def test_usage_defaults(self):
        result = self.call(Opener(Response(envelope(choices=[{
            "message": {"content": "{}", "tool_calls": None}, "finish_reason": None}]))))
        self.assertEqual((result.prompt_tokens, result.completion_tokens, result.cached_tokens), (0, 0, 0))
        self.assertIsNone(result.reasoning_tokens)
        self.assertIsNone(result.model)

    def test_http_error_mapping_and_safe_messages(self):
        for status, kind in [(401, "auth"), (403, "auth"), (408, "transient"),
                             (500, "transient"), (503, "transient"), (599, "transient"),
                             (429, "rate_limited"), (413, "too_large"), (404, "schema")]:
            with self.subTest(status=status):
                response = Response(KEY.encode(), status)
                self.assert_kind(kind, lambda: self.call(Opener(response)))
                self.assertTrue(response.closed)
        error = HTTPError(BASE, 401, KEY, {}, io.BytesIO(KEY.encode()))
        self.assert_kind("auth", lambda: self.call(Opener(error=error)))
        self.assertTrue(error.fp.closed)

    def test_context_length_error(self):
        for marker in ("context_length_exceeded", "maximum context length", "context window exceeded"):
            with self.subTest(marker=marker):
                self.assert_kind("too_large", lambda: self.call(Opener(Response(
                    {"error": {"code": marker, "message": KEY}}, 400))))

    def test_connection_and_timeout_mapping(self):
        for error in (TimeoutError(KEY), socket.timeout(KEY), ConnectionError(KEY),
                      URLError(KEY), OSError(KEY)):
            with self.subTest(error=type(error)):
                self.assert_kind("transient", lambda: self.call(Opener(error=error)))

    def test_invalid_json_missing_choices_and_malformed_fields(self):
        for payload in (b"not-json " + KEY.encode(), b"\xff", {}, {"choices": []},
                        {"choices": None}, {"choices": [None]}, [],
                        envelope(usage={"prompt_tokens": "secret"}),
                        envelope(usage={"completion_tokens_details": [1]}),
                        envelope(choices=[{"message": {}}]),
                        envelope(choices=[{"message": {"content": 42}}]),
                        envelope(choices=[{"message": {"tool_calls": [{"type": "function"}]}}])):
            with self.subTest(payload=repr(payload)[:60]):
                self.assert_kind("schema", lambda: self.call(Opener(Response(payload))))

    def test_length_finish_reason_is_truncated(self):
        payload = envelope(choices=[{"message": {"content": "partial"}, "finish_reason": "length"}])
        self.assert_kind("truncated", lambda: self.call(Opener(Response(payload))))

    def test_response_size_limit(self):
        self.assert_kind("too_large", lambda: self.call(Opener(Response(b"x" * (4 * 1024 * 1024 + 1)))))
        # Exactly 4 MiB is accepted when the document is valid JSON.
        raw = json.dumps(envelope()).encode()
        result = self.call(Opener(Response(raw + b" " * (4 * 1024 * 1024 - len(raw)))))
        self.assertEqual(result.content, "{}")

    def test_fake_clock_deadline_and_invalid_timeout(self):
        now = [0.0]

        def slow_open(request, *, timeout):
            now[0] += timeout + 0.01
            return Response(envelope())

        self.assert_kind("transient", lambda: self.call(slow_open, clock=lambda: now[0]))
        for timeout in (0, -1, float("inf"), float("nan")):
            opener = Opener()
            self.assert_kind("transient", lambda: self.call(opener, timeout=timeout))
            self.assertFalse(opener.requests)

    def test_real_loopback_post_and_localhost(self):
        with local_server() as (server, base):
            for endpoint in (base, base.replace("127.0.0.1", "localhost")):
                self.assertEqual(chat(endpoint, KEY, "test", [], timeout=2).content, "{}")
                path, headers, body = server.received
                self.assertEqual(path, "/v1/chat/completions")
                self.assertEqual(headers["Authorization"], "Bearer " + KEY)
                self.assertEqual(body, {"model": "test", "messages": [], "stream": False})

    def test_real_deadline_headers_body_and_trickle(self):
        for mode in ("headers", "body", "trickle"):
            with self.subTest(mode=mode), local_server(mode) as (_, base):
                started = time.monotonic()
                self.assert_kind("transient", lambda: chat(base, KEY, "test", [], timeout=0.18))
                self.assertLess(time.monotonic() - started, 0.9)

    def test_redirect_is_not_followed(self):
        with local_server("redirect") as (_, base):
            self.assert_kind("schema", lambda: chat(base, KEY, "test", [], timeout=2))


if __name__ == "__main__":
    unittest.main()
