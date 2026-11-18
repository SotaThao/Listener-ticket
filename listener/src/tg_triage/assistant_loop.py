"""In-memory assistant scheduling policy; callers provide clocks and perform I/O.

Use monotonic seconds for scheduling and Unix seconds for DailyCap. These helpers
are deliberately single-owner: the caller serializes turns and persists state.
"""
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType


LINKS = ("gemini", "gpt", "deepseek")
ACTIONS: Mapping[str, str] = MappingProxyType({
    "transient": "next_link",
    "rate_limited": "next_link",
    "auth": "next_link+open_breaker_auth",
    "schema": "repair_once_then_next",
    "truncated": "repair_once_then_next",
    "too_large": "next_link",
    "duplicate_tool": "force_proposal",
    "limit_reached": "force_proposal",
})


class CircuitBreaker:
    """One link's failure window and exclusive half-open probe."""

    def __init__(self, name, *, clock, threshold=3, window_s=300,
                 open_s=900, auth_open_s=3600):
        if threshold < 1 or min(window_s, open_s, auth_open_s) <= 0:
            raise ValueError("breaker limits must be positive")
        self.name = name
        self._clock = clock
        self._threshold = threshold
        self._window_s = window_s
        self._open_s = open_s
        self._auth_open_s = auth_open_s
        self._failures = deque()
        self._state = "closed"
        self._until = 0.0
        self._cooldown = open_s
        self._probe = False

    @property
    def state(self):
        if self._state == "open" and self._clock() >= self._until:
            self._state = "half_open"
            self._probe = False
        return self._state

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "open" or self._probe:
            return False
        self._probe = True
        return True

    def record_success(self):
        self._state = "closed"
        self._probe = False
        self._failures.clear()
        self._cooldown = self._open_s

    def cancel_probe(self):
        """Return an unused half-open trial when host admission denied I/O."""
        if self.state == 'half_open':
            self._probe = False

    def _open(self, seconds):
        self._state = "open"
        self._cooldown = seconds
        self._until = self._clock() + seconds
        self._probe = False
        self._failures.clear()

    def record_failure(self, kind):
        state = self.state
        if kind == "auth":
            self._open(self._auth_open_s)
        elif state == "half_open":
            self._open(min(self._cooldown * 2, 14400))
        elif state == "closed" and kind in ("transient", "rate_limited"):
            now = self._clock()
            while self._failures and self._failures[0] <= now - self._window_s:
                self._failures.popleft()
            self._failures.append(now)
            if len(self._failures) >= self._threshold:
                self._open(self._open_s)


class RateLimiter:
    """Admission in a rolling, half-open 60-second window."""

    def __init__(self, max_per_minute=20, *, clock):
        if max_per_minute < 1:
            raise ValueError("max_per_minute must be positive")
        self._limit = max_per_minute
        self._clock = clock
        self._calls = deque()

    def try_acquire(self) -> bool:
        now = self._clock()
        while self._calls and self._calls[0] <= now - 60:
            self._calls.popleft()
        if len(self._calls) >= self._limit:
            return False
        self._calls.append(now)
        return True


@dataclass
class _Attempt:
    status: str = "NEW"
    failures: int = 0
    retry_at: float | None = None
    first_infra: float | None = None


class AttemptTracker:
    """Per-fingerprint admission and bounded retry lifecycle.

    Infra retries wait 20 seconds, capped by the first infra event's deadline.
    Only ``failed`` consumes an attempt. Terminal fingerprints cannot restart.
    """

    def __init__(self, *, clock, max_attempts=3, backoffs_s=(120, 600),
                 wait_infra_s=86400):
        backoffs_s = tuple(backoffs_s)
        if (max_attempts < 1 or wait_infra_s <= 0
                or len(backoffs_s) < max_attempts - 1
                or any(delay <= 0 for delay in backoffs_s)):
            raise ValueError("invalid retry limits")
        self._clock = clock
        self._max_attempts = max_attempts
        self._backoffs = backoffs_s
        self._wait_infra_s = wait_infra_s
        self._batches: dict[str, _Attempt] = {}

    def status(self, fp):
        entry = self._batches.get(fp)
        if entry is None:
            return "NEW"
        if (entry.status == "WAIT_INFRA" and entry.first_infra is not None
                and self._clock() >= entry.first_infra + self._wait_infra_s):
            entry.status = "ESCALATED"
            entry.retry_at = None
        return entry.status

    def due(self, fp) -> bool:
        status = self.status(fp)
        if status == "NEW":
            return True
        if status not in ("RETRY", "WAIT_INFRA"):
            return False
        return self._clock() >= self._batches[fp].retry_at

    def begin(self, fp) -> bool:
        if not self.due(fp):
            return False
        entry = self._batches.setdefault(fp, _Attempt())
        entry.status = "IN_FLIGHT"
        entry.retry_at = None
        return True

    def wait_infra(self, fp):
        """Age queue-only infrastructure waits without spending a model attempt."""
        status = self.status(fp)
        if status in ('DONE', 'ESCALATED', 'IN_FLIGHT'):
            return status
        entry = self._batches.setdefault(fp, _Attempt())
        now = self._clock()
        if entry.first_infra is None:
            entry.first_infra = now
        if entry.retry_at is None:
            entry.retry_at = now
        entry.status = 'WAIT_INFRA'
        return self.status(fp)

    def finish(self, fp, outcome):
        if outcome not in ("ok", "failed", "infra"):
            raise ValueError("unknown attempt outcome")
        entry = self._batches.get(fp)
        if entry is None or entry.status != "IN_FLIGHT":
            raise ValueError("attempt is not in flight")
        now = self._clock()
        if outcome == "ok":
            entry.status = "DONE"
        elif outcome == "failed":
            entry.failures += 1
            if entry.failures >= self._max_attempts:
                entry.status = "ESCALATED"
            else:
                entry.status = "RETRY"
                entry.retry_at = now + self._backoffs[entry.failures - 1]
        else:
            if entry.first_infra is None:
                entry.first_infra = now
            deadline = entry.first_infra + self._wait_infra_s
            if now >= deadline:
                entry.status = "ESCALATED"
            else:
                entry.status = "WAIT_INFRA"
                entry.retry_at = min(now + 20, deadline)

    def retry_at(self, fp):
        self.status(fp)
        entry = self._batches.get(fp)
        return entry.retry_at if entry else None


class AssistantQueue:
    """FIFO admission with dedupe and paced recovery until the queue empties."""

    def __init__(self, *, clock, max_size=200, drain_interval_s=20):
        if max_size < 1 or drain_interval_s <= 0:
            raise ValueError("queue limits must be positive")
        self._clock = clock
        self._max_size = max_size
        self._interval = drain_interval_s
        self._queue = deque()
        self._members = set()
        self._recovering = False
        self._next_pop = 0.0

    def push(self, fp) -> bool:
        if fp in self._members or len(self) >= self._max_size:
            return False
        self._queue.append(fp)
        self._members.add(fp)
        return True

    def mark_recovered(self):
        if self._queue and not self._recovering:
            self._recovering = True
            self._next_pop = self._clock()

    def pop_ready(self) -> str | None:
        now = self._clock()
        if not self._queue or (self._recovering and now < self._next_pop):
            return None
        fp = self._queue.popleft()
        self._members.remove(fp)
        if self._recovering:
            self._next_pop = now + self._interval
        if not self._queue:
            self._recovering = False
        return fp

    def __len__(self):
        return len(self._queue)


class DailyCap:
    """Count admitted model calls by local day using a Unix-seconds clock.

    ``try_acquire`` (also named ``allow``) checks and consumes one call atomically
    within the caller's serialized loop. Zero disables enforcement, not counting.
    """

    def __init__(self, limit=0, *, clock, tz_offset_hours=7):
        if limit < 0:
            raise ValueError("daily limit must be nonnegative")
        self._limit = limit
        self._clock: Callable[[], float] = clock
        self._offset_s = tz_offset_hours * 3600
        self._day = None
        self._calls = 0

    def try_acquire(self) -> bool:
        day = (self._clock() + self._offset_s) // 86400
        if day != self._day:
            self._day = day
            self._calls = 0
        if self._limit and self._calls >= self._limit:
            return False
        self._calls += 1
        return True

    allow = try_acquire
