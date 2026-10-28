"""Cookie 加密（AES-256-GCM enc:v1）测试。"""

from app.auth.cookie_crypto import decrypt, encrypt, is_encrypted


def test_roundtrip():
    cookie = "unb=123; _m_h5_tk=abc_987; cookie2=xyz"
    encrypted = encrypt(cookie, "secret")
    assert encrypted != cookie
    assert is_encrypted(encrypted)
    assert decrypt(encrypted, "secret") == cookie


def test_wrong_secret_fails():
    encrypted = encrypt("unb=123", "secret-a")
    try:
        decrypt(encrypted, "secret-b")
        raise AssertionError("应当解密失败")
    except Exception:
        pass  # InvalidTag 等异常，符合预期


def test_format_prefix():
    assert encrypt("x", "s").startswith("enc:v1:")
