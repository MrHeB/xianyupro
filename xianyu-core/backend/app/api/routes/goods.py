"""商品接口（品类 A 手动上架 + 品类 B 采集商品池审核上架，阶段 4-5 实现）。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbSession
from app.models import User
from app.users.deps import get_current_user
from app.users.scoping import ensure_account_scope, visible_account_ids

router = APIRouter(prefix="/goods", tags=["goods"])


@router.get("")
async def list_goods(
    db: DbSession,
    user: User = Depends(get_current_user),
    category: str | None = None,
    account_id: int | None = None,
) -> list[dict]:
    """商品列表，可按品类/账号过滤（A 虚拟 / B 1688 采集）。

    不带 account_id = 当前用户可见范围内全部账号混合，响应里带 account_id 供前端标归属。
    """
    from app.models import Goods

    stmt = select(Goods).order_by(Goods.id.desc())
    ids = await visible_account_ids(db, user)
    if ids is not None:
        stmt = stmt.where(Goods.account_id.in_(ids))
        if account_id is not None and account_id not in ids:
            return []
    if account_id is not None:
        stmt = stmt.where(Goods.account_id == account_id)
    if category in ("A", "B"):
        stmt = stmt.where(Goods.category == category)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": g.id,
            "account_id": g.account_id,
            "external_goods_id": g.external_goods_id,
            "category": g.category,
            "source": g.source,
            "title": g.title,
            "price_cent": g.price_cent,
            "status": g.status,
            "pool_status": g.pool_status,
        }
        for g in rows
    ]


class GoodsIn(BaseModel):
    """品类 A 手动登记：商品在闲鱼 App 手动上架后登记进系统（绑 external_goods_id，
    自动发货按它匹配）；品类 B：采集商品池入库。"""

    account_id: int
    title: str = Field(min_length=1, max_length=128)
    category: str = Field(default="A", pattern="^[AB]$")
    price_cent: int = Field(gt=0)
    description: str | None = None
    image_urls: list[str] | None = None
    external_goods_id: str | None = Field(default=None, max_length=64)
    source: str = Field(default="manual", pattern="^(manual|csv|1688)$")


@router.post("")
async def create_goods(body: GoodsIn, db: DbSession, user: User = Depends(get_current_user)) -> dict:
    from app.models import Account, Goods

    await ensure_account_scope(db, user, body.account_id)
    account = await db.get(Account, body.account_id)
    if account is None:
        raise HTTPException(404, f"账号不存在：{body.account_id}")

    if body.external_goods_id:
        exists = (
            await db.execute(
                select(Goods.id).where(Goods.external_goods_id == body.external_goods_id)
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(400, f"外部商品 ID 已登记：{body.external_goods_id}")

    goods = Goods(
        account_id=body.account_id,
        external_goods_id=body.external_goods_id,
        category=body.category,
        source=body.source,
        title=body.title,
        description=body.description,
        price_cent=body.price_cent,
        image_urls=body.image_urls,
        # 已有外部 ID = 已在闲鱼上架 → 在售；否则待上架
        status=1 if body.external_goods_id else 0,
        pool_status="PUSHED" if body.external_goods_id else None,
    )
    db.add(goods)
    await db.commit()
    return {"id": goods.id, "title": goods.title, "status": goods.status}


class CsvImportIn(BaseModel):
    """CSV 批量导入品类 B 商品池：粘贴文本，列 title,price(元),image_urls(分号分隔)。
    导入即转换（标题去噪/加价）入池 pool_status=MAPPED。"""

    account_id: int
    csv_text: str = Field(min_length=1, max_length=200_000)
    markup_rate: float = Field(default=1.3, ge=0.5, le=3.0)


@router.post("/import-csv")
async def import_csv(
    body: CsvImportIn, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    from app.collectors.csv_import import import_csv_to_pool
    from app.models import Account

    await ensure_account_scope(db, user, body.account_id)
    if await db.get(Account, body.account_id) is None:
        raise HTTPException(404, f"账号不存在：{body.account_id}")
    result = await import_csv_to_pool(db, body.account_id, body.csv_text, rate=body.markup_rate)
    return result


@router.post("/{goods_id}/publish")
async def publish_goods(
    goods_id: int, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """上架（阶段 4-5）：四步发布链路（传图→类目→地址→提交，写操作限流 1 次/分钟）。
    成功：external_goods_id 回填 + status=1 + pool_status=PUSHED + 发布快照落库。
    失败：pool_status=FAILED，detail 带可读原因；网络类/本地限流（请求未送达闲鱼）不改状态。"""
    from app.api_client.errors import ErrorKind, XianyuApiError
    from app.auth.session_pool import session_pool
    from app.core.failure_guard import WriteGuardPaused
    from app.listing.publisher import publish_goods as do_publish
    from app.models import Goods

    goods = await db.get(Goods, goods_id)
    if goods is None:
        raise HTTPException(404, f"商品不存在：{goods_id}")
    await ensure_account_scope(db, user, goods.account_id)
    if goods.external_goods_id and goods.status == 1:
        raise HTTPException(400, f"商品已在售：{goods.external_goods_id}")
    if goods.source == "1688" and goods.pool_status in ("SOURCED", "DECIDED"):
        # 半自动闸门（§8.1）：1688 件未经人工放行不能发布——标题/价格此时仍是采集打底，
        # 未过风险扫描。放行在「采集工作区」做，这里不给任何 force 余地。
        raise HTTPException(
            400,
            "该采集件尚未放行，不能上架：先到「采集工作区」生成定价与文案并批量放行"
            f"（当前 pool_status={goods.pool_status}）",
        )

    session = session_pool.get(goods.account_id)
    if session is None or not session.is_alive:
        raise HTTPException(409, "账号未登录（先扫码登录），无法上架")

    try:
        external_id = await do_publish(db, goods, session)
    except WriteGuardPaused as exc:
        # 止损熔断中：不改 pool_status（非数据问题），到期或重新扫码后自动恢复
        raise HTTPException(429, str(exc)) from exc
    except XianyuApiError as exc:
        if exc.kind == ErrorKind.LOCAL_THROTTLE:
            # 自己令牌桶没排到号（1 次/分钟），请求没出门 → 不改 pool_status，稍后重发即可
            raise HTTPException(429, str(exc)) from exc
        if exc.kind == ErrorKind.NETWORK:
            # 请求多半没送达闲鱼 → 不该把件打成 FAILED 掉出自动重试队列
            # （定时路径的 auto_listing 只扫 MAPPED；路由在这里置 FAILED 就只剩手动重发）
            raise HTTPException(502, str(exc)) from exc
        goods.pool_status = "FAILED"
        await db.commit()
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        # 本地校验失败（如无图片），不改 pool_status——修数据后可直接重试
        raise HTTPException(400, str(exc)) from exc
    return {
        "goods_id": goods.id,
        "external_goods_id": external_id,
        "status": "published",
        "pool_status": goods.pool_status,
    }


# ==== 1688 采集 ====

class Collect1688In(BaseModel):
    """1688 采集：按 offer_id 列表采集详情页（httpx 直连 + 货源账号 Cookie），入池 SOURCED。

    instruction = 用户自然语言原文（§6 R1 / R7 追溯）；带上它即视为会话/输入框触发，
    批次台账记 trigger=nl，便于把"人话指令"和后来采到的商品对得上。
    """

    account_id: int
    offer_ids: list[str] = Field(min_length=1, max_length=100)
    instruction: str | None = Field(default=None, max_length=500)
    alibaba_account_id: int | None = None


@router.post("/collect-1688")
async def collect_1688(
    body: Collect1688In, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    from app.collectors.alibaba import import_1688_by_offer_ids
    from app.models import Account, AlibabaAccount

    await ensure_account_scope(db, user, body.account_id)
    if await db.get(Account, body.account_id) is None:
        raise HTTPException(404, f"账号不存在：{body.account_id}")
    if body.alibaba_account_id is not None:
        src = await db.get(AlibabaAccount, body.alibaba_account_id)
        if src is None or src.deleted:
            raise HTTPException(404, f"1688 货源账号不存在：{body.alibaba_account_id}")
        if user.role != "admin" and src.user_id != user.id:
            raise HTTPException(403, "无权使用该 1688 货源账号")
    result = await import_1688_by_offer_ids(
        db,
        body.account_id,
        body.offer_ids,
        trigger="nl" if body.instruction else "manual",
        instruction=body.instruction,
        alibaba_account_id=body.alibaba_account_id,
    )
    return result


# ==== 人工放行（§8.3：DECIDED → MAPPED，之后 auto_listing / 行内上架才可发出）====

class BatchApproveIn(BaseModel):
    """批量放行：勾选 goods_id 列表，单次上限 5 件；任一件不合格整批拒绝。"""

    goods_ids: list[int] = Field(min_length=1, max_length=5)
    confirm: bool = False


@router.post("/batch-approve")
async def batch_approve(
    body: BatchApproveIn, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    from app.collectors.pool_flow import ApprovalError, approve_offers
    from app.models import Goods

    if not body.confirm:
        # 未经确认禁止放行（§6 步骤⑥ / §8.3 第 1 条）；不提供 force/skip 之类的绕闸门参数
        raise HTTPException(400, "放行需显式确认（confirm=true）")
    rows = (await db.execute(select(Goods).where(Goods.id.in_(body.goods_ids)))).scalars().all()
    if len(rows) != len(body.goods_ids):
        raise HTTPException(404, "存在不存在的 goods_id，整批未放行")
    for g in rows:
        await ensure_account_scope(db, user, g.account_id)
    try:
        return await approve_offers(db, goods_ids=body.goods_ids, confirmed_by=user.username)
    except ApprovalError as exc:
        raise HTTPException(400, str(exc)) from exc


class CollectEditIn(BaseModel):
    """人工改写采集件（缺口⑨）：三项都可选，但至少要改一项。价格单位是分。"""

    title: str | None = Field(default=None, max_length=128)
    desc: str | None = Field(default=None, max_length=4000)
    price_cent: int | None = Field(default=None, ge=1)


@router.patch("/{goods_id}")
async def edit_collected(
    goods_id: int, body: CollectEditIn, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """人工改写 1688 采集件的标题/描述/售价，写完立刻重扫风险并回传结论。

    这是 `pricing=manual` 与"改写不掉的拦截项"唯一的人工出路；改动落在工作区的
    decision 段上（放行的读源），所以 `read_product`/待确认清单会同步看到新值。
    """
    from app.collectors.pool_flow import ApprovalError, edit_offer

    if body.title is None and body.desc is None and body.price_cent is None:
        raise HTTPException(400, "至少要改一项（title / desc / price_cent）")
    try:
        return await edit_offer(
            db, goods_id=goods_id, title=body.title, desc=body.desc,
            price_cent=body.price_cent, editor=user.username,
        )
    except ApprovalError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"工作区里没有这件的 product.json：{exc}") from exc
