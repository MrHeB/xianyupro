"""付款消息判定与节流测试（protocol.md §7.7）。"""

from app.messaging.payment import PaymentThrottle, compile_patterns, is_payment_message


def _msg(content: str, content_type: int = 26) -> dict:
    return {"contentType": content_type, "msgContent": content}


def test_positive_keyword():
    for text in ("我已付款，等待你发货", "买家已付款", "等待你发货哦"):
        assert is_payment_message(_msg(text)).is_payment, text


def test_negative_keyword_wins():
    j = is_payment_message(_msg("买家已拍下，待付款"))
    assert not j.is_payment
    assert "负向" in j.reason


def test_content_type_must_be_26():
    j = is_payment_message(_msg("我已付款", content_type=1))
    assert not j.is_payment
    assert "非 26" in j.reason


def test_no_keyword_match():
    j = is_payment_message(_msg("买家确认收货，交易成功"))
    assert not j.is_payment
    assert "未命中" in j.reason


def test_custom_patterns():
    positive = compile_patterns((r"PAID!",))
    negative = compile_patterns((r"REFUND",))
    assert is_payment_message(_msg("PAID!"), positive=positive, negative=negative).is_payment
    assert not is_payment_message(_msg("PAID! REFUND"), positive=positive, negative=negative).is_payment


def test_throttle_first_emit_then_block():
    t = PaymentThrottle(window_seconds=60)
    assert t.should_emit(1, "pnm-1", now=100.0)
    assert not t.should_emit(1, "pnm-1", now=130.0)   # 30s < 60s
    assert t.should_emit(1, "pnm-1", now=161.0)       # 出窗
    assert t.should_emit(1, "pnm-2", now=161.0)       # 不同 pnm_id 不互相影响
    assert t.should_emit(2, "pnm-1", now=161.0)       # 不同账号不互相影响


def test_throttle_empty_pnm_always_emits():
    t = PaymentThrottle()
    assert t.should_emit(1, "", now=1.0)
    assert t.should_emit(1, "", now=1.0)


def test_throttle_cleanup_keeps_size_bounded():
    t = PaymentThrottle(window_seconds=60)
    for i in range(2000):
        t.should_emit(1, f"pnm-{i}", now=float(i))
    assert len(t._seen) < 2000  # 触发过过期清理
