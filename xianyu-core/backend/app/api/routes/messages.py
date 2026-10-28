"""消息查看接口（第一阶段只读）。"""

from fastapi import APIRouter, Depends

from app.api.deps import DbSession
from app.models import User
from app.users.deps import get_current_user
from app.users.scoping import visible_account_ids

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("/conversations")
async def list_conversations(
    db: DbSession,
    user: User = Depends(get_current_user),
    account_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """会话列表（按最新消息倒序）：前端消息页按账号切换、按买家下钻用。"""
    from sqlalchemy import and_, func, select

    from app.models import ChatMessage, Conversation

    ids = await visible_account_ids(db, user)
    stmt = select(Conversation)
    if ids is not None:
        stmt = stmt.where(Conversation.account_id.in_(ids))
    if account_id is not None:
        stmt = stmt.where(Conversation.account_id == account_id)
    conversations = (await db.execute(stmt)).scalars().all()
    if not conversations:
        return []

    conv_ids = [c.id for c in conversations]
    latest = (
        select(
            ChatMessage.conversation_id,
            func.max(ChatMessage.message_time).label("last_time"),
            func.count(ChatMessage.id).label("msg_count"),
        )
        .where(ChatMessage.conversation_id.in_(conv_ids))
        .group_by(ChatMessage.conversation_id)
        .subquery()
    )
    preview_rows = (
        await db.execute(
            select(
                ChatMessage.conversation_id,
                ChatMessage.direction,
                ChatMessage.content,
            ).join(
                latest,
                and_(
                    ChatMessage.conversation_id == latest.c.conversation_id,
                    ChatMessage.message_time == latest.c.last_time,
                )
            )
        )
    ).all()
    # 同一时间戳多条时取先读到的一条，仅做预览文案，不做严格语义
    preview = {cid: (direction, content) for cid, direction, content in preview_rows}
    stats = {row.conversation_id: row for row in (
        await db.execute(
            select(latest.c.conversation_id, latest.c.last_time, latest.c.msg_count)
        )
    ).all()}

    items = []
    for c in conversations:
        row = stats.get(c.id)
        direction, content = preview.get(c.id, ("", ""))
        items.append(
            {
                "id": c.id,
                "account_id": c.account_id,
                "peer_id": c.peer_id,
                "peer_name": c.peer_name,
                "auto_reply_paused": bool(c.auto_reply_paused or c.manual_disabled),
                "last_message_time": row.last_time if row else None,
                "message_count": row.msg_count if row else 0,
                "last_direction": direction,
                "last_content": (content or "")[:60],
            }
        )
    items.sort(key=lambda x: x["last_message_time"] or 0, reverse=True)
    return items[: min(limit, 500)]


@router.get("")
async def list_messages(
    db: DbSession,
    user: User = Depends(get_current_user),
    conversation_id: int | None = None,
    account_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """消息列表（最近优先）。"""
    from sqlalchemy import select

    from app.models import ChatMessage

    ids = await visible_account_ids(db, user)
    stmt = select(ChatMessage)
    if ids is not None:
        stmt = stmt.where(ChatMessage.account_id.in_(ids))
    if account_id is not None:
        stmt = stmt.where(ChatMessage.account_id == account_id)
    if conversation_id is not None:
        stmt = stmt.where(ChatMessage.conversation_id == conversation_id)
    stmt = stmt.order_by(ChatMessage.message_time.desc()).limit(min(limit, 200))
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": m.id,
            "account_id": m.account_id,
            "conversation_id": m.conversation_id,
            "content_type": m.content_type,
            "direction": m.direction,
            "content": m.content,
            "is_auto_reply": bool(m.is_auto_reply),
            "message_time": m.message_time,
        }
        for m in rows
    ]
