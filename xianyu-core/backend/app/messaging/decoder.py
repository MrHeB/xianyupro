"""闲鱼 IM 推送包解码与消息解析（protocol.md §7.6，蓝本：pro ws_protocol.py）。

两层职责：
1. `decode_push_payloads`：syncPushPackage.data[i].data 的三层解码——
   明文 JSON → base64(JSON) → base64(MessagePack，可能是连续多个对象拼接)；
2. `parse_message`：把解码后的 dict（私有数字索引字段 or 新版命名结构）规范化为
   统一消息字段（sId/pnmId/senderUserId/msgContent/contentType/...）。

均为纯函数，离线可用录制的报文做单测。
"""

import base64
import json
import logging
import re
from typing import Any

import msgpack

logger = logging.getLogger(__name__)


# ==================== 第一层：字节流 → dict 列表 ====================


def _normalize_key(value: Any) -> str:
    """msgpack 的 key（可能是 bytes）转稳定字符串。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _normalize_value(value: Any) -> Any:
    """递归把 msgpack 解出的 bytes/容器转成可 JSON 化对象。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {_normalize_key(k): _normalize_value(v) for k, v in value.items()}
    return value


def decode_push_payloads(encrypted: str) -> list[dict[str, Any]]:
    """把一条 push data 解码为 dict 列表（一帧可能含多个对象）。

    策略序：base64(JSON) → msgpack 流式解包（连续对象）→ msgpack 单对象。
    全部失败返回 []（调用方跳过该条）。
    """
    if not encrypted:
        return []
    cleaned = encrypted
    if missing := len(cleaned) % 4:
        cleaned += "=" * (4 - missing)
    try:
        decoded = base64.b64decode(cleaned)
    except Exception:
        logger.warning("push data base64 解码失败 len=%d", len(encrypted))
        return []

    # 策略 0：base64(JSON)
    try:
        text = decoded.decode("utf-8").strip()
        if text.startswith(("{", "[")):
            return _collect_dicts(json.loads(text))
    except Exception:
        pass

    # 策略 1：msgpack 流式（连续拼接对象，实测 32 条/帧很常见）
    for kwargs in (
        {"raw": True},
        {"raw": False},
    ):
        try:
            unpacker = msgpack.Unpacker(strict_map_key=False, **kwargs)
            unpacker.feed(decoded)
            out: list[dict[str, Any]] = []
            for obj in unpacker:
                out.extend(_collect_dicts(obj))
            if out:
                return out
        except Exception:
            continue

    # 策略 2：单对象
    try:
        return _collect_dicts(msgpack.unpackb(decoded, raw=False, strict_map_key=False))
    except Exception:
        logger.warning("push data 全部解码策略失败 len=%d", len(encrypted))
        return []


def _collect_dicts(value: Any) -> list[dict[str, Any]]:
    """递归收集容器里所有 dict（含嵌套 JSON 字符串再解析）。"""
    normalized = _normalize_value(value) if not isinstance(value, dict) else {
        _normalize_key(k): _normalize_value(v) for k, v in value.items()
    }
    out: list[dict[str, Any]] = []
    if isinstance(normalized, dict):
        out.append(normalized)
        for child in normalized.values():
            out.extend(_collect_dicts(child))
    elif isinstance(normalized, list):
        for item in normalized:
            out.extend(_collect_dicts(item))
    elif isinstance(normalized, str):
        text = normalized.strip()
        if text.startswith(("{", "[")):
            try:
                out.extend(_collect_dicts(json.loads(text)))
            except Exception:
                pass
    return out


# ==================== 第二层：dict → 规范化消息 ====================

# 系统通知文本（reminderTitle 里出现 = 非真实用户名，别当昵称入库）
SYSTEM_PEER_KEYWORDS = (
    "交易消息", "工作台通知", "系统通知", "通知消息",
    "买家已拍下，待付款", "我已拍下，待付款",
    "我已付款，等待你发货", "买家确认收货，交易成功",
    "快给ta一个评价吧", "记得及时发货", "等待你发货", "订单提醒",
)


def extract_username_from_reminder(reminder_content: str) -> str:
    """从 "用户名 向你发送了一条新消息" / "用户名 ：内容" 里提取昵称。"""
    if not reminder_content:
        return ""
    content = reminder_content.strip()
    for pattern in (" 向你发送了一条新消息", " 向你发送了一条消息"):
        if (idx := content.find(pattern)) > 0:
            return content[:idx]
    for sep in (" ：", "："):
        idx = content.find(sep)
        if 0 < idx < 20:  # 限制位置，避免匹配到消息正文里的冒号
            return content[:idx]
    return ""


def is_synthetic_peer_name(name: str) -> bool:
    """名称是否为系统通知文本（非真实用户名）。"""
    if not name or not name.strip():
        return True
    return any(kw in name for kw in SYSTEM_PEER_KEYWORDS)


def parse_message(data: dict[str, Any]) -> dict[str, Any] | None:
    """把解码后的 dict 规范化为统一消息字段；识别不了返回 None。

    兼容五种真实结构（蓝本 pro parse_numbered_fields，取其实测有效路径）：
    a) 卡片更新消息（付款通知）：data["1"] str + data["4"] dict(reminderContent)；
    b) 简单话题消息：topic_title + session_id + content（过滤"话术脚本"）；
    c) 轻量内容消息：data["1"] int + data["3"] 内容容器；
    d) sessionInfo 会话激活包（contentType=8，取 itemId/itemTitle）；
    e) 标准数字索引结构：msg_info["1"=sender, "2"=sId, "3"=pnmId, "5"=time, "6"=content, "10"=reminder]。
    """
    if not isinstance(data, dict):
        return None

    # 控制包/成员状态包：纯数字键的列表结构
    first = data.get("1")
    if (
        isinstance(first, list)
        and first
        and all(isinstance(item, dict) for item in first)
        and all(set(map(str, item.keys())).issubset({"1", "2", "3", "4"}) for item in first)
        and not any(k in data for k in ("sessionInfo", "content", "2", "3", "5", "6", "10", "msgType"))
    ):
        return None

    # a) 卡片更新消息（付款通知 → 品类 A 自动发货核心信号）
    if isinstance(data.get("1"), str) and isinstance(data.get("4"), dict) and "reminderContent" in data["4"]:
        return _parse_card_update(data)

    # b) 简单话题消息（含话术脚本过滤）
    topic_title = data.get("topic_title")
    session_id = data.get("session_id") or data.get("sessionId")
    if isinstance(topic_title, str) and session_id and isinstance(data.get("content"), str):
        return _parse_simple_topic(data, topic_title, session_id)

    # c) 轻量内容消息：{"1": int, "3": {"1":sId, "2":内容, "3":pnmId, "4":contentType, "5":extJSON}}
    if isinstance(data.get("1"), int) and not isinstance(data.get("1"), bool) and isinstance(data.get("3"), dict):
        parsed = _parse_light_content(data)
        if parsed:
            return parsed

    # d) sessionInfo 会话激活包
    if isinstance(data.get("sessionInfo"), dict):
        return _parse_session_info(data)

    # e) 标准数字索引结构
    return _parse_numbered(data)


def _parse_card_update(data: dict[str, Any]) -> dict[str, Any] | None:
    """卡片更新消息（付款状态变更，"我已付款，等待你发货"）。"""
    m4 = data["4"]
    reminder_content = str(m4.get("reminderContent") or "")
    reminder_url = str(m4.get("reminderUrl") or "")
    reminder_title = str(m4.get("reminderTitle") or "")
    sender_user_id = str(m4.get("senderUserId") or m4.get("sendUserId") or "")
    chat_id = str(data.get("2") or "")
    s_id = chat_id.split("@")[0] if "@" in chat_id else chat_id

    try:
        msg_time = int(data.get("5") or 0)
    except (TypeError, ValueError):
        msg_time = 0

    # 商品 ID：优先 reminderUrl 的 itemId=，兜底 extJson
    xy_goods_id = ""
    if reminder_url and (m := re.search(r"[?&]itemId=(\d+)", reminder_url) or re.search(r"[?&]id=(\d+)", reminder_url)):
        xy_goods_id = m.group(1)
    if not xy_goods_id:
        ext_json = m4.get("extJson") or ""
        if isinstance(ext_json, str) and ext_json.startswith("{"):
            try:
                ext = json.loads(ext_json)
                if isinstance(ext, dict) and ext.get("itemId"):
                    xy_goods_id = str(ext["itemId"])
            except (json.JSONDecodeError, TypeError):
                pass

    if not any((s_id, sender_user_id, reminder_content, xy_goods_id)):
        return None
    return {
        "sId": s_id,
        "pnmId": str(data.get("3") or data.get("messageId") or ""),
        "senderUserId": sender_user_id,
        "senderUserName": reminder_title,
        "messageTime": msg_time,
        "msgContent": reminder_content,
        "contentType": 26,  # 卡片更新按付款消息处理，配合 payment.is_payment_message 判定
        "direction": "IN",
        "reminderContent": reminder_content,
        "reminderUrl": reminder_url,
        "receiverUserId": "",
        "xyGoodsId": xy_goods_id,
        "imageUrls": [],
    }


def _parse_simple_topic(data: dict[str, Any], topic_title: str, session_id: Any) -> dict[str, Any] | None:
    """简单话题消息。注意过滤"话术脚本"（商品卡片触发的预设话术，非真实买家消息）。"""
    is_chat_script = (
        not data.get("senderUserId")
        and not data.get("messageId")
        and not data.get("pnmId")
        and not data.get("messageTime")
        and not data.get("createTime")
        and bool(topic_title)
    )
    if is_chat_script:
        return None

    receiver_user_id = ""
    if isinstance(data.get("receiverIds"), list) and data["receiverIds"]:
        receiver_user_id = str(data["receiverIds"][0] or "")
    session_info = data.get("sessionInfo") if isinstance(data.get("sessionInfo"), dict) else None
    extensions = (session_info or {}).get("extensions") or {}
    sender_user_id = str(extensions.get("extUserId") or "")
    if not receiver_user_id:
        receiver_user_id = str(extensions.get("ownerUserId") or "")
    return {
        "sId": str(session_id),
        "pnmId": str(data.get("messageId") or data.get("pnmId") or ""),
        "senderUserId": sender_user_id,
        "senderUserName": "",
        "messageTime": 0,
        "msgContent": str(data.get("content") or ""),
        "contentType": 1,
        "direction": "IN",
        "reminderContent": topic_title,
        "reminderUrl": "",
        "receiverUserId": receiver_user_id,
        "xyGoodsId": "",
        "imageUrls": [],
    }


def _parse_light_content(data: dict[str, Any]) -> dict[str, Any] | None:
    """轻量内容消息：{"1": 子类型, "3": {"2": 文本, "4": contentType, "5": extJSON}}。"""
    container = data["3"]
    raw_content = str(container.get("2") or "")
    try:
        content_type = int(container.get("4")) if container.get("4") is not None else 1
    except (TypeError, ValueError):
        content_type = 1

    # extJSON 里可能有 text.text（真实正文）
    msg_content = ""
    ext_json_str = str(container.get("5") or "")
    if ext_json_str.startswith("{"):
        try:
            ext = json.loads(ext_json_str)
            text_obj = ext.get("text") if isinstance(ext.get("text"), dict) else None
            if text_obj and isinstance(text_obj.get("text"), str):
                msg_content = text_obj["text"].strip()
        except (json.JSONDecodeError, TypeError):
            pass

    final_content = msg_content or raw_content
    if not final_content:
        return None
    return {
        "sId": str(container.get("1") or "").strip(),
        "pnmId": str(container.get("3") or "").strip(),
        "senderUserId": "",
        "senderUserName": "",
        "messageTime": 0,
        "msgContent": final_content,
        "contentType": content_type,
        "direction": "IN",
        "reminderContent": final_content if content_type == 1 else "",
        "reminderUrl": "",
        "receiverUserId": "",
        "xyGoodsId": "",
        "imageUrls": [],
    }


def _parse_session_info(data: dict[str, Any]) -> dict[str, Any] | None:
    """会话激活包（contentType=8）：只有会话元信息，没有正文。"""
    session_info = data["sessionInfo"]
    extensions = session_info.get("extensions") if isinstance(session_info.get("extensions"), dict) else {}
    session_id = session_info.get("sessionId") or data.get("sessionId") or ""
    create_time = session_info.get("createTime") or extensions.get("VULCAN_CREATE_TIME") or 0
    sender_user_id = str(extensions.get("extUserId") or "")
    receiver_user_id = ""
    if isinstance(data.get("receiverIds"), list) and data["receiverIds"]:
        receiver_user_id = str(data["receiverIds"][0] or "")
    if not receiver_user_id:
        receiver_user_id = str(extensions.get("ownerUserId") or "")
    item_id = str(extensions.get("itemId") or "")
    item_title = str(extensions.get("itemTitle") or "")
    if not any((session_id, sender_user_id, receiver_user_id, item_id, item_title)):
        return None
    return {
        "sId": str(session_id),
        "pnmId": str(data.get("messageId") or data.get("pnmId") or ""),
        "senderUserId": sender_user_id,
        "senderUserName": "",
        "messageTime": int(create_time) if str(create_time).isdigit() else 0,
        "msgContent": item_title,
        "contentType": 8,
        "direction": "IN",
        "reminderContent": item_title,
        "reminderUrl": "",
        "receiverUserId": receiver_user_id,
        "xyGoodsId": item_id,
        "imageUrls": [],
    }


def _get(obj: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(obj, dict):
        return default
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return default


def _is_valid_message_info(obj: dict) -> bool:
    """标准 message_info 模式："2" 是纯数字会话 ID + "5" 是毫秒时间戳。

    防止把轻量容器的 {"2": 消息正文} 误认成 message_info。
    """
    if "2" not in obj or "5" not in obj:
        return False
    sid = obj["2"]
    if isinstance(sid, str) and re.search(r"[一-鿿]", sid):
        return False
    try:
        int(obj["5"])
        return True
    except (TypeError, ValueError):
        return False


def _find_message_info(obj: Any) -> dict:
    """递归找 message_info（"1" 子字典优先，其次自身，最后深搜）。"""
    if not isinstance(obj, dict):
        return {}
    first = _get(obj, "1", default={})
    if isinstance(first, dict) and ("6" in first or _is_valid_message_info(first)):
        return first
    if "6" in obj or _is_valid_message_info(obj):
        return obj
    for value in obj.values():
        found = _find_message_info(value)
        if found:
            return found
    return {}


def _parse_numbered(data: dict[str, Any]) -> dict[str, Any] | None:
    """标准数字索引结构（蓝本 pro 主路径，做了删减）。"""
    # 已读回执：data["2"] == 2，暂不处理
    if data.get("2") == 2:
        return None

    wrapper = data.get("1")
    if isinstance(wrapper, dict) and wrapper.get("3"):
        msg_info = wrapper
    else:
        msg_info = _find_message_info(data)
    if not msg_info:
        return None

    sender_info = _get(msg_info, "1", "senderInfo", default={})
    sender_user_id = ""
    sender_user_name = ""
    if isinstance(sender_info, dict):
        sender_user_id = str(_get(sender_info, "1", "senderUserId", default="") or "")
        sender_user_name = str(_get(sender_info, "2", "senderUserName", "nick", default="") or "")
    elif sender_info:
        sender_user_id = str(sender_info)
    if not sender_user_id:
        sender_user_id = str(_get(data, "senderUserId", "senderId", default="") or "")
    if not sender_user_name:
        sender_user_name = str(_get(data, "senderUserName", "senderNick", default="") or "")

    s_id = str(_get(msg_info, "2", "sId", default="") or "")
    pnm_id = str(_get(msg_info, "3", "pnmId", default="") or "")
    message_time = _get(msg_info, "5", "messageTime", default=0) or 0
    if not message_time:
        for time_source in (
            _get(msg_info, "createAt", "time", "modifyTime", default=0),
            _get(data, "createAt", "time", "modifyTime", default=0),
        ):
            if time_source:
                try:
                    if int(time_source) > 0:
                        message_time = int(time_source)
                        break
                except (TypeError, ValueError):
                    continue
    try:
        message_time = int(message_time)
    except (TypeError, ValueError):
        message_time = 0

    # 正文：msg_info["6"] = {"3": {"1": base64(JSON), "5": text}} 或直接 text
    content_obj = _get(msg_info, "6", "content", default={})
    msg_content = ""
    content_type = 1
    image_urls: list[str] = []
    if isinstance(content_obj, dict):
        inner = _get(content_obj, "3", "content", default={})
        if isinstance(inner, dict):
            for source in (_get(inner, "1", "data", default=""), _get(inner, "5", "text", "content", default="")):
                parsed = _decode_embedded(source)
                ctype, text, urls = _extract_content(parsed)
                if urls and not image_urls:
                    image_urls = urls
                    content_type = 2
                if text and not msg_content:
                    msg_content = text
                    if ctype not in (None, ""):
                        content_type = int(ctype)
                if msg_content and image_urls:
                    break
        if not msg_content:
            direct_text = _get(content_obj, "text", default="")
            if direct_text:
                msg_content = str(direct_text)[:500]
    elif isinstance(content_obj, str) and content_obj:
        msg_content = content_obj[:500]
    if not msg_content and image_urls:
        msg_content = image_urls[0]
    if not msg_content:
        top_content = _get(data, "content", default="")
        if isinstance(top_content, str) and top_content:
            msg_content = top_content[:500]

    # reminder（msg_info["10"]）：提醒正文/URL/昵称兜底
    reminder = _get(msg_info, "10", "reminder", default={})
    reminder_content = ""
    reminder_url = ""
    reminder_title = ""
    if isinstance(reminder, dict):
        reminder_content = str(_get(reminder, "reminderContent", "content", default="") or "")
        reminder_url = str(_get(reminder, "reminderUrl", "url", default="") or "")
        reminder_title = str(_get(reminder, "reminderTitle", "title", default="") or "")
        if not sender_user_id:
            sender_user_id = str(_get(reminder, "senderUserId", "sender", default="") or "")

    # 昵称兜底：reminderContent 的 "XXX 向你发送了一条新消息" 模式
    if (not sender_user_name or not sender_user_name.strip()) and reminder_content:
        sender_user_name = extract_username_from_reminder(reminder_content)
    if (not sender_user_name or not sender_user_name.strip()) and reminder_title:
        sender_user_name = reminder_title

    # 商品 ID：reminderUrl 的 itemId= 优先
    xy_goods_id = ""
    if reminder_url and (m := re.search(r"[?&]itemId=(\d+)", reminder_url) or re.search(r"[?&]id=(\d+)", reminder_url)):
        xy_goods_id = m.group(1)

    if not any((s_id, pnm_id, sender_user_id, msg_content, reminder_content)):
        return None
    return {
        "sId": s_id,
        "pnmId": pnm_id,
        "senderUserId": sender_user_id,
        "senderUserName": sender_user_name,
        "messageTime": message_time,
        "msgContent": msg_content,
        "contentType": content_type,
        "direction": "IN",
        "reminderContent": reminder_content,
        "reminderUrl": reminder_url,
        "receiverUserId": "",
        "xyGoodsId": xy_goods_id,
        "imageUrls": image_urls,
    }


def _decode_embedded(payload: Any) -> Any:
    """内容字段可能是：dict / 明文 JSON / base64(JSON)。"""
    if isinstance(payload, dict) or not isinstance(payload, str):
        return payload
    text = payload.strip()
    if not text:
        return ""
    if text.startswith(("{", "[")):
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
    cleaned = text
    if missing := len(cleaned) % 4:
        cleaned += "=" * (4 - missing)
    try:
        decoded = base64.b64decode(cleaned, validate=True).decode("utf-8").strip()
    except Exception:
        return text
    if decoded.startswith(("{", "[")):
        try:
            return json.loads(decoded)
        except (json.JSONDecodeError, TypeError):
            return decoded
    return decoded


def _extract_content(parsed: Any) -> tuple[Any, str, list[str]]:
    """从解出的内容对象提取 (contentType, 文本, 图片URL列表)。"""
    if isinstance(parsed, str):
        return 1, parsed[:500], []
    if not isinstance(parsed, dict):
        return 1, "", []
    parsed_type = parsed.get("contentType", 1)
    text_obj = parsed.get("text", {})
    parsed_text = text_obj.get("text", "") if isinstance(text_obj, dict) else str(text_obj or "")
    if parsed_text:
        return parsed_type, parsed_text, []
    if int(parsed_type or 1) == 2:
        urls = _extract_image_urls(parsed)
        if urls:
            return 2, urls[0], urls
    return parsed_type, "", []


def _extract_image_urls(payload: dict[str, Any]) -> list[str]:
    image_obj = payload.get("image") if isinstance(payload.get("image"), dict) else {}
    urls: list[str] = []
    for candidate in (image_obj.get("url"), image_obj.get("picUrl"), payload.get("imageUrl"), payload.get("url")):
        if isinstance(candidate, str) and candidate.strip():
            urls.append(candidate.strip())
    for pic in image_obj.get("pics") or []:
        if isinstance(pic, dict):
            for candidate in (pic.get("url"), pic.get("picUrl")):
                if isinstance(candidate, str) and candidate.strip():
                    urls.append(candidate.strip())
    return list(dict.fromkeys(urls))
