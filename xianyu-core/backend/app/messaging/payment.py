"""付款消息判定与节流（protocol.md §7.7，品类 A 自动发货核心信号）。

- 判定：contentType=26 且文案命中正关键词（"已付款/等待你发货"等），
  且未命中负向（"待付款/拍下待付"）——闲鱼文案是硬编码在参考项目里的，
  文案一变即失效，这里做成**可配置正则表**；
- 节流：同账号同 pnm_id 60s 一次（防周期性推送重复发货），DB 幂等是最后兜底。
"""

import re
import time
from dataclasses import dataclass, field

# 默认正/负关键词（可被 settings 覆盖）
DEFAULT_POSITIVE_PATTERNS = (
    r"已付款",
    r"等待你发货",
    r"等待卖家发货",
    r"买家已付款",
)
DEFAULT_NEGATIVE_PATTERNS = (
    r"待付款",
    r"拍下待付",
    r"未付款",
    r"取消交易",
    r"退款",
)


@dataclass(frozen=True)
class PaymentJudgement:
    is_payment: bool
    reason: str = ""


def compile_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


def is_payment_message(
    parsed: dict,
    *,
    positive: tuple[re.Pattern[str], ...] | None = None,
    negative: tuple[re.Pattern[str], ...] | None = None,
) -> PaymentJudgement:
    """判定解析后的消息是否为"买家已付款"。

    parsed 为 decoder.parse_message 的规范化输出。
    """
    if parsed.get("contentType") != 26:
        return PaymentJudgement(False, f"contentType={parsed.get('contentType')} 非 26")

    content = str(parsed.get("msgContent") or parsed.get("reminderContent") or "")
    if not content:
        return PaymentJudgement(False, "无文案可判定")

    positive = positive or compile_patterns(DEFAULT_POSITIVE_PATTERNS)
    negative = negative or compile_patterns(DEFAULT_NEGATIVE_PATTERNS)

    if any(p.search(content) for p in negative):
        return PaymentJudgement(False, "命中负向关键词")
    if any(p.search(content) for p in positive):
        return PaymentJudgement(True, "命中正向关键词")
    return PaymentJudgement(False, "未命中正向关键词")


@dataclass
class PaymentThrottle:
    """同账号同 pnm_id 节流（默认 60s）。进程内存版。"""

    window_seconds: float = 60.0
    _seen: dict[tuple[int, str], float] = field(default_factory=dict)

    def should_emit(self, account_id: int, pnm_id: str, now: float | None = None) -> bool:
        """首次（或窗口外）返回 True 并记录；窗口内重复返回 False。"""
        if not pnm_id:
            return True  # 无 ID 无从节流，交给 DB 幂等
        now = now if now is not None else time.time()
        key = (account_id, pnm_id)
        last = self._seen.get(key)
        if last is not None and now - last < self.window_seconds:
            return False
        self._seen[key] = now
        # 顺手清理过期项，防内存无限膨胀
        if len(self._seen) > 1024:
            cutoff = now - self.window_seconds
            self._seen = {k: t for k, t in self._seen.items() if t >= cutoff}
        return True
