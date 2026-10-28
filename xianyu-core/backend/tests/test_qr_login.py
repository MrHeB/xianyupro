"""扫码登录会话状态机离线测试（不触网：mock _poll_once）。"""

import pytest

from app.auth.qr_login import QrLoginService


def _make_service_with_session(session_id: str) -> QrLoginService:
    svc = QrLoginService()
    svc._sessions[session_id] = {
        "created_at": 0.0,  # 1970，必然超时
        "status": "new",
    }
    return svc


async def test_poll_unknown_session():
    svc = QrLoginService()
    result = await svc.poll("nonexistent-" + "x" * 32)
    assert result["status"] == "expired"


async def test_poll_invalid_session_id_format():
    svc = QrLoginService()
    assert (await svc.poll("short"))["status"] == "expired"


async def test_poll_session_timeout():
    session_id = "a" * 40
    svc = _make_service_with_session(session_id)
    result = await svc.poll(session_id)
    assert result["status"] == "expired"
    assert "登录超时" in result["message"]
    assert session_id not in svc._sessions  # 已清理


async def test_take_cookies_missing():
    svc = QrLoginService()
    assert await svc.take_cookies("b" * 40) is None


@pytest.mark.parametrize(
    "raw_status,expected",
    [
        ({"qrCodeStatus": "NEW"}, "new"),
        ({"qrCodeStatus": "SCANED"}, "scaned"),
        ({"qrCodeStatus": "EXPIRED"}, "expired"),
    ],
)
async def test_poll_once_status_mapping(raw_status, expected):
    svc = QrLoginService()

    class FakeResp:
        text = ""
        status_code = 200

        def json(self):
            return {"content": {"data": raw_status}}

    class FakeClient:
        cookies = {}

        async def post(self, *a, **kw):
            return FakeResp()

    result = await svc._poll_once(FakeClient(), {})
    assert result["status"] == expected
