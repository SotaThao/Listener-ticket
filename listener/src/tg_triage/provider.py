"""Model provider layer (Assistant definition 12.12).

The pipeline talks to a provider, never to a vendor API, so the model behind it can change (a
local Ollama today, a hosted API later) without touching the pipeline. A provider offers:

* `classify_topics(transcript)` -> tuple of TopicGroup, or None when the answer is unusable;
* `analyze_case(messages, forced_type=None)` -> CaseAnalysis, or None.

Both return untrusted records validated by existing parsers. Ollama supports local image input;
cloud providers require explicit consent, redact contact text and receive images only with a second
(image) consent; otherwise, or when the hosted call fails, the same request runs on local Ollama.
"""

import base64
import json
import time
from dataclasses import replace

from .classifier import Classifier
from .config import check_provider


class ModelProvider:
    """The minimal interface every provider implements."""

    name = ""

    def classify_topics(self, transcript, *, images=None):
        raise NotImplementedError

    def analyze_case(self, messages, forced_type=None, *, images=None):
        raise NotImplementedError


class OllamaProvider(Classifier, ModelProvider):
    """The first provider: the existing Ollama classifier, with reasoning (`think`) configurable."""

    name = "ollama"

    def classify_topics(self, transcript, *, images=None):
        """Split a closed window into topic groups. `transcript` is the numbered transcript, or the
        window's messages (the transcript is then built here)."""
        return self.split_topics(transcript, images=images)


def build_provider(config, **options):
    """The provider named by `config.model.provider`; ConfigError when it is not supported."""
    check_provider(config.model.provider)
    if config.model.provider == "ollama":
        provider = OllamaProvider(config.ollama, think=config.model.think, **options)
        provider.max_output = config.model.max_output
        return provider
    return CloudProvider(config, **options)


OPENAI_COMPATIBLE = ("deepseek", "openai", "compatible")


def _image_mime(encoded):
    """jpeg / png / webp from the first bytes of a base64 image; None for anything else."""
    try:
        head = base64.b64decode(encoded[:24], validate=True)
    except (ValueError, TypeError):
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class CloudProvider(OllamaProvider):
    """Hosted model with a local Ollama backup.

    Reuses the existing untrusted parsers. Hosted data needs `cloud_consent`; images additionally need
    `cloud_image_consent` (OpenAI-compatible providers only). Whenever the hosted call fails, returns
    nothing usable, or may not receive the request, the very same request is run on local Ollama
    (`[model] fallback = "ollama"`), so the pipeline never needs to know a fallback happened.
    """

    def __init__(self, config, **options):
        super().__init__(config.ollama, think=False, **options)
        self.model_config = c = config.model
        self.name = c.provider
        self.cloud_images = bool(c.cloud_consent and c.cloud_image_consent
                                 and c.provider in OPENAI_COMPATIBLE)
        # Images are judged either by the hosted model (both consents) or by local vision (fallback).
        self.supports_vision = c.fallback == "ollama" or self.cloud_images
        self.rejects_images = not self.supports_vision
        self._fallback = None
        if c.fallback == "ollama":
            self._fallback = Classifier(replace(config.ollama, model=c.fallback_model), opener=self.opener,
                                        prompt=self.prompt, think=c.fallback_think, usage=self.usage)
            self._fallback.usage_role = "fallback"
            self._fallback.max_output = c.max_output

    _ingress_cloud_view=True

    def _ingress_source_text(self, text):
        from .ai_transport import redact
        return redact(text)

    def _cloud_messages(self, messages):
        """OpenAI-style messages: contact text redacted, images as inline data: parts; None when an
        image cannot be sent as a known type."""
        from .ai_transport import redact
        from .thinking import ContextJSON, DocumentContextJSON
        def content_for_cloud(message):
            content=message['content']
            if message.get('role')=='user' and isinstance(content,ContextJSON):
                try:
                    packet=json.loads(content)
                except (ValueError,TypeError):
                    packet=None
                if isinstance(packet,dict) and (
                        ('source_ids' in packet and 'sources' in packet)
                        or ('NEW_SOURCE_IDS' in packet and 'source_columns' in packet and 'sources' in packet)
                        or ('REPLY' in packet and 'ORIGINAL_METADATA' in packet)
                        or ('confirmed_requests' in packet and 'unresolved' in packet)):
                    def clean(value):
                        if isinstance(value,str):return redact(value)
                        if isinstance(value,list):return [clean(v) for v in value]
                        if isinstance(value,dict):return {k:clean(v) for k,v in value.items()}
                        return value
                    serialized=json.dumps(clean(packet),ensure_ascii=False,separators=(',',':'))
                    return DocumentContextJSON(serialized) if isinstance(content,DocumentContextJSON) else serialized
            return redact(content)
        result = []
        for message in messages:
            content=content_for_cloud(message)
            images = message.get("images")
            if not images:
                result.append(dict(message, content=content))
                continue
            parts = [{"type": "text", "text": content}]
            for encoded in images:
                mime = _image_mime(encoded)
                if mime is None:
                    return None
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{mime};base64,{encoded}"}})
            result.append({"role": message["role"], "content": parts})
        return result

    def _record(self, outcome, usage, started):
        if self.usage is not None:
            c = self.model_config
            self.usage.record(c.provider, c.name, "primary", outcome, usage,
                              (time.monotonic() - started) * 1000)

    def _ask(self, body, parse, attempts=2):
        c = self.model_config
        has_images = any("images" in m for m in body["messages"])
        if c.cloud_consent and (not has_images or self.cloud_images):
            result = self._ask_cloud(body, parse, attempts)
            if result is not None:
                return result
        if self._fallback is None:
            return None
        from .thinking import CURRENT
        turn = CURRENT.get()
        if turn and turn.calls >= turn.max_calls:
            turn.reason = 'model_call_budget_exhausted'
            return None
        local = dict(body, messages=body.get("_local_messages") or body["messages"], model=c.fallback_model, think=c.fallback_think,
                     options=dict(body.get("options", {})))
        return self._fallback._ask(local, parse, attempts)

    def _ask_cloud(self, body, parse, attempts):
        from .ai_transport import complete, read_key, ProviderError
        from .model_lock import MODEL_LOCK, LockUnavailable
        from .model_lock import LockStopped
        from .thinking import CURRENT, BudgetExhausted
        turn = CURRENT.get()
        if turn:
            from .thinking_transport import complete
        c = self.model_config
        messages = self._cloud_messages(body["messages"])
        if messages is None:
            return None
        from .document_reader import request_fits,has_document_context
        if has_document_context(messages) and not request_fits(dict(body,messages=messages)):
            if turn:turn.reason='context_budget_exhausted'
            return None
        started = time.monotonic()
        try:
            key = read_key(c.key_file, c.provider + "|" + c.base_url, group_name="tglistener")
        except ProviderError as error:
            self._record(error.code, {}, started)
            return None
        # Format failure gets one same-host validation retry before changing provider.
        # The shared Turn remains the authority for actual calls and the deadline.
        attempt_limit=max(attempts,2) if turn else attempts
        for attempt_index in range(attempt_limit):
            try:
                timeout = turn.attempt(body,consume=False) if turn else c.timeout_seconds
            except BudgetExhausted:
                return None
            usage, outcome, result = {}, "ok", None
            try:
                with MODEL_LOCK.enter(**({'stop': turn} if turn else {})):
                    if turn:
                        timeout = turn.attempt(body)
                        if timeout <= 0:
                            turn.reason = 'decision_deadline_exhausted'
                            return None
                    started = time.monotonic()   # latency excludes the wait for the model lock
                    value = complete(c.provider, c.base_url, c.name, messages, body["format"], key=key,
                                     timeout=timeout, max_output=min(c.max_output,body.get('_narrow_output',8192) if turn else 8192),
                                     usage_sink=usage.update)
                result = parse(json.dumps(value))
                if result is None:
                    outcome = "invalid_output"
            except ProviderError as error:
                outcome = error.code
            except LockStopped:
                if turn:
                    turn.reason = 'decision_deadline_exhausted'
                outcome = None
                return None
            except BudgetExhausted:
                outcome = None
                return None
            except LockUnavailable:
                outcome = None   # no model call was made
                return None
            finally:
                if outcome is not None:
                    self._record(outcome, usage, started)
            if result is not None:
                return result
            if outcome not in (("invalid_output", "invalid_json") if turn else ("invalid_output",)):
                return None
            repair=getattr(parse,'repair',None)
            if outcome=='invalid_output' and callable(repair) and attempt_index+1<attempt_limit:
                repaired=repair(body)
                if repaired is None:
                    return None
                body=repaired
                messages=self._cloud_messages(body['messages'])
                if messages is None or has_document_context(messages) and not request_fits(dict(body,messages=messages)):
                    return None
        return None
