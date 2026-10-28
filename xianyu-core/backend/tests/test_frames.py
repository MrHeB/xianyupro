"""LWP 帧构造测试（纯函数）。"""

import base64
import json

from app.messaging import frames


def test_reg_frame_structure():
    frame = frames.build_reg_frame("token-abc", "device-123")
    assert frame["lwp"] == "/reg"
    h = frame["headers"]
    assert h["app-key"] == frames.IM_APP_KEY
    assert h["token"] == "token-abc"
    assert h["did"] == "device-123"
    assert h["dt"] == "j"
    assert h["wv"] == "im:3,au:3,sy:6"
    assert h["mid"]  # 必须有 mid


def test_ack_diff_pts_microsecond():
    frame = frames.build_ack_diff()
    body = frame["body"][0]
    assert frame["lwp"] == "/r/SyncStatus/ackDiff"
    assert body["pipeline"] == "sync"
    # 默认 pts = 当前毫秒 * 1000（微秒游标）
    assert body["pts"] > 1_000_000_000_000_000
    frame0 = frames.build_ack_diff(pts=0)
    assert frame0["body"][0]["pts"] > 1_000_000_000_000_000  # 0 也回退为当前时间

    frame7 = frames.build_ack_diff(pts=7)
    assert frame7["body"][0]["pts"] == 7


def test_ack_frame_passthrough():
    msg = {"lwp": "/s/vulcan", "headers": {"mid": "m1", "sid": "s1", "ua": "x", "app-key": "k"}}
    ack = frames.build_ack_frame(msg)
    assert ack["code"] == 200
    assert ack["headers"]["mid"] == "m1"
    assert ack["headers"]["sid"] == "s1"
    assert ack["headers"]["ua"] == "x"
    assert ack["headers"]["app-key"] == "k"

    # 无 headers 的包也能 ACK（mid 自动生成）
    ack2 = frames.build_ack_frame({})
    assert ack2["code"] == 200
    assert ack2["headers"]["mid"]


def test_send_text_frame_contains_self_in_receivers():
    """actualReceivers 必须包含发送者自己，否则 code=500（protocol.md §7.5）。"""
    frame = frames.build_send_text_frame("myid", "cid1", "toid", "在的")
    assert frame["lwp"] == "/r/MessageSend/sendByReceiverScope"
    msg_body, scope = frame["body"]
    assert scope["actualReceivers"] == ["toid@goofish", "myid@goofish"]
    assert msg_body["cid"] == "cid1@goofish"
    assert msg_body["content"]["contentType"] == 101

    custom = msg_body["content"]["custom"]
    assert custom["type"] == 1
    payload = json.loads(base64.b64decode(custom["data"]))
    assert payload == {"contentType": 1, "text": {"text": "在的"}}


def test_send_image_frame():
    frame = frames.build_send_image_frame("myid", "cid1", "toid", "https://img.example/1.jpg")
    custom = frame["body"][0]["content"]["custom"]
    assert custom["type"] == 2
    payload = json.loads(base64.b64decode(custom["data"]))
    assert payload["contentType"] == 2
    assert payload["image"]["pics"][0]["url"] == "https://img.example/1.jpg"


def test_create_chat_frame():
    frame = frames.build_create_chat_frame("myid", "toid", "item1")
    body = frame["body"][0]
    assert frame["lwp"] == "/r/SingleChatConversation/create"
    assert body["pairFirst"] == "toid@goofish"
    assert body["pairSecond"] == "myid@goofish"
    assert body["extension"]["itemId"] == "item1"


def test_parse_send_response():
    ok = frames.parse_send_response({"code": 200, "body": [{"uuid": "u1"}]})
    assert ok == {"code": 200, "uuid": "u1"}

    fail = frames.parse_send_response({"code": 500, "body": [{"message": "xxx"}]})
    assert fail["code"] == 500
    assert fail["error"] == "xxx"

    legacy = frames.parse_send_response({"body": {"code": 200, "uuid": "u2"}})
    assert legacy["code"] == 200
    assert legacy["uuid"] == "u2"


def test_mid_format():
    mid = frames.generate_mid()
    assert mid.endswith(" 0")
    assert int(mid.split(" ")[0]) > 1_700_000_000_000  # 毫秒时间戳开头


def test_device_id_format():
    did = frames.build_device_id("12345")
    prefix, _, unb = did.rpartition("-")
    assert unb == "12345"
    assert len(prefix) == 32  # UUID hex
