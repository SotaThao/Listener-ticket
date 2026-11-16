"""Model-call accounting: one `model_usage` row per call.

Only counts, timing, cost and an outcome code are stored. Message text, images, prompts and keys never
reach this module.
"""

import logging
import time
from datetime import datetime, timezone

from .config import parse_peak_window

LOG = logging.getLogger(__name__)


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


class UsageRecorder:
    """Writes one `model_usage` row per model call."""

    def __init__(self, store, config, *, clock=time.time):
        self.store = store
        self.pricing = config.model.pricing
        self.clock = clock

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
