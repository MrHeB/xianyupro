"""商品发布器（阶段 4-5：代码就绪待小号实测，protocol.md §11.2 验证项 V4）。

发布链路四步（protocol.md §3/§4，goofish-cli 已验证的顺序）：
1. 逐图上传闲鱼 CDN（stream-upload.goofish.com，非 mtop 无签名，逐张 0.3-0.8s 间隔）；
2. 类目推荐 mtop.taobao.idle.kgraph.property.recommend v2.0（按标题+图）；
3. 发布所在地 mtop.taobao.idle.local.poi.get v1.0（地址在 data 顶层的 selectedPoi）；
4. 提交 mtop.idle.pc.idleitem.publish v1.0（DTO 见 listing/dto.py，is_write 走限流）。

发布成功写 goods_edit_snapshot（快照回放式重发的根基，盘点 §4.6）。
鱼小铺多规格（backend.idleitem.publish）后置，当前只做普通号单规格。
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_client.errors import ErrorKind, XianyuApiError
from app.core.failure_guard import WriteGuardPaused, failure_guard, guard_key
from app.listing.dto import (
    ImageInfoDO,
    ItemAddrDTO,
    ItemCatDTO,
    ItemPostFeeDTO,
    ItemTextDTO,
    ItemPriceDTO,
    PublishItemRequest,
)
from app.models import Goods, GoodsEditSnapshot

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://stream-upload.goofish.com/api/upload.api"
UPLOAD_QUERY = {"floderId": "0", "appkey": "xy_chat", "_input_charset": "utf-8"}

RECOMMEND_API = "mtop.taobao.idle.kgraph.property.recommend"
RECOMMEND_VERSION = "2.0"
POI_API = "mtop.taobao.idle.local.poi.get"
POI_VERSION = "1.0"
PUBLISH_API = "mtop.idle.pc.idleitem.publish"
PUBLISH_VERSION = "1.0"

# 虚拟商品默认类目（软件类，pro 同款 [待验证]）
VIRTUAL_CAT_ID = "50025461"
VIRTUAL_CAT_NAME = "软件服务"

UPLOAD_HEADERS = {
    "Origin": "https://www.goofish.com",
    "Referer": "https://www.goofish.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


@dataclass
class UploadedImage:
    url: str          # 闲鱼 CDN URL
    width: int = 0
    height: int = 0

    @property
    def size_str(self) -> str:
        return f"{self.width}x{self.height}" if self.width and self.height else ""


def _parse_pix(pix: str | None) -> tuple[int, int]:
    """'800x600' → (800, 600)；非法返回 (0, 0)。"""
    if not pix or "x" not in pix:
        return 0, 0
    try:
        w, _, h = pix.lower().partition("x")
        return int(w), int(h)
    except ValueError:
        return 0, 0


class ItemPublisher:
    """单账号商品发布器（transport 层；DB 落库在模块级 publish_goods）。"""

    def __init__(self, session, *, http: httpx.AsyncClient | None = None) -> None:
        """session：auth 会话池的 AccountSession（cookie + mtop）。http 可注入测试假件。"""
        self.session = session
        self._http = http

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=60.0, headers=UPLOAD_HEADERS)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    # ---------- ① 图片上传（protocol.md §4：非 mtop、无签名、只要 Cookie） ----------

    async def _post_upload(self, name: str, content: bytes) -> UploadedImage:
        """上传二进制到闲鱼 CDN（一律声明 image/png，goofish-cli 实测可过，勿"顺手修正"）。"""
        http = await self._client()
        try:
            up = await http.post(
                UPLOAD_URL,
                params=UPLOAD_QUERY,
                files={"file": (name, content, "image/png")},
                headers={**UPLOAD_HEADERS, "Cookie": self.session.cookie},
            )
            payload = up.json()
        except (httpx.HTTPError, ValueError) as exc:
            # 与 MtopClient 同口径：传输层异常折算成 NETWORK，别让它冒成裸 500
            raise XianyuApiError(
                ErrorKind.NETWORK, f"图片上传网络异常（{type(exc).__name__}），未提交闲鱼"
            ) from exc
        obj = payload.get("object") or {}
        url = obj.get("url")
        if not url:
            raise XianyuApiError(ErrorKind.UNKNOWN, f"图片上传失败：{payload}")
        width, height = _parse_pix(obj.get("pix"))
        return UploadedImage(url=url, width=width, height=height)

    async def upload_image(self, image_url: str) -> UploadedImage:
        """单张远程图：下载源图 → 传闲鱼 CDN。"""
        http = await self._client()
        try:
            resp = await http.get(image_url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise XianyuApiError(
                ErrorKind.NETWORK, f"源图下载失败（{type(exc).__name__}）：{image_url[:80]}"
            ) from exc
        name = urlparse(image_url).path.rsplit("/", 1)[-1] or "image.png"
        return await self._post_upload(name, resp.content)

    async def upload_image_path(self, local_path: str | Path) -> UploadedImage:
        """单张本地落盘图 → 传闲鱼 CDN（采集工作区 images/*.jpg，设计文档 §9-3）。"""
        path = Path(local_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"本地图片不存在：{path}")
        return await self._post_upload(path.name, path.read_bytes())

    async def upload_images(self, image_urls: list[str]) -> list[UploadedImage]:
        """逐张上传，人性化间隔 0.3-0.8s（protocol.md §1.4）。"""
        images: list[UploadedImage] = []
        for i, url in enumerate(image_urls):
            if i:
                await asyncio.sleep(random.uniform(0.3, 0.8))
            images.append(await self.upload_image(url))
        return images

    # ---------- ② 类目推荐（protocol.md §3.4） ----------

    async def recommend_category(self, title: str, images: list[UploadedImage]) -> ItemCatDTO:
        """类目推荐（protocol.md §3.4，只读）。

        `imageInfos` 传**图对象数组**，不是 URL 字符串数组：字符串数组会让服务端
        反序列化 DTO 时抛 `FAIL_SYS_SERVICE_INNER_FAULT::服务内部故障`（2026-09-20 烧掉
        3 次写失败额度才定位到）。两个参考实现同为对象数组（goofish-cli
        `commands/category/recommend.py`、xianyu-pilot `services/auto_category.py`），
        字段与发布请求体的 `imageInfoDOList` 一致，故复用 `ImageInfoDO`。
        """
        data = await self.session.mtop.call(
            RECOMMEND_API,
            RECOMMEND_VERSION,
            {
                "title": title,
                "lockCpv": False,
                "multiSKU": False,
                "publishScene": "mainPublish",
                "scene": "newPublishChoice",
                "description": title,
                "imageInfos": [
                    ImageInfoDO(
                        url=img.url,
                        major=(i == 0),
                        widthSize=img.width,
                        heightSize=img.height,
                    ).model_dump()
                    for i, img in enumerate(images)
                ],
                "uniqueCode": "1775905618164677",
            },
        )
        cat = (data.get("categoryPredictResult") or {})
        cat_id = cat.get("catId")
        if not cat_id:
            raise XianyuApiError(ErrorKind.UNKNOWN, f"类目推荐无结果：{data}")
        return ItemCatDTO(
            catId=str(cat_id),
            catName=str(cat.get("catName") or ""),
            channelCatId=str(cat["channelCatId"]) if cat.get("channelCatId") else None,
            tbCatId=str(cat["tbCatId"]) if cat.get("tbCatId") else None,
        )

    # ---------- ③ 发布所在地（protocol.md §3.5） ----------

    async def default_address(self) -> ItemAddrDTO:
        """商品所在地（发布页那块地址，来自账号常用地址）。

        真机形状（2026-09-20 取证）：`data` 顶层**只有 `selectedPoi` 一个键**，
        没有 `commonAddresses`；字段名是 `poi` / `poiId` / `latitude`+`longitude`，
        不是 `poiName` / `gps`。旧实现去 `commonAddresses` 里逐条找 `selectedPoi`，
        于是把"账号其实有地址"读成了「账号无默认收货地址」并硬失败。
        入参带不带经纬度返回一致，所以传空即可。
        """
        data = await self.session.mtop.call(POI_API, POI_VERSION, {})
        addresses = data.get("commonAddresses") or []
        poi = data.get("selectedPoi") or (addresses[0] if addresses else None)
        if not isinstance(poi, dict):
            # 参考实现在没有地址时把 itemAddrDTO 传空对象，发布照样成功；
            # 在这里抛错等于"账号没地址"要占一次发布写失败额度（3 次 → 24h 熔断）。
            logger.warning(
                "poi.get 未返回地址（键=%s），按空所在地发布——商品将不显示所在地",
                sorted(data),
            )
            return ItemAddrDTO(prov="", city="", area="", divisionId="")
        lng, lat = poi.get("longitude"), poi.get("latitude")
        return ItemAddrDTO(
            prov=str(poi.get("prov") or ""),
            city=str(poi.get("city") or ""),
            area=str(poi.get("area") or ""),
            divisionId=str(poi.get("divisionId") or ""),
            gps=f"{lng},{lat}" if lng is not None and lat is not None else None,
            poiId=str(poi["poiId"]) if poi.get("poiId") else None,
            poiName=str(poi["poi"]) if poi.get("poi") else None,
        )

    # ---------- ④ 发布提交（is_write 走 1 次/分钟令牌桶） ----------

    async def publish(self, request: PublishItemRequest) -> str:
        """发布商品，返回 external_goods_id。"""
        data = await self.session.mtop.call(
            PUBLISH_API, PUBLISH_VERSION, request.model_dump(), is_write=True
        )
        external_id = _extract_item_id(data)
        if not external_id:
            # 发布接口返回结构 [待验证]；拿不到 ID 视为结果不确定，交人工核对
            raise XianyuApiError(
                ErrorKind.UNKNOWN, f"发布返回无商品ID（可能已成功，人工核对）：{data}"
            )
        return external_id

    def build_request(
        self,
        *,
        title: str,
        desc: str,
        price_cent: int,
        images: list[UploadedImage],
        category: ItemCatDTO,
        address: ItemAddrDTO,
        virtual: bool = False,
        orig_price_cent: int | None = None,
    ) -> PublishItemRequest:
        if not images:
            raise ValueError("发布至少需要 1 张图（首图）")
        if virtual:
            # 虚拟商品：无需邮寄 + 虚拟类目（protocol.md §3.1）
            post_fee = ItemPostFeeDTO.no_shipping()
            if not category.catId or category.catId == "0":
                category = ItemCatDTO(catId=VIRTUAL_CAT_ID, catName=VIRTUAL_CAT_NAME)
        else:
            post_fee = ItemPostFeeDTO.free()
        return PublishItemRequest(
            imageInfoDOList=[
                ImageInfoDO(url=img.url, major=(i == 0), widthSize=img.width, heightSize=img.height)
                for i, img in enumerate(images)
            ],
            itemTextDTO=ItemTextDTO(title=title, desc=desc),
            itemPriceDTO=ItemPriceDTO(
                priceInCent=str(price_cent),
                origPriceInCent=str(orig_price_cent) if orig_price_cent else None,
            ),
            itemPostFeeDTO=post_fee,
            itemCatDTO=category,
            itemAddrDTO=address,
        )


def _extract_item_id(data: dict) -> str | None:
    """发布返回的商品 ID（字段名 [待验证]，按常见拼写防御性提取）。"""
    for key in ("itemId", "id"):
        if data.get(key):
            return str(data[key])
    item_do = data.get("itemDO") or {}
    for key in ("itemId", "id"):
        if item_do.get(key):
            return str(item_do[key])
    return None


# ---------- 模块级编排：发布 Goods 全链路 + DB 落库 ----------

async def publish_goods(db: AsyncSession, goods: Goods, session, *, http=None) -> str:
    """四步发布 + 落库：external_goods_id / status=1 在售 / pool_status=PUSHED
    + goods_edit_snapshot（source=publish，快照回放式重发的根基）。

    熔断（风控手册 §4 R-2/R-4/R-5）：熔断期内直接拒发（WriteGuardPaused，
    retryable=False）；风控/登录态类失败 immediate 触发 24h 止损，其余累计 3 次，
    **但 NETWORK / LOCAL_THROTTLE（请求没送达闲鱼，或自己令牌桶没排到号）不占额度**。
    解锁信号取 AccountAuth.last_login_at
    （仅重新扫码时更新；updated_at 会被保活滚动刷新，不能作信号）。
    失败抛 XianyuApiError（路由层负责置 pool_status=FAILED 并回传原因）。
    `http` 只为测试注入传输假件，生产留空。
    """
    from app.models import AccountAuth

    if not (goods.image_urls or []):
        # 空 imageInfos 会被服务端读成"服务内部故障"，白烧一次写失败额度（见 §3.4）
        raise ValueError("发布至少需要 1 张图（image_urls 为空）")

    key = guard_key("publish", goods.account_id)
    auth = await db.get(AccountAuth, goods.account_id)
    login_ts = auth.last_login_at if auth else None

    paused = failure_guard.is_paused(key, cookie_updated_at=login_ts)
    if paused:
        raise WriteGuardPaused(key, paused.remaining_seconds)

    publisher = ItemPublisher(session, http=http)
    try:
        images = await publisher.upload_images(goods.image_urls or [])
        category = await publisher.recommend_category(goods.title, images)
        address = await publisher.default_address()
        request = publisher.build_request(
            title=goods.title,
            desc=goods.description or goods.title,
            price_cent=goods.price_cent,
            images=images,
            category=category,
            address=address,
            virtual=goods.category == "A",
        )
        external_id = await publisher.publish(request)
    except XianyuApiError as exc:
        if exc.kind in (ErrorKind.NETWORK, ErrorKind.LOCAL_THROTTLE):
            # 请求根本没送达闲鱼（连接被断/超时/网关回了 HTML），或是我们自己令牌桶
            # 没排到号——都不是平台在拒我们。记进止损额度会让三次本地抖动/限流换来
            # 24h 熔断，方向错了。仍然抛出，交上层重试。
            logger.warning(
                "发布链路 %s 异常，不计入止损额度 goods_id=%s：%s", exc.kind.value, goods.id, exc
            )
            raise
        # 风控/登录态/非法访问 = 确定性错误，阈值降为 1 立即熔断（手册 §1.2）
        immediate = exc.kind in (
            ErrorKind.RISK_CONTROL,
            ErrorKind.ILLEGAL_ACCESS,
            ErrorKind.SESSION_EXPIRED,
        )
        failure_guard.record_failure(key, cookie_updated_at=login_ts, immediate=immediate)
        raise
    finally:
        await publisher.aclose()

    failure_guard.record_success(key)
    goods.external_goods_id = external_id
    goods.status = 1  # 在售
    goods.pool_status = "PUSHED"
    db.add(
        GoodsEditSnapshot(
            goods_id=goods.id, source="publish", snapshot_json=request.model_dump()
        )
    )
    await db.commit()
    logger.info(
        "商品发布成功 goodsId=%d externalId=%s title=%s",
        goods.id, external_id, goods.title,
    )
    return external_id
