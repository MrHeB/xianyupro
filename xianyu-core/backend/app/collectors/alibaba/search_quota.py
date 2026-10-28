"""每个 1688 货源账号的检索配额（需求文档 §4.5 / N7）。

只管**定时**路径：手动入口的闸门是"人自己点的那一下"（§1 N7），所以 `run_collect_pipeline`
只在被显式传入配额对象时才扣费，端点那条调用不带它。

计数单位是**开一次检索浏览器**，不是"跑一次定时"——一轮 pipeline 最多换
`MAX_SEARCH_ROUNDS` 次词，每次都是一整趟 chromium + 一次对外请求，搜索页比详情页更敏感
（§6 风险 2），所以按次扣才对得上"每小时几次"这句话。

进程内滑动窗口，与 `search_browser` 的 `Semaphore(1)` 同一口径：backend 是单进程 asyncio
常驻部署，重启即清零。重启清零意味着"重启后允许一次 burst"，这比给调度器加一张计数表便宜，
而真要防跨进程频控，前置条件是先把部署改成多副本（那时连同令牌桶一起迁 Redis，是既定待办）。
"""

import time
from collections import deque
from collections.abc import Callable

from app.core.config import settings

WINDOW_SECONDS = 3600.0


class SearchQuota:
    """`alibaba_account_id` → 一小时内已用检索次数。"""

    def __init__(
        self,
        per_hour: int | Callable[[], int],
        *,
        window_seconds: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._per_hour = per_hour
        self._window = float(window_seconds)
        self._clock = clock
        self._hits: dict[int, deque[float]] = {}

    @property
    def limit(self) -> int:
        raw = self._per_hour() if callable(self._per_hour) else self._per_hour
        return max(0, int(raw))

    def _fresh(self, account_id: int, now: float) -> deque[float]:
        bucket = self._hits.setdefault(account_id, deque())
        while bucket and now - bucket[0] >= self._window:
            bucket.popleft()
        return bucket

    def remaining(self, alibaba_account_id: int) -> int:
        return max(0, self.limit - len(self._fresh(alibaba_account_id, self._clock())))

    def acquire(self, alibaba_account_id: int) -> bool:
        """占用一个名额。拿不到就返回 False，调用方**不要**去开浏览器。"""
        limit = self.limit
        if limit <= 0:
            return False  # 配 0 = 关掉定时检索，不是"随便跑"
        bucket = self._fresh(alibaba_account_id, self._clock())
        if len(bucket) >= limit:
            return False
        bucket.append(self._clock())
        return True

    def reset(self) -> None:
        self._hits.clear()


search_quota = SearchQuota(lambda: settings.collect_search_hourly_quota)
