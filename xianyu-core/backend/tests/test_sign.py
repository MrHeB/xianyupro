"""mtop 签名算法测试（算法来自 protocol.md §1.2）。"""

import hashlib

from app.api_client.sign import compact_json, extract_token, make_sign, signed_params


def test_compact_json_no_whitespace():
    assert compact_json({"b": 1, "a": "x"}) == '{"b":1,"a":"x"}'


def test_extract_token():
    cookie = "unb=123; _m_h5_tk=abc123def_987; cookie2=xyz"
    assert extract_token(cookie) == "abc123def"
    assert extract_token("unb=123") == ""


def test_make_sign_matches_manual_computation():
    data_str = '{"itemId":"123"}'
    t = 1710000000000
    app_key = "34839810"
    expected = hashlib.md5(f"token&{t}&{app_key}&{data_str}".encode()).hexdigest()
    assert make_sign("token", t, app_key, data_str) == expected


def test_signed_params_shape():
    params = signed_params("mtop.taobao.idle.pc.detail", "1.0", {"itemId": "123"}, "tok", "34839810")
    assert params["appKey"] == "34839810"
    assert params["jsv"] == "2.7.2"
    assert params["api"] == "mtop.taobao.idle.pc.detail"
    assert params["v"] == "1.0"
    assert params["sessionOption"] == "AutoLoginOnly"
    assert len(params["t"]) == 13  # 真实毫秒时间戳
    assert len(params["sign"]) == 32
