"""推送包解码与消息解析测试（用 msgpack 现场构造报文，离线验证解码链路）。"""

import base64
import json

import msgpack

from app.messaging.decoder import (
    decode_push_payloads,
    extract_username_from_reminder,
    is_synthetic_peer_name,
    parse_message,
)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


# ==================== 第一层：decode_push_payloads ====================


def test_decode_base64_json():
    payload = {"sessionId": "123", "content": "hi"}
    data = _b64(json.dumps(payload).encode())
    assert decode_push_payloads(data) == [payload]


def test_decode_msgpack_single_object():
    payload = {"1": "2201@goofish", "2": 1, "3": "msgid-1"}
    data = _b64(msgpack.packb(payload, use_bin_type=True))
    decoded = decode_push_payloads(data)
    assert payload in decoded


def test_decode_msgpack_consecutive_objects():
    """实测一帧常含多个拼接对象（32 条/帧很常见）。"""
    objs = [{"sessionId": str(i), "content": f"m{i}"} for i in range(5)]
    data = _b64(b"".join(msgpack.packb(o, use_bin_type=True) for o in objs))
    decoded = decode_push_payloads(data)
    for o in objs:
        assert o in decoded


def test_decode_nested_collection():
    """msgpack 解出的对象里嵌着 JSON 字符串，也要能收集。"""
    inner = {"text": "hello"}
    wrapper = {"1": json.dumps(inner)}
    data = _b64(msgpack.packb(wrapper, use_bin_type=True))
    decoded = decode_push_payloads(data)
    flat = json.dumps(inner, ensure_ascii=False)
    assert flat in {json.dumps(d, ensure_ascii=False) for d in decoded}


def test_decode_garbage_returns_empty():
    assert decode_push_payloads("") == []
    assert decode_push_payloads("!!!not-base64!!!") == []


# ==================== 第二层：parse_message ====================


def test_parse_numbered_text_message():
    """标准结构：data["1"] = msg_info（sender/sId/pnmId/time/content/reminder）。"""
    inner_content = {"contentType": 1, "text": {"text": "多少钱？"}}
    msg_info = {
        "1": {"1": "buyer001", "2": "买家小明"},           # senderInfo
        "2": "47812870000",                                 # sId
        "3": "f36902eff45445bdb2d1b0fd168f8264",           # pnmId
        "5": 1757200000000,                                # messageTime (ms)
        "6": {"3": {"1": base64.b64encode(json.dumps(inner_content).encode()).decode()}},
        "10": {"reminderContent": "买家小明 向你发送了一条新消息"},
    }
    parsed = parse_message({"1": msg_info})
    assert parsed is not None
    assert parsed["sId"] == "47812870000"
    assert parsed["pnmId"] == "f36902eff45445bdb2d1b0fd168f8264"
    assert parsed["senderUserId"] == "buyer001"
    assert parsed["msgContent"] == "多少钱？"
    assert parsed["contentType"] == 1
    assert parsed["messageTime"] == 1757200000000
    assert parsed["direction"] == "IN"


def test_parse_card_update_payment():
    """卡片更新消息（付款通知）：data["1"] str + data["4"] reminderContent。"""
    data = {
        "1": "479833890096@goofish",
        "2": "479833890096@goofish",
        "3": "cc8bc2df7c934df086e0567cb69f1573",
        "4": {
            "reminderContent": "我已付款，等待你发货",
            "reminderTitle": "买家已付款",
            "senderUserId": "3149637063",
            "reminderUrl": "fleamarket://message_chat?itemId=900052644277&peerUserId=3149637063",
        },
        "5": 1757200000000,
    }
    parsed = parse_message(data)
    assert parsed is not None
    assert parsed["contentType"] == 26
    assert parsed["msgContent"] == "我已付款，等待你发货"
    assert parsed["xyGoodsId"] == "900052644277"
    assert parsed["senderUserId"] == "3149637063"
    assert parsed["sId"] == "479833890096"


def test_parse_control_packet_returns_none():
    data = {"1": [{"1": "a", "2": 1}, {"1": "b", "3": 2}]}
    assert parse_message(data) is None


def test_parse_chat_script_filtered():
    """商品卡片触发的"话术脚本"不是真实买家消息，必须过滤。"""
    data = {
        "topic_title": "shading_opening",
        "session_id": "123",
        "content": "怎么登录？",
    }
    assert parse_message(data) is None


def test_parse_light_content():
    """轻量内容消息：{"1": int, "3": {"2": 文本, "4": contentType}}。"""
    data = {
        "1": 101,
        "3": {"1": "", "2": "还在吗", "3": "", "4": 1, "5": ""},
    }
    parsed = parse_message(data)
    assert parsed is not None
    assert parsed["msgContent"] == "还在吗"
    assert parsed["contentType"] == 1


def test_parse_session_info():
    data = {
        "sessionInfo": {
            "sessionId": "999",
            "extensions": {"extUserId": "buyer9", "itemId": "777", "itemTitle": "测试商品"},
        }
    }
    parsed = parse_message(data)
    assert parsed is not None
    assert parsed["contentType"] == 8
    assert parsed["xyGoodsId"] == "777"
    assert parsed["msgContent"] == "测试商品"


def test_parse_unrecognized_returns_none():
    assert parse_message({"foo": "bar"}) is None


# ==================== 昵称提取 ====================


def test_extract_username_from_reminder():
    assert extract_username_from_reminder("买家小明 向你发送了一条新消息") == "买家小明"
    assert extract_username_from_reminder("小明：在吗") == "小明"
    assert extract_username_from_reminder("今天天气不错") == ""


def test_is_synthetic_peer_name():
    assert is_synthetic_peer_name("我已付款，等待你发货")
    assert is_synthetic_peer_name("")
    assert not is_synthetic_peer_name("买家小明")
