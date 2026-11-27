"""Hosted model with images, local Ollama backup, usage accounting and pricing.

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

from agentfleet_common import ai_transport
from agentfleet_common.ai_transport import ProviderError, complete, usage_of
from tg_triage.config import ConfigError, ModelConfig, PricingConfig
from tg_triage.provider import build_provider
from tg_triage.store import Store
from tg_triage.usage import UsageRecorder, price
from tests.helpers import FAKE_MODEL_KEY, FAKE_TG_SECRETS, load_with_test_secrets, put_test_secret
from tests.helpers import EXAMPLE, JPG, PNG, WEBP, FakeHTTP, config, message

REAL_COMPLETE = ai_transport.complete
MODEL_SCOPE = "deepseek|https://api.deepseek.com"
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
        self.addCleanup(patch.stopall)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = put_test_secret(Path(self.tmp.name), "model-key.json",
                                   json.dumps({"scope": MODEL_SCOPE, "key": "test-key"}))

    def provider(self, upstream, http=None, **model):
        self.upstream = upstream
        self.http = http or FakeHTTP()
        patch("agentfleet_common.ai_transport.complete",
              lambda *a, **k: REAL_COMPLETE(*a, transport=upstream, **k)).start()
        model.setdefault("key_file", str(self.key))
        settings = replace(config(), model=model_config(**model))
        self.recorder = UsageRecorder(self.store, settings, clock=lambda: self.now)
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


class RedactTests(ProviderBase):

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
        provider = self.provider(Upstream(), FakeHTTP(OLLAMA_ENVELOPE), key_file="")
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

    def test_ollama_path_of_the_classifier_records_tokens_with_an_injected_opener(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        settings = config()
        recorder = UsageRecorder(store, settings, clock=lambda: 1_800_000_000.0)
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
        recorder = UsageRecorder(store, config(), clock=lambda: self.ts(14, 2))
        recorder.record("ollama", "qwen3.5:9b", "primary", "ok", self.USAGE, 5)
        self.assertEqual(store.model_usage_since(0)[0][8], 0.0)


class RetentionAndConfigTests(unittest.TestCase):
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
            return load_with_test_secrets(path, credentials={
                **FAKE_TG_SECRETS, "model-key.json": json.dumps({"scope": MODEL_SCOPE, "key": FAKE_MODEL_KEY}),
            })

    def test_example_defaults(self):
        model = load_with_test_secrets(EXAMPLE).model
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
