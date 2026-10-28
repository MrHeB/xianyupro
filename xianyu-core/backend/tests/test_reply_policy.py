"""AI 回复准入策略测试（纯函数）。"""

from datetime import datetime, timedelta

from app.ai.reply_policy import PolicyContext, ReplyMode, evaluate_reply_policy

NOW = datetime(2026, 9, 6, 12, 0, 0)


def test_manual_mode_blocks():
    decision = evaluate_reply_policy(PolicyContext(mode=ReplyMode.MANUAL), NOW)
    assert not decision.should_reply


def test_daily_limit_blocks():
    decision = evaluate_reply_policy(
        PolicyContext(daily_reply_count=200, max_daily_replies=200), NOW
    )
    assert not decision.should_reply


def test_manual_intervention_pause():
    ctx = PolicyContext(last_manual_reply_at=NOW - timedelta(minutes=10))
    assert not evaluate_reply_policy(ctx, NOW).should_reply

    ctx = PolicyContext(last_manual_reply_at=NOW - timedelta(minutes=45))
    assert evaluate_reply_policy(ctx, NOW).should_reply


def test_default_allows():
    assert evaluate_reply_policy(PolicyContext(), NOW).should_reply
