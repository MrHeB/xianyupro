"""AI 自动回复准入策略（纯函数，零 DB 依赖，可单测——参考盘点 §4.9 优先级 ★5）。

完整判定序（参考 pro automation_runtime 硬条件，自建裁剪为可离线单测的子集）：
mode(auto/hybrid/manual) → maxDailyReplies → 会话级暂停 →
pauseOnHumanIntervene(30min) → 黑名单关键词 → 工作时段。
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ReplyMode(str, Enum):
    AUTO = "auto"        # 全自动
    HYBRID = "hybrid"    # 生成草稿人工确认（v1 先当 auto 处理，前端确认按钮后置）
    MANUAL = "manual"    # 纯人工


@dataclass(frozen=True)
class PolicyDecision:
    should_reply: bool
    reason: str


@dataclass(frozen=True)
class PolicyContext:
    mode: ReplyMode = ReplyMode.AUTO
    daily_reply_count: int = 0
    max_daily_replies: int = 200
    last_manual_reply_at: datetime | None = None
    last_auto_reply_at: datetime | None = None
    # 人工接管后暂停窗口（秒）
    manual_pause_seconds: int = 1800
    # —— 会话级状态（conversation 表状态机）——
    conversation_paused: bool = False     # auto_reply_paused / manual_disabled
    # —— 关键词闸：命中即不自动回复（留给人工）——
    blocked_keywords: tuple[str, ...] = ()
    message_text: str = ""
    # —— 工作时段（None = 全天）——
    work_hour_start: int | None = None    # 0-23 含
    work_hour_end: int | None = None      # 0-23 含
    # —— 同会话最小自动回复间隔（秒），防连发风暴 ——
    auto_reply_min_interval: int = 8


def evaluate_reply_policy(ctx: PolicyContext, now: datetime) -> PolicyDecision:
    """判定是否允许自动回复。命中任何一条闸门即拒绝并给出原因。"""
    if ctx.mode == ReplyMode.MANUAL:
        return PolicyDecision(False, "manual 模式不自动回复")

    if ctx.daily_reply_count >= ctx.max_daily_replies:
        return PolicyDecision(False, "超过每日回复上限")

    if ctx.conversation_paused:
        return PolicyDecision(False, "会话已暂停自动回复")

    if ctx.last_manual_reply_at is not None:
        elapsed = (now - ctx.last_manual_reply_at).total_seconds()
        if elapsed < ctx.manual_pause_seconds:
            return PolicyDecision(False, "人工已接管（30min 暂停窗口内）")

    if ctx.blocked_keywords:
        text = ctx.message_text or ""
        for keyword in ctx.blocked_keywords:
            if keyword and re.search(keyword, text):
                return PolicyDecision(False, f"命中关键词闸：{keyword}")

    if not _in_work_hours(now.hour, ctx.work_hour_start, ctx.work_hour_end):
        return PolicyDecision(False, "不在工作时段")

    if (
        ctx.last_auto_reply_at is not None
        and (now - ctx.last_auto_reply_at).total_seconds() < ctx.auto_reply_min_interval
    ):
        return PolicyDecision(False, "距上次自动回复间隔过短")

    return PolicyDecision(True, "通过")


def _in_work_hours(hour: int, start: int | None, end: int | None) -> bool:
    """工作时段判定。None/None = 全天；支持跨夜（如 22→6）。"""
    if start is None or end is None:
        return True
    if start <= end:
        return start <= hour <= end
    return hour >= start or hour <= end
