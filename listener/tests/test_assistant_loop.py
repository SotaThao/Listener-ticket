"""Deterministic scheduling boundaries without I/O, sleeps, or other streams."""
import unittest

from tg_triage.assistant_loop import (
    ACTIONS, LINKS, AssistantQueue, AttemptTracker, CircuitBreaker, DailyCap,
    RateLimiter,
)


class FakeClock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.breaker = CircuitBreaker("gemini", clock=self.clock)

    def trip(self):
        for _ in range(3):
            self.breaker.record_failure("transient")

    def test_closed_open_half_open_single_trial_then_success(self):
        self.assertEqual(self.breaker.state, "closed")
        self.assertTrue(self.breaker.allow())
        for _ in range(2):
            self.breaker.record_failure("transient")
            self.assertTrue(self.breaker.allow())
        self.breaker.record_failure("transient")
        self.assertEqual(self.breaker.state, "open")
        self.assertFalse(self.breaker.allow())
        self.clock.advance(899.999)
        self.assertFalse(self.breaker.allow())
        self.clock.advance(.001)
        self.assertEqual(self.breaker.state, "half_open")
        self.assertTrue(self.breaker.allow())
        for _ in range(50):
            self.assertEqual(self.breaker.state, "half_open")
            self.assertFalse(self.breaker.allow())
        self.breaker.record_success()
        self.assertEqual(self.breaker.state, "closed")
        self.assertTrue(self.breaker.allow())

    def test_auth_opens_immediately_for_sixty_minutes(self):
        self.breaker.record_failure("auth")
        self.assertEqual(self.breaker.state, "open")
        self.clock.advance(3599)
        self.assertFalse(self.breaker.allow())
        self.clock.advance(1)
        self.assertTrue(self.breaker.allow())
        self.assertFalse(self.breaker.allow())

    def test_failed_half_open_doubles_cooldown_up_to_four_hours(self):
        self.trip()
        duration = 900
        for expected in (1800, 3600, 7200, 14400, 14400):
            self.clock.advance(duration)
            self.assertTrue(self.breaker.allow())
            self.breaker.record_failure("transient")
            self.assertEqual(self.breaker.state, "open")
            self.clock.advance(expected - 1)
            self.assertFalse(self.breaker.allow())
            self.clock.advance(1)
            self.assertEqual(self.breaker.state, "half_open")
            duration = 0

    def test_success_clears_failure_history_and_resets_cooldown(self):
        self.breaker.record_failure("transient")
        self.breaker.record_failure("transient")
        self.breaker.record_success()
        self.breaker.record_failure("transient")
        self.assertEqual(self.breaker.state, "closed")
        self.trip()
        self.clock.advance(900)
        self.assertTrue(self.breaker.allow())
        self.breaker.record_success()
        self.trip()
        self.clock.advance(900)
        self.assertTrue(self.breaker.allow())

    def test_old_failures_expire_at_window_boundary(self):
        self.breaker.record_failure("transient")
        self.clock.advance(150)
        self.breaker.record_failure("transient")
        self.clock.advance(150)
        self.breaker.record_failure("transient")
        self.assertEqual(self.breaker.state, "closed")
        self.breaker.record_failure("rate_limited")
        self.assertEqual(self.breaker.state, "open")

    def test_schema_and_budget_errors_do_not_trip_closed_breaker(self):
        for kind in ("schema", "truncated", "too_large", "duplicate_tool", "limit_reached"):
            for _ in range(4):
                self.breaker.record_failure(kind)
            self.assertEqual(self.breaker.state, "closed")
        self.trip()
        self.clock.advance(900)
        self.assertTrue(self.breaker.allow())
        self.breaker.record_failure("schema")
        self.assertEqual(self.breaker.state, "open")

    def test_links_have_independent_breakers_and_custom_limits(self):
        breakers = {name: CircuitBreaker(name, clock=self.clock, threshold=1,
                                        open_s=10, auth_open_s=30) for name in LINKS}
        breakers["gemini"].record_failure("transient")
        self.assertFalse(breakers["gemini"].allow())
        self.assertTrue(breakers["gpt"].allow())
        self.assertTrue(breakers["deepseek"].allow())
        self.clock.advance(10)
        self.assertTrue(breakers["gemini"].allow())
        breakers["gpt"].record_failure("auth")
        self.clock.advance(29)
        self.assertFalse(breakers["gpt"].allow())
        self.clock.advance(1)
        self.assertTrue(breakers["gpt"].allow())


class AttemptTrackerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.tracker = AttemptTracker(clock=self.clock)

    def test_two_ten_minute_backoffs_then_escalated(self):
        self.assertEqual(self.tracker.status("a"), "NEW")
        self.assertIsNone(self.tracker.retry_at("a"))
        self.assertTrue(self.tracker.due("a"))
        for delay in (120, 600):
            self.assertTrue(self.tracker.begin("a"))
            self.assertEqual(self.tracker.status("a"), "IN_FLIGHT")
            self.tracker.finish("a", "failed")
            self.assertEqual(self.tracker.status("a"), "RETRY")
            self.assertEqual(self.tracker.retry_at("a"), self.clock() + delay)
            self.clock.advance(delay - 1)
            self.assertFalse(self.tracker.due("a"))
            self.assertFalse(self.tracker.begin("a"))
            self.clock.advance(1)
            self.assertTrue(self.tracker.due("a"))
        self.assertTrue(self.tracker.begin("a"))
        self.tracker.finish("a", "failed")
        self.assertEqual(self.tracker.status("a"), "ESCALATED")
        self.assertIsNone(self.tracker.retry_at("a"))
        self.assertFalse(self.tracker.begin("a"))
        self.assertFalse(self.tracker.due("a"))

    def test_idempotency_and_success_are_per_fingerprint(self):
        self.assertTrue(self.tracker.begin("a"))
        for _ in range(50):
            self.assertFalse(self.tracker.begin("a"))
        self.assertFalse(self.tracker.due("a"))
        self.assertTrue(self.tracker.begin("b"))
        self.tracker.finish("a", "ok")
        self.assertEqual(self.tracker.status("a"), "DONE")
        self.assertFalse(self.tracker.begin("a"))
        self.assertFalse(self.tracker.due("a"))
        self.assertIsNone(self.tracker.retry_at("a"))
        self.assertEqual(self.tracker.status("b"), "IN_FLIGHT")

    def test_infra_retries_do_not_consume_attempts(self):
        for _ in range(10):
            self.assertTrue(self.tracker.begin("a"))
            self.tracker.finish("a", "infra")
            self.assertEqual(self.tracker.status("a"), "WAIT_INFRA")
            self.assertEqual(self.tracker.retry_at("a"), self.clock() + 20)
            self.assertFalse(self.tracker.begin("a"))
            self.clock.advance(20)
        for delay in (120, 600):
            self.assertTrue(self.tracker.begin("a"))
            self.tracker.finish("a", "failed")
            self.assertEqual(self.tracker.status("a"), "RETRY")
            self.assertEqual(self.tracker.retry_at("a"), self.clock() + delay)
            self.clock.advance(delay)
        self.assertTrue(self.tracker.begin("a"))
        self.tracker.finish("a", "failed")
        self.assertEqual(self.tracker.status("a"), "ESCALATED")

    def test_infra_deadline_is_first_failure_plus_twenty_four_hours(self):
        self.tracker.begin("a")
        self.tracker.finish("a", "infra")
        self.clock.advance(86390)
        self.assertTrue(self.tracker.begin("a"))
        self.tracker.finish("a", "infra")
        self.assertEqual(self.tracker.retry_at("a"), 86400)
        self.clock.advance(9)
        self.assertEqual(self.tracker.status("a"), "WAIT_INFRA")
        self.clock.advance(1)
        self.assertEqual(self.tracker.status("a"), "ESCALATED")
        self.assertFalse(self.tracker.due("a"))
        self.assertFalse(self.tracker.begin("a"))
        self.assertIsNone(self.tracker.retry_at("a"))

    def test_inflight_result_after_infra_deadline(self):
        self.tracker.begin("a")
        self.tracker.finish("a", "infra")
        self.clock.advance(20)
        self.tracker.begin("a")
        self.clock.advance(86400)
        self.assertEqual(self.tracker.status("a"), "IN_FLIGHT")
        self.tracker.finish("a", "infra")
        self.assertEqual(self.tracker.status("a"), "ESCALATED")

    def test_custom_attempt_limit_and_success_after_retry(self):
        tracker = AttemptTracker(clock=self.clock, max_attempts=1, backoffs_s=())
        tracker.begin("a")
        tracker.finish("a", "failed")
        self.assertEqual(tracker.status("a"), "ESCALATED")
        self.tracker.begin("a")
        self.tracker.finish("a", "failed")
        self.clock.advance(120)
        self.tracker.begin("a")
        self.tracker.finish("a", "ok")
        self.assertEqual(self.tracker.status("a"), "DONE")

    def test_invalid_finish_does_not_create_or_consume_attempt(self):
        with self.assertRaises(ValueError):
            self.tracker.finish("a", "failed")
        self.assertEqual(self.tracker.status("a"), "NEW")
        self.tracker.begin("a")
        with self.assertRaises(ValueError):
            self.tracker.finish("a", "typo")
        self.assertEqual(self.tracker.status("a"), "IN_FLIGHT")
        self.tracker.finish("a", "ok")
        with self.assertRaises(ValueError):
            self.tracker.finish("a", "failed")
        self.assertEqual(self.tracker.status("a"), "DONE")


class RateLimiterTests(unittest.TestCase):
    def test_default_twenty_calls_and_exact_sixty_second_boundary(self):
        clock = FakeClock()
        limiter = RateLimiter(clock=clock)
        for _ in range(20):
            self.assertTrue(limiter.try_acquire())
        self.assertFalse(limiter.try_acquire())
        clock.advance(59.999)
        self.assertFalse(limiter.try_acquire())
        clock.advance(.001)
        self.assertTrue(limiter.try_acquire())

    def test_rolling_window_and_denial_does_not_extend_wait(self):
        clock = FakeClock()
        limiter = RateLimiter(2, clock=clock)
        self.assertTrue(limiter.try_acquire())
        clock.advance(30)
        self.assertTrue(limiter.try_acquire())
        clock.advance(29)
        self.assertFalse(limiter.try_acquire())
        clock.advance(1)
        self.assertTrue(limiter.try_acquire())
        self.assertFalse(limiter.try_acquire())
        clock.advance(30)
        self.assertTrue(limiter.try_acquire())


class AssistantQueueTests(unittest.TestCase):
    def test_default_cap_dedupe_fifo_and_readmission(self):
        queue = AssistantQueue(clock=FakeClock())
        for i in range(200):
            self.assertTrue(queue.push(str(i)))
        self.assertFalse(queue.push("0"))
        self.assertFalse(queue.push("overflow"))
        self.assertEqual(len(queue), 200)
        self.assertEqual(queue.pop_ready(), "0")
        self.assertTrue(queue.push("0"))
        for i in range(1, 200):
            self.assertEqual(queue.pop_ready(), str(i))
        self.assertEqual(queue.pop_ready(), "0")
        self.assertEqual(len(queue), 0)
        self.assertIsNone(queue.pop_ready())

    def test_fifty_items_drain_at_most_one_per_twenty_seconds(self):
        clock = FakeClock()
        queue = AssistantQueue(clock=clock)
        for i in range(50):
            queue.push(str(i))
        queue.mark_recovered()
        times = []
        for i in range(50):
            self.assertEqual(queue.pop_ready(), str(i))
            times.append(clock())
            if i < 49:
                queue.mark_recovered()  # Repeated notifications cannot reset pacing.
                self.assertIsNone(queue.pop_ready())
                clock.advance(19.5)
                self.assertIsNone(queue.pop_ready())
                clock.advance(.5)
        self.assertEqual(times, [20 * i for i in range(50)])
        self.assertEqual(len(queue), 0)
        self.assertIsNone(queue.pop_ready())
        queue.push("fresh-a")
        queue.push("fresh-b")
        self.assertEqual(queue.pop_ready(), "fresh-a")
        self.assertEqual(queue.pop_ready(), "fresh-b")

    def test_recovery_slow_consumer_has_no_catchup_burst(self):
        clock = FakeClock()
        queue = AssistantQueue(clock=clock, max_size=3, drain_interval_s=7)
        queue.mark_recovered()  # Empty recovery does not pace future arrivals.
        for fp in ("a", "b", "c"):
            queue.push(fp)
        queue.mark_recovered()
        self.assertEqual(queue.pop_ready(), "a")
        clock.advance(100)
        self.assertEqual(queue.pop_ready(), "b")
        queue.push("d")
        self.assertIsNone(queue.pop_ready())
        clock.advance(7)
        self.assertEqual(queue.pop_ready(), "c")
        self.assertIsNone(queue.pop_ready())
        clock.advance(7)
        self.assertEqual(queue.pop_ready(), "d")


class DailyCapTests(unittest.TestCase):
    def test_default_cap_is_off(self):
        cap = DailyCap(clock=FakeClock())
        for _ in range(1000):
            self.assertTrue(cap.try_acquire())

    def test_enforcing_cap_and_reset_at_local_midnight(self):
        clock = FakeClock(17 * 3600 - 1)  # UTC+7 is one second before midnight.
        cap = DailyCap(2, clock=clock)
        self.assertTrue(cap.try_acquire())
        self.assertTrue(cap.allow())
        self.assertFalse(cap.try_acquire())
        self.assertFalse(cap.allow())
        clock.advance(1)
        self.assertTrue(cap.try_acquire())
        self.assertTrue(cap.try_acquire())
        self.assertFalse(cap.try_acquire())
        clock.advance(3 * 86400)
        self.assertTrue(cap.try_acquire())

    def test_custom_negative_timezone(self):
        clock = FakeClock(5 * 3600 - 1)
        cap = DailyCap(1, clock=clock, tz_offset_hours=-5)
        self.assertTrue(cap.allow())
        self.assertFalse(cap.allow())
        clock.advance(1)
        self.assertTrue(cap.allow())


class PolicyTests(unittest.TestCase):
    def test_link_order_and_error_actions(self):
        self.assertEqual(LINKS, ("gemini", "gpt", "deepseek"))
        self.assertEqual(dict(ACTIONS), {
            "transient": "next_link", "rate_limited": "next_link",
            "auth": "next_link+open_breaker_auth",
            "schema": "repair_once_then_next", "truncated": "repair_once_then_next",
            "too_large": "next_link", "duplicate_tool": "force_proposal",
            "limit_reached": "force_proposal",
        })

    def test_invalid_limits_are_rejected(self):
        clock = FakeClock()
        constructors = (
            lambda: CircuitBreaker("a", clock=clock, threshold=0),
            lambda: CircuitBreaker("a", clock=clock, window_s=0),
            lambda: RateLimiter(0, clock=clock),
            lambda: AttemptTracker(clock=clock, max_attempts=0),
            lambda: AttemptTracker(clock=clock, backoffs_s=(1,)),
            lambda: AttemptTracker(clock=clock, backoffs_s=(0, 1)),
            lambda: AttemptTracker(clock=clock, wait_infra_s=0),
            lambda: AssistantQueue(clock=clock, max_size=0),
            lambda: AssistantQueue(clock=clock, drain_interval_s=0),
            lambda: DailyCap(-1, clock=clock),
        )
        for constructor in constructors:
            with self.subTest(constructor=constructor), self.assertRaises(ValueError):
                constructor()


if __name__ == "__main__":
    unittest.main()
