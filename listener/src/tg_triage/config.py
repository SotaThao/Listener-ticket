"""Load TOML settings and credentials without exposing input in errors."""

import math
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import rules


class ConfigError(ValueError):
    """A configuration error with a safe, field-specific message."""


@dataclass(frozen=True)
class ProxyConfig:
    host: str = ""
    port: int = 443
    secret: str = field(default="", repr=False)


@dataclass(frozen=True)
class TelegramConfig:
    session_path: str
    chat_ids: tuple[int, ...]
    min_text_length: int
    proxy: ProxyConfig
    listen_all_groups: bool = False
    ignored_command_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OllamaConfig:
    url: str
    model: str
    timeout_seconds: float
    confidence_threshold: float


SUPPORTED_PROVIDERS = ("ollama", "anthropic", "openai", "deepseek", "compatible")
# Assistant definition 12.12: reasoning is slow on CPU, so the default wait is generous.
DEFAULT_MODEL_TIMEOUT_SECONDS = 600.0


DEFAULT_PEAK_HOURS_UTC = ("01:00-04:00", "06:00-10:00")
_PEAK_WINDOW = re.compile(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)")


def parse_peak_window(value):
    """"HH:MM-HH:MM" (UTC, start < end) -> (start_minute, end_minute); None when malformed."""
    match = _PEAK_WINDOW.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    start = int(match[1]) * 60 + int(match[2])
    end = int(match[3]) * 60 + int(match[4])
    return (start, end) if start < end else None


@dataclass(frozen=True)
class PricingConfig:
    """Cost of a hosted model call, per 1M tokens at peak hours (defaults: deepseek-flash, USD,
    api-docs.deepseek.com/quick_start/pricing, measured 2026-10-15). Outside `peak_hours_utc`
    (and all weekend) the price is multiplied by `offpeak_factor`. Local Ollama always costs 0."""

    currency: str = "USD"
    input_cache_hit: float = 0.006
    input_cache_miss: float = 0.3
    output: float = 1.2
    offpeak_factor: float = 0.5
    peak_hours_utc: tuple[str, ...] = DEFAULT_PEAK_HOURS_UTC


@dataclass(frozen=True)
class ModelConfig:
    """Which model judges chat / bug / task / feedback (12.12), behind a provider layer.

    `name` and `timeout_seconds` are the values the provider uses; `[ollama]` keeps the server
    address and the confidence threshold. Only provider "ollama" exists today.
    """

    provider: str = "ollama"
    name: str = "qwen3.5:9b"
    think: bool = True
    timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    base_url: str = "http://127.0.0.1:11434"
    max_output: int = 2048
    cloud_consent: bool = False
    key_file: str = ""
    # A hosted provider may receive images only when both consents are true (OpenAI-compatible only).
    cloud_image_consent: bool = False
    # Backup when the hosted provider fails or may not receive the request: "ollama" | "none".
    fallback: str = "ollama"
    fallback_model: str = "qwen3.5:9b"
    fallback_think: bool = True
    pricing: PricingConfig = field(default_factory=PricingConfig)


@dataclass(frozen=True)
class AggregationConfig:
    """Case grouping (Assistant definition 12.1). window_minutes = 0 disables waiting."""

    window_minutes: int = 2
    max_messages_per_case: int = 20
    # 12.9: a closed window longer than this is split into consecutive chunks by time and each
    # chunk is sent to the model and verified on its own.
    max_messages_per_window: int = 40


@dataclass(frozen=True)
class AttachmentsConfig:
    """Attachment limits (Assistant definition 12.8 images, 12.14 documents).

    max_per_case = 0 disables images; max_documents_per_case = 0 disables documents. The two
    per-case counts are separate. max_bytes applies to each file; max_per_hour counts every
    `gh release upload` (images and documents together)."""

    max_bytes: int = 10 * 1024 * 1024
    max_per_case: int = 5
    max_per_hour: int = 60
    max_documents_per_case: int = 5


@dataclass(frozen=True)
class Config:
    mode: str
    telegram: TelegramConfig
    ollama: OllamaConfig
    max_issues_per_hour: int
    db_path: str
    api_id: int
    api_hash: str = field(repr=False)
    gh_token: str = field(default="", repr=False)
    gh_repo: str = ""
    aggregation: AggregationConfig = field(default_factory=AggregationConfig)
    attachments: AttachmentsConfig = field(default_factory=AttachmentsConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    console_revision: str = ""
    thinking_enabled: bool = True
    topic_memory_enabled: bool = False
    autonomous_enabled: bool = False
    context_quiet_seconds: int = 30
    context_max_wait_seconds: int = 60

    def redacted(self) -> dict:
        result = asdict(self)
        for key in ("api_hash", "gh_token"):
            result[key] = {"length": len(getattr(self, key))}
        result["telegram"]["proxy"]["secret"] = {
            "length": len(self.telegram.proxy.secret)
        }
        return result


def _table(data, name):
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"{name}: required TOML table")
    return value


def _string(data, key, name, *, empty=False, default=None):
    value = data.get(key, default)
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ConfigError(f"{name}: expected {'a string' if empty else 'a non-empty string'}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ConfigError(f"{name}: control characters are not allowed")
    return value


def _integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{name}: expected integer >= {minimum}")
    return value


def _optional_table(data, name):
    """An absent section means defaults; a present non-table value is an error."""
    if name not in data:
        return {}
    value = data[name]
    if not isinstance(value, dict):
        raise ConfigError(f"{name}: expected TOML table")
    return value


def _number(value, name, minimum, maximum=None):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < minimum or (maximum is not None and value > maximum)):
        raise ConfigError(f"{name}: expected finite number in the allowed range")
    return float(value)


def check_provider(provider):
    """Fixed messages only: the configured value is never echoed back."""
    if provider == "claude":
        raise ConfigError("model.provider: 'claude' chưa hỗ trợ, hiện chỉ có 'ollama'")
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError("model.provider: unsupported provider")
    return provider


def _pricing(model_table, provider):
    """`[model.pricing]`. Built-in prices are deepseek's; openai / anthropic / compatible have none, so
    their cost stays 0 until the table sets prices (ollama is always free, whatever the table says)."""
    table = _optional_table(model_table, "pricing")
    base = PricingConfig() if provider in ("deepseek", "ollama") else PricingConfig(input_cache_hit=0.0, input_cache_miss=0.0, output=0.0)
    currency = _string(table, "currency", "model.pricing.currency", default=base.currency)
    if not re.fullmatch(r"[A-Za-z]{3}", currency):
        raise ConfigError("model.pricing.currency: expected a 3-letter code")
    hours = table.get("peak_hours_utc", list(base.peak_hours_utc))
    if (not isinstance(hours, list) or len(hours) > 8
            or any(parse_peak_window(item) is None for item in hours)):
        raise ConfigError("model.pricing.peak_hours_utc: expected HH:MM-HH:MM windows (UTC, start before end)")
    return PricingConfig(
        currency.upper(),
        *(_number(table.get(key, getattr(base, key)), f"model.pricing.{key}", 0, 100000)
          for key in ("input_cache_hit", "input_cache_miss", "output")),
        _number(table.get("offpeak_factor", base.offpeak_factor), "model.pricing.offpeak_factor", 0, 1),
        tuple(hours))


def _read_toml(path):
    try:
        with Path(path).open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, ValueError):
        raise ConfigError("config: cannot read valid TOML") from None
    return data


def load_database_path(path):
    """CSV export requires only storage settings, never account credentials."""
    return _string(_table(_read_toml(path), "storage"), "db_path", "storage.db_path")


def load_config(path, environ=None, *, validate_only=False) -> Config:
    env = dict(os.environ if environ is None else environ)
    if validate_only:
        # Validate persisted schema independently of private systemd EnvironmentFile credentials.
        # Genuine credentials and requested mode are checked again by authenticated startup.
        env = {'TG_API_ID':'1','TG_API_HASH':'0'*32,'GH_TOKEN':'schema-validation','GH_REPO':'schema/validation','TG_PROXY_SECRET':'schema-validation'}
    data = _read_toml(path)
    mode = env.get("TG_TRIAGE_MODE") or data.get("mode")
    if mode not in ("dry-run", "live"):
        raise ConfigError("mode: expected dry-run or live")
    try:
        api_id = int(env.get("TG_API_ID", ""))
    except (ValueError, TypeError):
        raise ConfigError("TG_API_ID: required positive integer") from None
    if api_id <= 0:
        raise ConfigError("TG_API_ID: required positive integer")
    api_hash_value = env.get("TG_API_HASH", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash_value):
        raise ConfigError("TG_API_HASH: required 32 hexadecimal characters")
    gh_token = env.get("GH_TOKEN", "")
    gh_repo = env.get("GH_REPO", "")
    if mode == "live" and not gh_token.strip():
        raise ConfigError("GH_TOKEN: required in live mode")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in gh_token):
        raise ConfigError("GH_TOKEN: control characters are not allowed")
    if (mode == "live" or gh_repo) and not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", gh_repo):
        raise ConfigError("GH_REPO: expected owner/repo")
    tg = _table(data, "telegram")
    ignored_prefixes = tg.get("ignored_command_prefixes", [])
    # COMMAND's first capturing group is the canonical alternation of owned names.
    command_names = rules.COMMAND.pattern.split("(", 1)[1].split(")", 1)[0].split("|")
    if (not isinstance(ignored_prefixes, list) or any(
            not isinstance(prefix, str) or re.fullmatch(r"/[A-Za-z0-9_]+", prefix) is None
            for prefix in ignored_prefixes)):
        raise ConfigError("telegram.ignored_command_prefixes: expected command names")
    names = [prefix[1:].casefold() for prefix in ignored_prefixes]
    if (len(set(names)) != len(names) or any(
            command.casefold().startswith(name) for name in names for command in command_names)):
        raise ConfigError("telegram.ignored_command_prefixes: duplicate or overlapping listener command")
    listen_all = tg.get("listen_all_groups", False)
    if type(listen_all) is not bool:
        raise ConfigError("telegram.listen_all_groups: expected true or false")
    chats = tg.get("chat_ids", [] if listen_all else None)
    if listen_all:
        if chats != []:
            raise ConfigError("telegram.chat_ids: must be empty or absent when listen_all_groups is true")
    elif (not isinstance(chats, list) or not chats
            or any(type(chat) is not int or chat >= 0 for chat in chats)):
        raise ConfigError("telegram.chat_ids: required non-empty list of group IDs")
    proxy = tg.get("proxy", {})
    if not isinstance(proxy, dict):
        raise ConfigError("telegram.proxy: expected TOML table")
    host = _string(proxy, "host", "telegram.proxy.host", empty=True, default="")
    port = _integer(proxy.get("port", 443), "telegram.proxy.port")
    if port > 65535:
        raise ConfigError("telegram.proxy.port: expected port <= 65535")
    if "secret" in proxy:
        raise ConfigError("telegram.proxy.secret: forbidden in TOML; use TG_PROXY_SECRET")
    secret = _string(env, "TG_PROXY_SECRET", "TG_PROXY_SECRET", empty=not bool(host), default="")
    ollama = _table(data, "ollama")
    url = _string(ollama, "url", "ollama.url")
    try:
        parsed = urlsplit(url)
        valid_url = (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment)
        parsed.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ConfigError("ollama.url: expected HTTP(S) URL without credentials")
    model_table = _optional_table(data, "model")
    provider = check_provider(_string(model_table, "provider", "model.provider", default="ollama"))
    think = model_table.get("think", True)
    if type(think) is not bool:
        raise ConfigError("model.think: expected true or false")
    # [model] wins over the older [ollama] model / timeout_seconds keys, which stay accepted.
    model_name = (_string(model_table, "model", "model.model") if "model" in model_table
                  else _string(ollama, "model", "ollama.model"))
    if "timeout_seconds" in model_table:
        timeout = _number(model_table["timeout_seconds"], "model.timeout_seconds", 0.001)
    elif "timeout_seconds" in ollama:
        timeout = _number(ollama["timeout_seconds"], "ollama.timeout_seconds", 0.001)
    else:
        timeout = DEFAULT_MODEL_TIMEOUT_SECONDS
    from .ai_transport import validate_endpoint, ProviderError, ENDPOINTS
    base_url = model_table.get("base_url", ENDPOINTS.get(provider, url))
    try:
        validate_endpoint(provider, base_url)
    except ProviderError:
        raise ConfigError("model.base_url: invalid endpoint") from None
    max_output = _integer(model_table.get("num_predict", 2048), "model.num_predict", 256)
    consent = model_table.get("cloud_consent", False)
    key_file = model_table.get("key_file", "")
    if max_output > 8192 or type(consent) is not bool or not isinstance(key_file, str) or (key_file and not Path(key_file).is_absolute()) or (provider != "ollama" and think):
        raise ConfigError("model: invalid capability or limits")
    image_consent = model_table.get("cloud_image_consent", False)
    fallback = model_table.get("fallback", "ollama")
    fallback_think = model_table.get("fallback_think", True)
    if (type(image_consent) is not bool or type(fallback_think) is not bool
            or fallback not in ("ollama", "none")):
        raise ConfigError("model: invalid fallback or image consent")
    fallback_model = _string(model_table, "fallback_model", "model.fallback_model", default="qwen3.5:9b")
    pricing = _pricing(model_table, provider)
    aggregation_table = _optional_table(data, "aggregation")
    attachments_table = _optional_table(data, "attachments")
    default_aggregation, default_attachments = AggregationConfig(), AttachmentsConfig()
    aggregation = AggregationConfig(
        _integer(aggregation_table.get("window_minutes", default_aggregation.window_minutes),
                 "aggregation.window_minutes", 0),
        _integer(aggregation_table.get("max_messages_per_case", default_aggregation.max_messages_per_case),
                 "aggregation.max_messages_per_case"),
        _integer(aggregation_table.get("max_messages_per_window", default_aggregation.max_messages_per_window),
                 "aggregation.max_messages_per_window"))
    attachments = AttachmentsConfig(
        _integer(attachments_table.get("max_bytes", default_attachments.max_bytes),
                 "attachments.max_bytes"),
        _integer(attachments_table.get("max_per_case", default_attachments.max_per_case),
                 "attachments.max_per_case", 0),
        _integer(attachments_table.get("max_per_hour", default_attachments.max_per_hour),
                 "attachments.max_per_hour", 0),
        _integer(attachments_table.get("max_documents_per_case", default_attachments.max_documents_per_case),
                 "attachments.max_documents_per_case", 0))
    topic_memory_enabled = _optional_table(data, 'thinking').get('topic_memory_enabled', False)
    if type(topic_memory_enabled) is not bool:
        raise ConfigError('thinking.topic_memory_enabled: expected boolean')
    thinking_enabled = _optional_table(data, 'thinking').get('enabled', True)
    if type(thinking_enabled) is not bool:
        raise ConfigError('thinking.enabled: expected boolean')
    autonomous_enabled = _optional_table(data, 'thinking').get('autonomous_enabled', False)
    if type(autonomous_enabled) is not bool:
        raise ConfigError('thinking.autonomous_enabled: expected boolean')
    if autonomous_enabled and not thinking_enabled:
        raise ConfigError('thinking.autonomous_enabled requires thinking.enabled')
    thinking_table=_optional_table(data,'thinking')
    context_quiet_seconds=_integer(thinking_table.get('context_quiet_seconds',30),'thinking.context_quiet_seconds',30)
    context_max_wait_seconds=_integer(thinking_table.get('context_max_wait_seconds',60),'thinking.context_max_wait_seconds',30)
    if not context_quiet_seconds<=context_max_wait_seconds<=60:
        raise ConfigError('thinking: expected 30 <= context_quiet_seconds <= context_max_wait_seconds <= 60')
    return Config(
        mode=mode,
        telegram=TelegramConfig(
            _string(tg, "session_path", "telegram.session_path"), tuple(chats),
            _integer(tg.get("min_text_length"), "telegram.min_text_length", 0),
            ProxyConfig(host, port, secret), listen_all, tuple(ignored_prefixes)),
        ollama=OllamaConfig(
            (base_url if provider == "ollama" else url).rstrip("/"), model_name, timeout,
            _number(ollama.get("confidence_threshold"), "ollama.confidence_threshold", 0, 1)),
        max_issues_per_hour=_integer(
            _table(data, "github").get("max_issues_per_hour"), "github.max_issues_per_hour"),
        db_path=_string(_table(data, "storage"), "db_path", "storage.db_path"),
        api_id=api_id, api_hash=api_hash_value, gh_token=gh_token, gh_repo=gh_repo,
        aggregation=aggregation, attachments=attachments,
        model=ModelConfig(provider, model_name, think, timeout, base_url, max_output, consent, key_file,
                         image_consent, fallback, fallback_model, fallback_think, pricing),
        console_revision=str(data.get("console", {}).get("revision", "")),
        thinking_enabled=thinking_enabled,
        topic_memory_enabled=topic_memory_enabled,
        autonomous_enabled=autonomous_enabled,
        context_quiet_seconds=context_quiet_seconds,
        context_max_wait_seconds=context_max_wait_seconds,
    )
