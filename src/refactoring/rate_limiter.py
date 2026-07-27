import os
import threading
import time

import dotenv


class TokenBucketRateLimiter:
    """
    Simple thread-safe token-bucket limiter. Shared across all agent
    instances/threads so the *aggregate* request rate stays under the cap,
    not just each agent's individual rate.

    acquire() blocks until a token is available rather than raising.
    """

    def __init__(self, max_per_minute: int, safety_margin: float = 0.9):
        self.capacity = max(1, int(max_per_minute * safety_margin))
        self.tokens = float(self.capacity)
        self.refill_rate = self.capacity / 60.0  # tokens per second
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
                self.last_refill = now

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return

                deficit = tokens - self.tokens
                wait_time = deficit / self.refill_rate

            time.sleep(wait_time)


dotenv.load_dotenv()

GLOBAL_MODEL_RATE_LIMITER = TokenBucketRateLimiter(max_per_minute=int(os.environ.get("REFACTOR_AGENT_MAX_RPM", 15)))
