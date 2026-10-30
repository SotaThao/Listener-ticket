"""Hosted model with images, local Ollama backup, usage accounting, pricing and the console summary.

Offline: a fake upstream replaces the network (`transport=`), fake HTTP replaces Ollama, SQLite is
in-memory or temporary. No test here may reach the real network."""

import base64
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from tg_triage import ai_transport, usage as usage_module
from tg_triage.ai_transport import ProviderError, complete, usage_of
from tg_triage.config import ConfigError, ModelConfig, PricingConfig, load_config
from tg_triage.provider import build_provider
from tg_triage.store import Store
from tg_triage.usage import UsageRecorder, build_summary, price
from tests.helpers import EXAMPLE, JPG, PNG, WEBP, FakeHTTP, config, message

REAL_COMPLETE = ai_transport.complete
KEY_ENV = {"AGENTFLEET_KEY_SCOPE": "deepseek|https://api.deepseek.com", "AGENTFLEET_API_KEY": "test-key"}
CASE = json.dumps({"verdict": "ticket", "type": "bug", "title": "Lỗi đăng nhập", "summary": "Không đăng nhập được.",
                   "priority": "high", "confidence": 0.9, "key_message_ids": [1]}, ensure_ascii=False)
DEEPSEEK_USAGE = {"prompt_tokens": 3024, "completion_tokens": 1300, "prompt_cache_hit_tokens": 1000,
                  "prompt_cache_miss_tokens": 2024}
OLLAMA_ENVELOPE = {"message": {"content": CASE}, "prompt_eval_count": 410, "eval_count": 77}


def deepseek_envelope(content=CASE, usage=None):
    return {"choices": [{"message": {"content": content}}], "usage": usage or DEEPSEEK_USAGE}


class Upstream:
    """Fake hosted API: records every payload, answers from a script (dict, or an exception to raise)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, payload, headers, timeout, provider):
        self.calls.append((url, payload, headers, provider))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def model_config(**extra):
    fields = dict(provider="deepseek", name="deepseek-flash", think=False, base_url="https://api.deepseek.com",
                  cloud_consent=True, cloud_image_consent=True)
    fields.update(extra)
    return ModelConfig(**fields)


class ProviderBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 10, 14, 12, 0).timestamp()
        patch.dict(os.environ, KEY_ENV).start()
        self.addCleanup(patch.stopall)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def provider(self, upstream, http=None, **model):
        self.upstream = upstream
        self.http = http or FakeHTTP()
        patch("tg_triage.ai_transport.complete",
              lambda *a, **k: REAL_COMPLETE(*a, transport=upstream, **k)).start()
        settings = replace(config(), model=model_config(**model))
        self.recorder = UsageRecorder(self.store, settings, clock=lambda: self.now, trailing=False)
        return build_provider(settings, opener=self.http, prompt="P", case_prompt="CASE",
                              topics_prompt="TOPICS", usage=self.recorder)

    def image(self, name, data):
        path = Path(self.tmp.name) / name
        path.write_bytes(data)
        return path

    def rows(self):
        return [dict(zip(("ts", "provider", "model", "role", "outcome", "prompt", "completion", "hit", "cost"), row))
                for row in self.store.model_usage_since(0)]


class HostedImageTests(ProviderBase):
    def test_deepseek_gets_images_as_content_parts_and_text_is_redacted(self):
        for name, data, mime in (("a.jpg", JPG, "image/jpeg"), ("b.png", PNG, "image/png"),
                                 ("c.webp", WEBP, "image/webp")):
            with self.subTest(mime=mime):
                provider = self.provider(Upstream(deepseek_envelope()))
                text = message(2, "liên hệ owner@example.com hoặc 0901234567 về lỗi")
                result = provider.analyze_case([message(1, ""), text], images={1: self.image(name, data)})
                self.assertEqual(result.verdict, "ticket")
                (url, payload, headers, kind), = self.upstream.calls
                self.assertEqual((kind, url), ("deepseek", "https://api.deepseek.com/chat/completions"))
                self.assertEqual(headers["Authorization"], "Bearer test-key")
                user = [m for m in payload["messages"] if isinstance(m["content"], list)]
                self.assertEqual(len(user), 1)
                self.assertEqual(user[0]["role"], "user")
                kinds = [part["type"] for part in user[0]["content"]]
                self.assertEqual(kinds, ["text", "image_url"])
                url_value = user[0]["content"][1]["image_url"]["url"]
                self.assertTrue(url_value.startswith(f"data:{mime};base64,"))
                self.assertEqual(base64.b64decode(url_value.split(",", 1)[1]), data)
                self.assertTrue(all(isinstance(m["content"], str) for m in payload["messages"]
                                    if m["role"] == "system"))
                wire = json.dumps(payload)
                self.assertNotIn("owner@example.com", wire)
                self.assertNotIn("0901234567", wire)
                self.assertNotIn('"images"', wire)
                self.assertEqual(self.http.calls, [])   # the hosted answer was enough: Ollama untouched

    def test_without_image_consent_the_images_go_to_local_ollama_only(self):
        provider = self.provider(Upstream(), FakeHTTP(OLLAMA_ENVELOPE), cloud_image_consent=False)
        self.assertTrue(provider.supports_vision)
        self.assertFalse(provider.rejects_images)
        result = provider.analyze_case([message(1, ""), message(2, "xem ảnh lỗi")], images={1: self.image("a.jpg", JPG)})
        self.assertEqual(result.verdict, "ticket")
        self.assertEqual(self.upstream.calls, [])
        payload = json.loads(self.http.calls[0][0].data)
        self.assertEqual(base64.b64decode(payload["messages"][1]["images"][0]), JPG)
        self.assertEqual([r["role"] for r in self.rows()], ["fallback"])
        self.assertEqual(self.rows()[0]["provider"], "ollama")

    def test_anthropic_never_gets_images_and_goes_local(self):
        provider = self.provider(Upstream(), FakeHTTP(OLLAMA_ENVELOPE), provider="anthropic",
                                 base_url="https://api.anthropic.com")
        provider.analyze_case([message(1, ""), message(2, "xem ảnh")], images={1: self.image("a.jpg", JPG)})
        self.assertEqual(self.upstream.calls, [])
        self.assertEqual(len(self.http.calls), 1)

    def test_an_unknown_image_type_is_never_sent_to_the_hosted_model(self):
        provider = self.provider(Upstream(), FakeHTTP(OLLAMA_ENVELOPE))
        provider.analyze_case([message(1, ""), message(2, "xem")], images={1: self.image("a.gif", b"GIF89a" + b"0" * 40)})
        self.assertEqual(self.upstream.calls, [])
        self.assertEqual(len(self.http.calls), 1)

    def test_fallback_none_without_image_consent_keeps_the_old_refusal(self):
        provider = self.provider(Upstream(), fallback="none", cloud_image_consent=False)
        self.assertFalse(provider.supports_vision)
        self.assertTrue(provider.rejects_images)
        self.assertIsNone(provider.analyze_case([message(1, ""), message(2, "x")], images={1: self.image("a.jpg", JPG)}))
        self.assertEqual((self.upstream.calls, self.http.calls, self.rows()), ([], [], []))

    def test_complete_validates_images_and_raises_the_input_limit_only_for_them(self):
        parts = [{"type": "text", "text": "t"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
        ok = Upstream(deepseek_envelope("{}"))
        big = [{"role": "user", "content": [parts[0], {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + "A" * (1024 * 1024)}}]}]
        self.assertEqual(complete("deepseek", "https://api.deepseek.com", "m", big, {}, key="k", transport=ok), {})
        with self.assertRaises(ProviderError) as error:
            complete("deepseek", "https://api.deepseek.com", "m", [{"role": "user", "content": "x" * (129 * 1024)}],
                     {}, key="k", transport=ok)
        self.assertEqual(error.exception.code, "input_too_large")
        huge = [{"role": "user", "content": [parts[0], {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + "A" * (21 * 1024 * 1024)}}]}]
        with self.assertRaises(ProviderError) as error:
            complete("deepseek", "https://api.deepseek.com", "m", huge, {}, key="k", transport=ok)
        self.assertEqual(error.exception.code, "input_too_large")
        for provider, base, code in (("anthropic", "https://api.anthropic.com", "images_unsupported"),
                                     ("deepseek", "https://api.deepseek.com", "invalid_image")):
            bad = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://evil.example/x.png"}}]}]
            if provider == "anthropic":
                bad = [{"role": "user", "content": parts}]
            with self.subTest(provider=provider), self.assertRaises(ProviderError) as error:
                complete(provider, base, "m", bad, {}, key="k", transport=ok)
            self.assertEqual(error.exception.code, code)
        self.assertEqual(len(ok.calls), 1)


class RedactTests(ProviderBase):
    def test_timestamps_and_bracketed_ids_survive_real_contact_data_does_not(self):
        keep = ("[416] LEO Nguyen · 2026-10-15 08:51 [kèm ảnh]: nên thêm ký tự",
                "[123456789] Lan · 2026-10-15 08:51:30: ok",
                "[123456789] [trả lời 123456780] Lan · 2026-10-15 08:51: ok",
                "họp lúc 20:30 ngày 15.10.2026 hoặc 2026-10-15",
                "TOÀN BỘ CASE, các ID bắt buộc: 12345678, 12345679. Đọc tất cả")
        for text in keep:
            with self.subTest(text=text):
                self.assertEqual(ai_transport.redact(text), text)
        for phone in ("0912 345 678", "+84 912 345 678", "0912.345.678", "(028) 3822 1234", "0912345678", "0912-345-678"):
            with self.subTest(phone=phone):
                self.assertEqual(ai_transport.redact(f"gọi {phone} nhé"), "gọi [phone] nhé")
        self.assertEqual(ai_transport.redact("mail owner@example.com"), "mail [email]")
        # a phone typed right before a time keeps the time; a phone in brackets inside the text is still hidden
        self.assertEqual(ai_transport.redact("0912345678 08:51"), "[phone] 08:51")
        self.assertEqual(ai_transport.redact("x [0912345678] y"), "x [[phone]] y")

    def test_a_full_transcript_line_with_time_long_id_and_phone(self):
        line = ("[123456789] [trả lời 123456780] Lan · 2026-10-15 08:51 [kèm ảnh]: "
                "gọi 0912 345 678 hoặc owner@example.com lúc 20:30")
        self.assertEqual(ai_transport.redact(line), "[123456789] [trả lời 123456780] Lan · 2026-10-15 08:51 [kèm ảnh]: "
                                                    "gọi [phone] hoặc [email] lúc 20:30")

    def test_the_hosted_payload_keeps_the_transcript_timestamp_and_ids(self):
        provider = self.provider(Upstream(deepseek_envelope()))
        stamp = f"{message(1).received_at.astimezone():%Y-%m-%d %H:%M}"
        provider.analyze_case([message(123456789, ""), message(123456790, "gọi 0912 345 678", reply_to_id=123456789)],
                              images={123456789: self.image("a.jpg", JPG)})
        wire = json.dumps(self.upstream.calls[0][1], ensure_ascii=False)
        self.assertIn(f"[123456789] Người thử nghiệm · {stamp}", wire)
        self.assertIn("[123456790] [trả lời 123456789]", wire)
        self.assertIn("các ID bắt buộc: 123456789, 123456790", wire)
        self.assertNotIn("0912", wire)
        self.assertIn("[phone]", wire)


class FallbackTests(ProviderBase):
    def ask(self, provider):
        return provider.analyze_case([message(1, "đăng nhập bị lỗi")])

    def test_primary_error_runs_the_same_request_on_ollama_and_marks_roles(self):
        provider = self.provider(Upstream(ProviderError("rate_limited")), FakeHTTP(OLLAMA_ENVELOPE))
        self.assertEqual(self.ask(provider).verdict, "ticket")
        rows = self.rows()
        self.assertEqual([(r["role"], r["provider"], r["model"], r["outcome"]) for r in rows],
                         [("primary", "deepseek", "deepseek-flash", "rate_limited"),
                          ("fallback", "ollama", "qwen3.5:9b", "ok")])
        self.assertEqual((rows[1]["prompt"], rows[1]["completion"], rows[1]["cost"]), (410, 77, 0))
        sent = json.loads(self.http.calls[0][0].data)
        self.assertEqual((sent["model"], sent["think"]), ("qwen3.5:9b", True))
        self.assertEqual(sent["messages"][1]["content"].count("đăng nhập bị lỗi"), 1)

    def test_fallback_model_and_think_are_configurable(self):
        provider = self.provider(Upstream(ProviderError("timeout")), FakeHTTP(OLLAMA_ENVELOPE),
                                 fallback_model="qwen3:4b", fallback_think=False)
        self.ask(provider)
        sent = json.loads(self.http.calls[0][0].data)
        self.assertEqual((sent["model"], sent["think"]), ("qwen3:4b", False))
        self.assertEqual(self.rows()[-1]["model"], "qwen3:4b")

    def test_broken_json_and_unusable_answers_fall_back_after_the_normal_retry(self):
        provider = self.provider(Upstream(deepseek_envelope("not json"), deepseek_envelope("{}"),
                                          deepseek_envelope("{}")), FakeHTTP(OLLAMA_ENVELOPE))
        self.assertEqual(self.ask(provider).verdict, "ticket")
        self.assertEqual([(r["role"], r["outcome"]) for r in self.rows()],
                         [("primary", "invalid_json"), ("fallback", "ok")])

    def test_parse_returning_none_is_an_error_row_then_fallback(self):
        provider = self.provider(Upstream(deepseek_envelope("{}"), deepseek_envelope("{}")), FakeHTTP(OLLAMA_ENVELOPE))
        self.assertEqual(self.ask(provider).verdict, "ticket")
        self.assertEqual([(r["role"], r["outcome"]) for r in self.rows()],
                         [("primary", "invalid_output"), ("primary", "invalid_output"), ("fallback", "ok")])
        self.assertEqual(self.rows()[0]["prompt"], 3024)   # tokens of a wasted call are still counted

    def test_missing_key_or_consent_goes_to_ollama(self):
        with patch.dict(os.environ, {"AGENTFLEET_API_KEY": ""}):
            provider = self.provider(Upstream(), FakeHTTP(OLLAMA_ENVELOPE))
            self.assertEqual(self.ask(provider).verdict, "ticket")
            self.assertEqual([(r["role"], r["outcome"]) for r in self.rows()],
                             [("primary", "key_required"), ("fallback", "ok")])
        provider = self.provider(Upstream(), FakeHTTP(OLLAMA_ENVELOPE), cloud_consent=False)
        self.assertEqual(self.ask(provider).verdict, "ticket")
        self.assertEqual(self.upstream.calls, [])
        self.assertEqual([r["role"] for r in self.rows()][-1:], ["fallback"])

    def test_fallback_failing_too_returns_none_for_needs_human(self):
        provider = self.provider(Upstream(ProviderError("provider_failed")), FakeHTTP(URLError("refused")))
        self.assertIsNone(self.ask(provider))
        self.assertEqual([(r["role"], r["outcome"]) for r in self.rows()],
                         [("primary", "provider_failed"), ("fallback", "provider_failed")])

    def test_fallback_none_keeps_the_old_none_on_failure(self):
        provider = self.provider(Upstream(ProviderError("timeout")), fallback="none")
        self.assertIsNone(self.ask(provider))
        self.assertEqual(self.http.calls, [])
        self.assertEqual([r["role"] for r in self.rows()], ["primary"])

    def test_a_good_hosted_answer_records_one_primary_row_with_cost(self):
        provider = self.provider(Upstream(deepseek_envelope()))
        self.assertEqual(self.ask(provider).verdict, "ticket")
        row, = self.rows()
        self.assertEqual((row["role"], row["outcome"], row["prompt"], row["completion"], row["hit"]),
                         ("primary", "ok", 3024, 1300, 1000))
        self.assertAlmostEqual(row["cost"], price(PricingConfig(), usage_of("deepseek", deepseek_envelope()), self.now), 6)
        self.assertGreater(row["cost"], 0)

    def test_nothing_about_the_message_is_stored(self):
        provider = self.provider(Upstream(ProviderError("timeout")), FakeHTTP(OLLAMA_ENVELOPE))
        provider.analyze_case([message(1, "SECRET-TEXT-123 owner@example.com")])
        dump = json.dumps(self.store.model_usage_since(0))
        self.assertNotIn("SECRET", dump)
        self.assertNotIn("example.com", dump)
        columns = [r[1] for r in self.store.connection.execute("PRAGMA table_info(model_usage)")]
        self.assertEqual(columns, ["id", "ts", "provider", "model", "role", "outcome", "prompt_tokens",
                                   "completion_tokens", "cache_hit_tokens", "latency_ms", "cost"])


class UsageSinkTests(unittest.TestCase):
    def test_sink_receives_deepseek_tokens(self):
        seen = []
        complete("deepseek", "https://api.deepseek.com", "m", [{"role": "user", "content": "x"}], {}, key="k",
                 transport=Upstream(deepseek_envelope("{}")), usage_sink=seen.append)
        self.assertEqual(seen, [{"prompt_tokens": 3024, "completion_tokens": 1300, "cache_hit_tokens": 1000}])

    def test_sink_receives_ollama_tokens(self):
        seen = []
        complete("ollama", "http://127.0.0.1:11434", "m", [{"role": "user", "content": "x"}], {},
                 transport=Upstream(OLLAMA_ENVELOPE), usage_sink=seen.append)
        self.assertEqual(seen, [{"prompt_tokens": 410, "completion_tokens": 77, "cache_hit_tokens": 0}])

    def test_sink_is_optional_called_even_for_unparsable_content_and_cannot_break_the_call(self):
        seen = []
        with self.assertRaises(ProviderError):
            complete("deepseek", "https://api.deepseek.com", "m", [], {}, key="k",
                     transport=Upstream(deepseek_envelope("nope")), usage_sink=seen.append)
        self.assertEqual(seen[0]["prompt_tokens"], 3024)
        def broken(_):
            raise RuntimeError("sink")
        self.assertEqual(complete("deepseek", "https://api.deepseek.com", "m", [], {}, key="k",
                                  transport=Upstream(deepseek_envelope("{}")), usage_sink=broken), {})
        self.assertEqual(complete("deepseek", "https://api.deepseek.com", "m", [], {}, key="k",
                                  transport=Upstream(deepseek_envelope("{}"))), {})

    def test_odd_usage_shapes_give_zeros(self):
        for envelope in ({}, {"usage": []}, {"usage": {"prompt_tokens": "9", "completion_tokens": -1}}, []):
            with self.subTest(envelope=envelope):
                self.assertEqual(usage_of("deepseek", envelope),
                                 {"prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0})

    def test_ollama_path_of_the_classifier_records_tokens_with_an_injected_opener(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        settings = config()
        recorder = UsageRecorder(store, settings, clock=lambda: 1_800_000_000.0, trailing=False)
        provider = build_provider(settings, opener=FakeHTTP(OLLAMA_ENVELOPE), prompt="P", case_prompt="C",
                                  topics_prompt="T", usage=recorder)
        self.assertEqual(provider.analyze_case([message(1, "lỗi")]).verdict, "ticket")
        row, = store.model_usage_since(0)
        self.assertEqual(row[1:], ("ollama", "qwen3.5:9b", "primary", "ok", 410, 77, 0, row[8]))
        self.assertEqual(row[8], 0.0)


class PricingTests(unittest.TestCase):
    UTC = timezone.utc
    USAGE = {"prompt_tokens": 3024, "completion_tokens": 1300, "cache_hit_tokens": 1000}

    def ts(self, day, hour, minute=0):
        return datetime(2026, 10, day, hour, minute, tzinfo=self.UTC).timestamp()

    def test_peak_price_on_a_weekday(self):
        expected = (1000 * 0.006 + 2024 * 0.3 + 1300 * 1.2) / 1e6
        for hour, minute in ((1, 0), (3, 59), (6, 0), (9, 30)):   # 14 Oct 2026 is a Wednesday
            with self.subTest(hour=hour):
                self.assertAlmostEqual(price(PricingConfig(), self.USAGE, self.ts(14, hour, minute)), expected, 12)

    def test_off_peak_uses_the_factor_outside_windows_and_at_weekends(self):
        expected = (1000 * 0.006 + 2024 * 0.3 + 1300 * 1.2) / 1e6 * 0.5
        for day, hour, minute in ((14, 0, 59), (14, 4, 0), (14, 5, 0), (14, 10, 0), (14, 23, 0), (17, 2, 0), (18, 7, 0)):
            with self.subTest(day=day, hour=hour):
                self.assertAlmostEqual(price(PricingConfig(), self.USAGE, self.ts(day, hour, minute)), expected, 12)

    def test_custom_pricing_and_cache_hit_is_capped_at_the_prompt(self):
        pricing = PricingConfig("EUR", 1.0, 2.0, 3.0, 0.25, ("00:00-23:59",))
        self.assertAlmostEqual(price(pricing, {"prompt_tokens": 10, "cache_hit_tokens": 99, "completion_tokens": 1},
                                     self.ts(14, 12)), (10 * 1.0 + 3.0) / 1e6, 12)
        self.assertAlmostEqual(price(pricing, {"prompt_tokens": 1_000_000, "completion_tokens": 0},
                                     self.ts(17, 12)), 2.0 * 0.25, 9)

    def test_ollama_is_free_even_with_a_price_table(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        recorder = UsageRecorder(store, config(), clock=lambda: self.ts(14, 2), trailing=False)
        recorder.record("ollama", "qwen3.5:9b", "primary", "ok", self.USAGE, 5)
        self.assertEqual(store.model_usage_since(0)[0][8], 0.0)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "triage.sqlite3"
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 10, 14, 12, 0).timestamp()   # local noon, as the file defines "today"
        self.settings = replace(config(), db_path=str(self.db), model=model_config())
        self.clock_value = self.now
        self.recorder = UsageRecorder(self.store, self.settings, clock=lambda: self.clock_value, trailing=False)
        self.path = Path(self.tmp.name) / "agentfleet-usage.json"

    def test_summary_schema_values_and_daily_lines(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 50, "cache_hit_tokens": 40}
        self.recorder.record("deepseek", "deepseek-flash", "primary", "rate_limited", {}, 10)
        self.recorder.record("ollama", "qwen3.5:9b", "fallback", "ok", {"prompt_tokens": 7, "completion_tokens": 3}, 10)
        self.clock_value -= 3 * 86400   # three days ago: inside 7d and 30d, outside today
        self.recorder.record("deepseek", "deepseek-flash", "primary", "ok", usage, 10)
        self.clock_value = self.now - 20 * 86400
        self.recorder.record("deepseek", "deepseek-flash", "primary", "ok", usage, 10)
        self.clock_value = self.now - 40 * 86400
        self.recorder.record("deepseek", "deepseek-flash", "primary", "ok", usage, 10)
        self.clock_value = self.now
        self.recorder.publish(force=True)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"schema", "generatedAt", "currency", "windows", "daily"})
        self.assertEqual((data["schema"], data["currency"], data["generatedAt"]), (1, "USD", self.now))
        self.assertEqual(set(data["windows"]), {"today", "7d", "30d"})
        window_keys = {"calls", "ok", "errors", "fallbacks", "promptTokens", "completionTokens",
                       "cacheHitTokens", "cost", "byModel"}
        model_keys = {"provider", "model", "calls", "errors", "fallbacks", "promptTokens",
                      "completionTokens", "cacheHitTokens", "cost"}
        for window in data["windows"].values():
            self.assertEqual(set(window), window_keys)
            for entry in window["byModel"]:
                self.assertEqual(set(entry), model_keys)
        today, week, month = data["windows"]["today"], data["windows"]["7d"], data["windows"]["30d"]
        self.assertEqual((today["calls"], today["ok"], today["errors"], today["fallbacks"]), (2, 1, 1, 1))
        self.assertEqual((today["promptTokens"], today["completionTokens"], today["cacheHitTokens"]), (7, 3, 0))
        self.assertEqual((week["calls"], week["errors"], week["fallbacks"], week["promptTokens"]), (3, 1, 1, 107))
        self.assertEqual((month["calls"], month["promptTokens"]), (4, 207))
        self.assertEqual(sorted((e["provider"], e["calls"]) for e in month["byModel"]),
                         [("deepseek", 3), ("ollama", 1)])
        self.assertEqual(round(sum(e["cost"] for e in month["byModel"]), 6), month["cost"])
        self.assertEqual(month["cost"], round(month["cost"], 6))
        daily = data["daily"]
        self.assertEqual(len(daily), 30)
        self.assertEqual([d["date"] for d in daily], sorted(d["date"] for d in daily))
        self.assertEqual(daily[-1]["date"], "2026-10-14")
        self.assertEqual(daily[0]["date"], "2026-09-15")
        self.assertEqual(daily[-1], {"date": "2026-10-14", "calls": 2, "errors": 1, "fallbacks": 1, "cost": today["cost"]})
        self.assertEqual({d["date"]: d["calls"] for d in daily if d["calls"]},
                         {"2026-10-14": 2, "2026-10-11": 1, "2026-09-24": 1})
        self.assertEqual(sum(1 for d in daily if d["calls"] == 0), 27)
        for line in daily:
            self.assertEqual(set(line), {"date", "calls", "errors", "fallbacks", "cost"})

    def test_empty_database_still_produces_a_full_schema(self):
        data = build_summary([], "USD", self.now)
        self.assertEqual(len(data["daily"]), 30)
        self.assertEqual(data["windows"]["today"]["byModel"], [])
        self.assertEqual(data["windows"]["30d"]["calls"], 0)

    def test_writes_are_throttled_to_one_per_interval(self):
        self.recorder.publish(force=True)
        first = json.loads(self.path.read_text())["generatedAt"]
        self.clock_value = self.now + 10
        self.recorder.record("ollama", "m", "primary", "ok", {}, 1)
        data = json.loads(self.path.read_text())
        self.assertEqual((data["generatedAt"], data["windows"]["today"]["calls"]), (first, 0))
        self.clock_value = self.now + 31
        self.recorder.record("ollama", "m", "primary", "ok", {}, 1)
        data = json.loads(self.path.read_text())
        self.assertEqual((data["generatedAt"], data["windows"]["today"]["calls"]), (self.now + 31, 2))

    def test_a_throttled_write_is_not_lost_the_trailing_timer_fires(self):
        recorder = UsageRecorder(self.store, self.settings, clock=lambda: self.clock_value, interval=0.2)
        recorder.publish(force=True)
        recorder.record("ollama", "m", "primary", "ok", {}, 1)
        self.assertEqual(json.loads(self.path.read_text())["windows"]["today"]["calls"], 0)
        import time
        time.sleep(0.1)
        self.clock_value += 1   # the fake clock passes the interval; the timer re-publishes
        for _ in range(40):
            if json.loads(self.path.read_text())["windows"]["today"]["calls"] == 1:
                break
            time.sleep(0.05)
        self.assertEqual(json.loads(self.path.read_text())["windows"]["today"]["calls"], 1)
        recorder.trailing = False   # stop the re-arming timer: the fake clock no longer moves

    def test_write_is_atomic_temp_then_replace_with_no_leftover(self):
        seen = []
        real = os.replace

        def replace_spy(source, target):
            seen.append((Path(source).name, json.loads(Path(source).read_text()) ["schema"], Path(target).exists()))
            return real(source, target)

        with patch.object(usage_module.os, "replace", replace_spy):
            self.recorder.publish(force=True)
        self.assertEqual(seen, [("agentfleet-usage.tmp", 1, False)])
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).glob("agentfleet-usage*")), ["agentfleet-usage.json"])
        before = self.path.read_bytes()
        with patch.object(usage_module.os, "replace", side_effect=OSError("disk")):
            self.recorder.publish(force=True)   # swallowed: accounting never breaks the listener
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).glob("agentfleet-usage*")), ["agentfleet-usage.json"])

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_posix_mode_is_0640(self):
        self.recorder.publish(force=True)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_memory_database_writes_no_file(self):
        recorder = UsageRecorder(Store(":memory:"), config(), clock=lambda: self.now, trailing=False)
        self.assertIsNone(recorder.path)
        recorder.publish(force=True)

    def test_startup_publish_is_wired_next_to_receipt_ready(self):
        text = (Path(__file__).resolve().parents[1] / "src" / "tg_triage" / "telegram_io.py").read_text(encoding="utf-8")
        self.assertLess(text.index("ready(config)"), text.index("usage.publish(force=True)"))


class RetentionAndConfigTests(unittest.TestCase):
    ENV = {"TG_API_ID": "1", "TG_API_HASH": "0" * 32}

    def test_rows_older_than_90_days_are_pruned_and_the_table_is_additive(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        now = 1_800_000_000.0
        store.record_model_usage(now - 91 * 86400, "deepseek", "m", "primary", "ok")
        store.record_model_usage(now - 89 * 86400, "deepseek", "m", "primary", "ok")
        store.record_model_usage(now, "deepseek", "m", "primary", "ok")
        self.assertEqual(len(store.model_usage_since(0)), 2)
        names = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"messages", "cases", "groups", "reconciliation", "attachments", "group_notices",
                         "model_usage"} <= names)

    def load(self, **replacements):
        text = EXAMPLE.read_text(encoding="utf-8")
        for old, new in replacements.items():
            assert old in text, old
            text = text.replace(old, new)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path, self.ENV)

    def test_example_defaults(self):
        model = load_config(EXAMPLE, self.ENV).model
        self.assertEqual((model.cloud_image_consent, model.fallback, model.fallback_model, model.fallback_think),
                         (False, "ollama", "qwen3.5:9b", True))
        self.assertEqual(model.pricing, PricingConfig("USD", 0.006, 0.3, 1.2, 0.5, ("01:00-04:00", "06:00-10:00")))

    def test_deepseek_with_image_consent_pricing_and_fallback_keys(self):
        model = self.load(**{'provider = "ollama"': 'provider = "deepseek"', "think = true": "think = false",
                             "cloud_consent = false": 'cloud_consent = true\ncloud_image_consent = true\n'
                                                      'fallback = "none"\nfallback_model = "qwen3:4b"\n'
                                                      'fallback_think = false\n[model.pricing]\ncurrency = "eur"\n'
                                                      'output = 2.5\noffpeak_factor = 0.25\n'
                                                      'peak_hours_utc = ["08:00-09:30"]'}).model
        self.assertEqual((model.cloud_image_consent, model.fallback, model.fallback_model, model.fallback_think),
                         (True, "none", "qwen3:4b", False))
        self.assertEqual(model.pricing, PricingConfig("EUR", 0.006, 0.3, 2.5, 0.25, ("08:00-09:30",)))

    def test_other_hosted_providers_have_no_built_in_price(self):
        model = self.load(**{'provider = "ollama"': 'provider = "openai"', "think = true": "think = false"}).model
        self.assertEqual((model.pricing.input_cache_hit, model.pricing.input_cache_miss, model.pricing.output),
                         (0.0, 0.0, 0.0))

    def test_invalid_values_are_refused_without_echo(self):
        for change in ('cloud_image_consent = "yes"', 'fallback = "ghp_TEST"', "fallback_think = 1",
                       'fallback_model = ""'):
            with self.subTest(change=change), self.assertRaises(ConfigError) as error:
                self.load(**{"cloud_consent = false": "cloud_consent = false\n" + change})
            self.assertNotIn("ghp_TEST", str(error.exception))
        for pricing in ("output = -1", "offpeak_factor = 2", 'currency = "dollars"', 'peak_hours_utc = ["10:00-09:00"]',
                        'peak_hours_utc = ["25:00-26:00"]', "peak_hours_utc = 5", 'output = "1.2"'):
            with self.subTest(pricing=pricing), self.assertRaises(ConfigError):
                self.load(**{"cloud_consent = false": "cloud_consent = false\n[model.pricing]\n" + pricing})


if __name__ == "__main__":
    unittest.main()
