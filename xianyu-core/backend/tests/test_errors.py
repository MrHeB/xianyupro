"""mtop 错误分类测试。"""

from app.api_client.errors import ErrorKind, classify_error


def test_risk_control_patterns():
    for ret in (["RGV587_ERROR::哎呀出错了"], ["FAIL_SYS_USER_VALIDATE::x"]):
        assert classify_error(ret) == ErrorKind.RISK_CONTROL


def test_token_expired_with_server_typo():
    # 服务端真实拼写是 EXOIRED，勿"修复"
    assert classify_error(["FAIL_SYS_TOKEN_EXOIRED::x"]) == ErrorKind.TOKEN_EXPIRED


def test_session_expired():
    assert classify_error(["FAIL_SYS_SESSION_EXPIRED::x"]) == ErrorKind.SESSION_EXPIRED


def test_illegal_access():
    assert classify_error(["FAIL_SYS_ILLEGAL_ACCESS::x"]) == ErrorKind.ILLEGAL_ACCESS


def test_biz_error():
    assert classify_error(["FAIL_BIZ_SKU_PRICE_ILLEGAL::x"]) == ErrorKind.BIZ_ERROR
