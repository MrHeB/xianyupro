"""1688 采集执行器（httpx 直连详情页 + parser 解析，入商品池 SOURCED）。

按 offer_id 逐个采集（搜索列表页要 1688 登录态，见 auth/alibaba_login.py）。
详情页/搜索页在无登录态时会返回 x5sec 风控壳而非商品 HTML，`is_risk_shell` 负责识别，
命中即判 blocked —— 绝不能当成"采集成功"往池子里写空记录（历史坑，见 §9 修复项）。
用法：collector = AlibabaExecutor(cookie_text=...)；
     await collector.collect_by_offer_ids(db, account_id, offer_ids)
"""

import asyncio
import logging
import random
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.alibaba.parser import parse_offer
from app.collectors.alibaba.transform import transform_product
from app.collectors.alibaba.workspace import CollectWorkspace, make_batch_id
from app.collectors.base import PoolStatus, SourcedProduct, is_risk_shell
from app.core.config import settings
from app.models import Goods

logger = logging.getLogger(__name__)

# 1688 详情页 URL 模板
DETAIL_URL_TEMPLATE = "https://detail.1688.com/offer/{offer_id}.html"

# 连续这么多次撞风控就停整批（§6 硬性约束 5：不无限重试）
MAX_CONSECUTIVE_BLOCKED = 3

# 请求头（模拟浏览器，避免被反爬拦截）
DETAIL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.1688.com/",
    "Cache-Control": "no-cache",
}


class AlibabaExecutor:
    """1688 采集执行器：单 offer_id 采集详情页 → parser 解析 → 入库 SOURCED。"""

    def __init__(
        self,
        *,
        http: Optional[httpx.AsyncClient] = None,
        request_delay_range: tuple[float, float] = (1.0, 3.0),
        max_concurrent: int = 2,
        workspace: CollectWorkspace | None = None,
        cookie_text: str | None = None,
    ) -> None:
        """
        Args:
            http: 可注入的 httpx 客户端（测试用）；None 则内部创建。
            request_delay_range: 请求间随机延迟区间（秒），人性化节奏防反爬。
            max_concurrent: 并发上限（1688 详情页反爬敏感，建议 1-3）。
            workspace: 本地文件夹工作区（§3.1），None 用 settings 根目录。
            cookie_text: 1688 货源账号 Cookie 明文（auth/alibaba_login 扫码所得）。
                不带 = 匿名，详情页会被 x5sec punish；跨域 Cookie 手动拼进请求头，
                绕开 httpx jar 的 domain 限制（同 core/cookies.py 里闲鱼那处处理）。
        """
        self._http = http
        self._request_delay_range = request_delay_range
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._workspace = workspace or CollectWorkspace()
        self._cookie_text = cookie_text

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            headers = dict(DETAIL_HEADERS)
            if self._cookie_text:
                headers["Cookie"] = self._cookie_text
            self._http = httpx.AsyncClient(timeout=30.0, headers=headers)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _fetch_detail(self, offer_id: str) -> Optional[str]:
        """抓取单个 offer_id 的详情页 HTML。"""
        url = DETAIL_URL_TEMPLATE.format(offer_id=offer_id)
        http = await self._client()
        try:
            resp = await http.get(url)
            if resp.status_code == 200:
                return resp.text
            logger.warning("1688 详情页抓取失败 offer_id=%s status=%d", offer_id, resp.status_code)
        except Exception:
            logger.exception("1688 详情页抓取异常 offer_id=%s", offer_id)
        return None

    async def _collect_one(
        self, db: AsyncSession, account_id: int, offer_id: str, batch_id: str
    ) -> tuple[str, str]:
        """
        采集单个 offer_id：落工作区（product.json collect 段 + 图片）并入库。
        Returns: (offer_id, 'added' | 'skipped' | 'failed' | 'blocked')
        """
        # 去重：同账号同来源同 offer_id 已存在则跳过
        existing = (
            await db.execute(
                select(Goods.id).where(
                    Goods.account_id == account_id,
                    Goods.source == "1688",
                    Goods.source_offer_id == offer_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            logger.info("1688 商品已存在，跳过 account_id=%d offer_id=%s", account_id, offer_id)
            return offer_id, "skipped"

        html = await self._fetch_detail(offer_id)
        if not html:
            return offer_id, "failed"
        if is_risk_shell(html):
            # 风控壳不是"这个商品没数据"，是"我们没资格看"——落一条空 Goods 会让面板
            # 显示采集成功、放行闸门又查不出问题，只会把脏数据往下游推。
            logger.warning(
                "1688 详情页返回风控壳（需 1688 货源账号登录态）offer_id=%s bytes=%d",
                offer_id,
                len(html),
            )
            self._workspace.write_blocked(batch_id, offer_id, html, reason="risk_shell")
            return offer_id, "blocked"

        sourced = parse_offer(html, offer_id)
        if not sourced.title:
            # 页拿到了却解析不出标题 = 页面改版或解析失效，同样不能当成功入库
            logger.warning("1688 解析无标题，判为采集失败 offer_id=%s", offer_id)
            self._workspace.write_blocked(batch_id, offer_id, html, reason="parse_empty")
            return offer_id, "failed"

        # 工作区落盘（§3.1/§3.3）：collect 段 + 图片下载；解析失败存 raw_excerpt
        collect = self._workspace.build_collect_section(
            offer_id=offer_id,
            title_raw=sourced.title,
            price_cent=sourced.price_cent,
            image_urls=sourced.image_urls,
            raw=sourced.raw,
            raw_excerpt=None if sourced.title else html,
        )
        local_images = await self._workspace.fetch_images(
            batch_id, offer_id, sourced.image_urls or [], http=await self._client()
        )
        product_path = self._workspace.write_collect(batch_id, collect, local_images=local_images)

        # 采集只到 SOURCED：标题/价格先按规则打底（Goods.price_cent 非空，必须有值），
        # 但**不进 MAPPED**——MAPPED 是"人工放行"的专属语义，auto_listing 只吃 MAPPED，
        # 这样定时采集一旦上线也不会自己把东西发出去（§8.1 防线 / §3.2 状态机）。
        # LLM 定价/文案由 /collect/offers/{batch}/{offer}/decide 产出 decision 段后升 DECIDED。
        if sourced.price_cent > 0:
            rate = settings.collect_markup_rate
            transformed = transform_product(sourced, rate=rate)
            title = transformed.title
            description = transformed.description
            price_cent = transformed.price_cent
            image_urls = transformed.image_urls
        else:
            rate = None
            title = sourced.title
            description = sourced.description
            price_cent = sourced.price_cent
            image_urls = sourced.image_urls
            logger.warning(
                "1688 无成本价，需人工定价 account_id=%d offer_id=%s", account_id, offer_id
            )
        pool_status = PoolStatus.SOURCED.value

        goods = Goods(
            account_id=account_id,
            category="B",
            source="1688",
            source_offer_id=sourced.source_offer_id,
            title=title,
            description=description,
            price_cent=price_cent,
            source_price_cent=sourced.price_cent or None,
            image_urls=image_urls,
            pool_status=pool_status,
            local_path=str(product_path.parent),
            raw_payload={
                **(sourced.raw or {}),
                "source_title": sourced.title,
                "source_offer_id": sourced.source_offer_id,
                "markup_rate": rate,
            },
        )
        db.add(goods)
        await db.commit()
        logger.info(
            "1688 采集入库 %s account_id=%d offer_id=%s goods_id=%d",
            pool_status, account_id, offer_id, goods.id,
        )
        return offer_id, "added"

    async def collect_by_offer_ids(
        self,
        db: AsyncSession,
        account_id: int,
        offer_ids: list[str],
        *,
        batch_id: str | None = None,
        trigger: str = "manual",
        instruction: str | None = None,
    ) -> dict:
        """
        批量按 offer_id 采集（去重 + 随机延迟 + 并发控制 + 批次台账 §3.1）。

        连续 MAX_CONSECUTIVE_BLOCKED 件撞风控即中止剩余采集（§6 约束 5：不无限重试），
        未尝试的件计入 "not_attempted" 返回，让调用方知道批次是被截断的。
        Returns: {"batch_id":…, "added":…, "skipped":…, "failed":…, "blocked":…,
                  "not_attempted":…, "aborted": bool}
        """
        batch_id = batch_id or make_batch_id(instruction or trigger)
        self._workspace.start_batch(
            batch_id, trigger=trigger, instruction=instruction, offer_ids=offer_ids
        )
        stats = {"added": 0, "skipped": 0, "failed": 0, "blocked": 0}
        consecutive_blocked = 0
        attempted = 0
        aborted = False
        for i, offer_id in enumerate(offer_ids):
            async with self._semaphore:
                if i:
                    await asyncio.sleep(random.uniform(*self._request_delay_range))
                _, outcome = await self._collect_one(db, account_id, offer_id, batch_id)
            stats[outcome] += 1
            attempted += 1
            if outcome == "blocked":
                consecutive_blocked += 1
                if consecutive_blocked >= MAX_CONSECUTIVE_BLOCKED:
                    aborted = True
                    break
            else:
                consecutive_blocked = 0
        not_attempted = len(offer_ids) - attempted
        self._workspace.close_batch(batch_id, **stats)
        if aborted:
            logger.warning(
                "1688 采集中止：连续 %d 件撞风控，剩余 %d 件未尝试 batch_id=%s",
                consecutive_blocked,
                not_attempted,
                batch_id,
            )
        return {"batch_id": batch_id, "not_attempted": not_attempted, "aborted": aborted, **stats}

    async def collect_by_keyword(
        self,
        db: AsyncSession,
        account_id: int,
        keyword: str,
        limit: int = 20,
        *,
        batch_id: str | None = None,
        trigger: str = "keyword",
        instruction: str | None = None,
        max_pages: int = 1,
    ) -> dict:
        """按关键词采集：先检索出 offer_id（一次性浏览器），再走详情页链路。

        检索失败（风控壳/登录墙）时 `SearchBlocked` 直接向上抛，不在这里吞——
        吞掉就会产出一个 added=0 的空批次台账，看起来像"这个关键词没货"，
        而真实原因是"我们没资格看"，两者处置动作完全相反。
        """
        from app.collectors.alibaba.search_browser import search_offers

        found = await search_offers(
            keyword, cookie_text=self._cookie_text, limit=limit, max_pages=max_pages
        )
        result = await self.collect_by_offer_ids(
            db,
            account_id,
            found.offer_ids,
            batch_id=batch_id,
            trigger=trigger,
            instruction=instruction or keyword,
        )
        result["searched"] = {
            "keyword": keyword,
            "found": len(found.offer_ids),
            "source": found.source,
            "pages": found.pages,
        }
        return result


async def import_1688_by_offer_ids(
    db: AsyncSession,
    account_id: int,
    offer_ids: list[str],
    *,
    batch_id: str | None = None,
    trigger: str = "manual",
    instruction: str | None = None,
    alibaba_account_id: int | None = None,
) -> dict:
    """模块级便捷函数：按 offer_id 列表采集 → 落工作区 → 入池 SOURCED（待定价/待放行）。

    alibaba_account_id 指定用哪个 1688 货源账号的登录态抓取；不传则匿名抓，
    当前 1688 对匿名详情页返回 x5sec 风控壳，整批会判 blocked。
    """
    cookie_text = None
    if alibaba_account_id is not None:
        from app.auth.alibaba_account_service import alibaba_cookie_text

        cookie_text = await alibaba_cookie_text(db, alibaba_account_id)

    executor = AlibabaExecutor(cookie_text=cookie_text)
    try:
        return await executor.collect_by_offer_ids(
            db, account_id, offer_ids, batch_id=batch_id, trigger=trigger, instruction=instruction
        )
    finally:
        await executor.aclose()