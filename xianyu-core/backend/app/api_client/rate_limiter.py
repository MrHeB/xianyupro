"""风控友好请求框架（protocol.md §1.4）：

- 写操作令牌桶 1 次/分钟（跨进程需换 Redis，当前为单进程内存版）；
- RGV587 命中 → 熔断 10 分钟拒绝后续请求。
"""

import time


class TokenBucket:
    """令牌桶：写操作默认 1 次/分钟。"""

    def __init__(self, rate_per_minute: float = 1.0, capacity: int = 1):
        self.rate = rate_per_minute / 60.0  # 每秒生成速率
        self.capacity = capacity
        self._tokens = float(capacity)
        self._last = time.monotonic()

    def acquire(self) -> bool:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    @property
    def wait_seconds(self) -> float:
        """距下一个令牌可用的秒数（人性化随机延迟可在此基础上加抖动）。"""
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self.rate


class CircuitBreaker:
    """熔断器：风控命中后冷却 cooldown_seconds。"""

    def __init__(self, cooldown_seconds: float = 600.0):
        self.cooldown = cooldown_seconds
        self._opened_at: float | None = None

    def trip(self) -> None:
        self._opened_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown:
            self._opened_at = None  # 半开：放行试探
            return False
        return True
