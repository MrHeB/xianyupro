"""LWP 帧构造（protocol.md §7，蓝本：goofish-cli ws.py + pro ws_client.py）。

全部纯函数——不碰网络不碰状态，WS 客户端只负责发送/接收。
帧语义：
- /reg                     注册鉴权（app-key + IM accessToken）；
- /r/SyncStatus/ackDiff    增量同步（pts=微秒游标，短轮询：无消息服务端秒回空包）；
- /!                       心跳（15s）+ 对下行包的 ACK（code=200 透传 mid/sid）；
- /r/MessageSend/...       发消息（actualReceivers 必须含发送者自己）。
"""

import base64
import hashlib
import json
import time
import uuid as uuid_lib
from typing import Any

# IM appKey（protocol.md §7.3，固定值）
IM_APP_KEY = "444e9908a51d1cb236a27862abc769c9"

UA_WEB = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
UA_IM = (
    UA_WEB
    + " DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/124.0.0.0) "
    + "DingWeb/2.1.5 IMPaaS DingWeb/2.1.5"
)

WS_HOST = "wss-goofish.dingtalk.com"


def generate_mid() -> str:
    """帧头 mid：随机数 + 毫秒时间戳 + " 0"（对齐 pro ws_protocol.generate_mid）。"""
    ts = int(time.time() * 1000)
    rand = int(hashlib.md5(str(ts).encode()).hexdigest(), 16) % 1000
    return f"{rand}{ts} 0"


def generate_uuid() -> str:
    """消息体 uuid（发送消息用）。"""
    return uuid_lib.uuid4().hex


def build_device_id(unb: str) -> str:
    """/reg 的 did：`<UUID>-<unb>`（protocol.md §7.3）。"""
    return f"{uuid_lib.uuid4().hex.upper()}-{unb}"


def build_reg_frame(token: str, device_id: str) -> dict[str, Any]:
    """注册鉴权帧。"""
    return {
        "lwp": "/reg",
        "headers": {
            "cache-header": "app-key token ua wv",
            "app-key": IM_APP_KEY,
            "token": token,
            "ua": UA_IM,
            "dt": "j",
            "wv": "im:3,au:3,sy:6",
            "sync": "0,0;0;0;",
            "did": device_id,
            "mid": generate_mid(),
        },
    }


def build_ack_diff(pts: int = 0) -> dict[str, Any]:
    """增量同步帧。pts=0 表示全量；正常轮询用微秒时间戳游标。"""
    current_ms = int(time.time() * 1000)
    return {
        "lwp": "/r/SyncStatus/ackDiff",
        "headers": {"mid": generate_mid()},
        "body": [
            {
                "pipeline": "sync",
                "tooLong2Tag": "PNM,1",
                "channel": "sync",
                "topic": "sync",
                "highPts": 0,
                "pts": pts or current_ms * 1000,
                "seq": 0,
                "timestamp": current_ms,
            }
        ],
    }


def build_heartbeat_frame() -> dict[str, Any]:
    """15s 空心跳。"""
    return {"lwp": "/!", "headers": {"mid": generate_mid()}}


def build_ack_frame(msg: dict[str, Any]) -> dict[str, Any]:
    """对下行包回 code=200 ACK，透传关键 header（先 ACK 再处理）。"""
    h = msg.get("headers") or {}
    ack: dict[str, Any] = {
        "code": 200,
        "headers": {
            "mid": h.get("mid") or generate_mid(),
            "sid": h.get("sid", ""),
        },
    }
    for k in ("app-key", "ua", "dt"):
        if k in h:
            ack["headers"][k] = h[k]
    return ack


def _encode_custom_payload(payload: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("utf-8")


def build_send_text_frame(myid: str, cid: str, toid: str, text: str) -> dict[str, Any]:
    """发文本消息：contentType=101 + custom{type:1, data:base64(JSON)}。

    ⚠️ actualReceivers 必须包含发送者自己，否则 code=500（protocol.md §7.5）。
    """
    payload = {"contentType": 1, "text": {"text": text}}
    return _build_send_frame(myid, cid, toid, ctype=1, data_b64=_encode_custom_payload(payload))


def build_send_image_frame(
    myid: str, cid: str, toid: str, url: str, width: int = 800, height: int = 800
) -> dict[str, Any]:
    """发图片消息（url 必须是闲鱼图床 URL，裸外链不被协议支持）。"""
    payload = {
        "contentType": 2,
        "image": {"pics": [{"type": 0, "url": url, "width": width, "height": height}]},
    }
    return _build_send_frame(myid, cid, toid, ctype=2, data_b64=_encode_custom_payload(payload))


def _build_send_frame(myid: str, cid: str, toid: str, ctype: int, data_b64: str) -> dict[str, Any]:
    return {
        "lwp": "/r/MessageSend/sendByReceiverScope",
        "headers": {"mid": generate_mid()},
        "body": [
            {
                "uuid": generate_uuid(),
                "cid": f"{cid}@goofish",
                "conversationType": 1,
                "content": {
                    "contentType": 101,
                    "custom": {"type": ctype, "data": data_b64},
                },
                "redPointPolicy": 0,
                "extension": {"extJson": "{}"},
                "ctx": {"appVersion": "1.0", "platform": "web"},
                "mtags": {},
                "msgReadStatusSetting": 1,
            },
            {"actualReceivers": [f"{toid}@goofish", f"{myid}@goofish"]},
        ],
    }


def build_create_chat_frame(myid: str, toid: str, item_id: str) -> dict[str, Any]:
    """无会话时建单聊会话（extension.itemId 关联商品）。"""
    return {
        "lwp": "/r/SingleChatConversation/create",
        "headers": {"mid": generate_mid()},
        "body": [
            {
                "pairFirst": f"{toid}@goofish",
                "pairSecond": f"{myid}@goofish",
                "bizType": "1",
                "extension": {"itemId": item_id},
                "ctx": {"appVersion": "1.0", "platform": "web"},
            }
        ],
    }


def parse_send_response(frame: dict[str, Any]) -> dict[str, Any]:
    """解析 sendByReceiverScope 回包：{"code": 200} 或 {"code": 500, "error": ...}。"""
    body = frame.get("body")
    if isinstance(body, list) and body:
        body = body[0]
    code = frame.get("code") or (body.get("code") if isinstance(body, dict) else None) or 500
    result: dict[str, Any] = {"code": code, "uuid": None}
    if isinstance(body, dict):
        result["uuid"] = body.get("uuid")
        if code != 200:
            result["error"] = str(
                body.get("message") or body.get("errorInfo") or body.get("error") or frame.get("message") or ""
            )
    return result
