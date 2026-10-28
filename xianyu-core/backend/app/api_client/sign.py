"""mtop 签名算法（protocol.md §1.2，三处参考源码交叉验证一致）。

sign = md5(token + "&" + t + "&" + appKey + "&" + data)
- token  = Cookie `_m_h5_tk` 第一个 "_" 之前的部分；
- t      = int(time.time() * 1000)（真实毫秒，不要抹零）；
- data   = json.dumps(data, separators=(",", ":"))，紧凑序列化，字段序敏感！
"""

import hashlib
import json
import time


def compact_json(data: dict) -> str:
    """紧凑 JSON——任何排序/空格都会导致签名失败。"""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def extract_token(cookie: str) -> str:
    """从 Cookie 串提取 _m_h5_tk 中 "_" 之前的部分（签名用 token）。"""
    for pair in cookie.split(";"):
        key, _, value = pair.strip().partition("=")
        if key == "_m_h5_tk":
            return value.split("_", 1)[0]
    return ""


def make_sign(token: str, t_ms: int, app_key: str, data_str: str) -> str:
    raw = f"{token}&{t_ms}&{app_key}&{data_str}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def signed_params(api: str, version: str, data: dict, token: str, app_key: str) -> dict:
    """构造一次 mtop 调用的完整公共 query 参数（protocol.md §1.1）。"""
    data_str = compact_json(data)
    t_ms = int(time.time() * 1000)
    return {
        "jsv": "2.7.2",
        "appKey": app_key,
        "t": str(t_ms),
        "sign": make_sign(token, t_ms, app_key, data_str),
        "v": version,
        "api": api,
        "type": "originaljson",
        "dataType": "json",
        "timeout": "20000",
        "sessionOption": "AutoLoginOnly",
        "accountSite": "xianyu",
    }
