"""Model-call accounting: one `model_usage` row per call and the console summary file.

Only counts, timing, cost and an outcome code are stored. Message text, images, prompts and keys never
reach this module. The summary (`agentfleet-usage.json`, schema 1) is a shared contract with the console:
camelCase keys, written atomically next to the database, at most once per `interval` seconds.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import parse_peak_window

LOG = logging.getLogger(__name__)

SUMMARY_NAME = "agentfleet-usage.json"
SUMMARY_SCHEMA = 1
SUMMARY_INTERVAL_SECONDS = 30.0
DAILY_DAYS = 30


def is_peak(pricing, ts):
    """Peak pricing applies Monday-Friday inside `peak_hours_utc` (UTC); everything else is off-peak."""
    moment = datetime.fromtimestamp(ts, timezone.utc)
    if moment.weekday() >= 5:
        return False
    minute = moment.hour * 60 + moment.minute
    for window in pricing.peak_hours_utc:
        parsed = parse_peak_window(window)
        if parsed is not None and parsed[0] <= minute < parsed[1]:
            return True
    return False


def price(pricing, usage, ts):
    """cost = (hit*hit_price + (prompt-hit)*miss_price + completion*output_price) / 1e6, scaled by
    `offpeak_factor` outside peak hours."""
    prompt = max(0, int(usage.get("prompt_tokens", 0)))
    hit = min(prompt, max(0, int(usage.get("cache_hit_tokens", 0))))
    completion = max(0, int(usage.get("completion_tokens", 0)))
    cost = (hit * pricing.input_cache_hit + (prompt - hit) * pricing.input_cache_miss
            + completion * pricing.output) / 1e6
    return cost if is_peak(pricing, ts) else cost * pricing.offpeak_factor


def _day_start(day):
    return datetime(day.year, day.month, day.day).timestamp()


def _window(rows, since):
    models = {}
    total = {"calls": 0, "ok": 0, "errors": 0, "fallbacks": 0, "promptTokens": 0,
             "completionTokens": 0, "cacheHitTokens": 0, "cost": 0.0}
    for ts, provider, model, role, outcome, prompt, completion, hit, cost in rows:
        if ts < since:
            continue
        entry = models.setdefault((provider, model), {
            "provider": provider, "model": model, "calls": 0, "errors": 0, "fallbacks": 0,
            "promptTokens": 0, "completionTokens": 0, "cacheHitTokens": 0, "cost": 0.0})
        failed = outcome != "ok"
        for target in (entry, total):
            target["calls"] += 1
            target["errors"] += failed
            target["fallbacks"] += role == "fallback"
            target["promptTokens"] += prompt
            target["completionTokens"] += completion
            target["cacheHitTokens"] += hit
            target["cost"] += cost
    total["ok"] = total["calls"] - total["errors"]
    for target in (total, *models.values()):
        target["cost"] = round(target["cost"], 6)
    total["byModel"] = sorted(models.values(), key=lambda e: (-e["calls"], e["provider"], e["model"]))
    return total


def build_summary(rows, currency, now):
    """The FENCE schema: today / 7d / 30d windows by local calendar day, plus 30 daily lines."""
    today = datetime.fromtimestamp(now).date()
    days = [today - timedelta(days=offset) for offset in range(DAILY_DAYS - 1, -1, -1)]
    daily = {day: {"date": day.isoformat(), "calls": 0, "errors": 0, "fallbacks": 0, "cost": 0.0}
             for day in days}
    for ts, _provider, _model, role, outcome, _p, _c, _h, cost in rows:
        line = daily.get(datetime.fromtimestamp(ts).date())
        if line is not None:
            line["calls"] += 1
            line["errors"] += outcome != "ok"
            line["fallbacks"] += role == "fallback"
            line["cost"] += cost
    for line in daily.values():
        line["cost"] = round(line["cost"], 6)
    return {
        "schema": SUMMARY_SCHEMA, "generatedAt": float(now), "currency": currency,
        "windows": {"today": _window(rows, _day_start(today)),
                    "7d": _window(rows, _day_start(today - timedelta(days=6))),
                    "30d": _window(rows, _day_start(days[0]))},
        "daily": [daily[day] for day in days],
    }


def write_atomic(path, payload):
    """Temp file + fsync + os.replace, like receipt.py; mode 0640 and group tglistener off Windows."""
    path = Path(path)
    temp = path.with_suffix(".tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            if os.name != "nt":
                import grp
                try:
                    os.chown(temp, -1, grp.getgrnam("tglistener").gr_gid)
                except (KeyError, OSError):
                    pass
            os.chmod(temp, 0o640 if os.name != "nt" else 0o600)
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


class UsageRecorder:
    """Writes `model_usage` rows and keeps the console summary fresh (throttled)."""

    def __init__(self, store, config, *, clock=time.time, interval=SUMMARY_INTERVAL_SECONDS,
                 trailing=True):
        self.store = store
        self.pricing = config.model.pricing
        self.clock = clock
        self.interval = interval
        self.trailing = trailing
        db_path = str(config.db_path)
        self.path = None if db_path == ":memory:" else Path(db_path).parent / SUMMARY_NAME
        self._lock = threading.Lock()
        self._last_write = None
        self._timer = None

    def record(self, provider, model, role, outcome, usage=None, latency_ms=0):
        """Never raises: accounting must not break classification."""
        try:
            usage = usage or {}
            ts = self.clock()
            cost = 0.0 if provider == "ollama" else price(self.pricing, usage, ts)
            self.store.record_model_usage(
                ts, provider, model, role, outcome, usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0), usage.get("cache_hit_tokens", 0),
                max(0, int(latency_ms)), round(cost, 6))
        except Exception as error:
            LOG.warning("Usage record failed exception=%s", type(error).__name__)
            return
        self.publish()

    def publish(self, *, force=False):
        """Write the summary file now (startup) or at most once per interval; never raises."""
        if self.path is None:
            return
        with self._lock:
            now = self.clock()
            if (not force and self._last_write is not None
                    and now - self._last_write < self.interval):
                self._schedule(self.interval - (now - self._last_write))
                return
            try:
                rows = self.store.model_usage_since(_day_start(
                    datetime.fromtimestamp(now).date() - timedelta(days=DAILY_DAYS - 1)))
                write_atomic(self.path, build_summary(rows, self.pricing.currency, now))
            except Exception as error:
                LOG.warning("Usage summary write failed exception=%s", type(error).__name__)
            self._last_write = now

    def _schedule(self, delay):
        """A throttled write is not lost: one trailing write fires when the interval has passed."""
        if not self.trailing or self._timer is not None:
            return
        self._timer = threading.Timer(delay + 0.05, self._trailing_write)
        self._timer.daemon = True
        self._timer.start()

    def _trailing_write(self):
        with self._lock:
            self._timer = None
        self.publish()
