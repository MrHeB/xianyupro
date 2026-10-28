"""M0 前置修复测试：§9-1 采集不再因缺列报错、§9-2 即采即转 MAPPED、§9-3 本地图上传、
§3.4 类目推荐 + §3.5 发布所在地 的请求/响应形状（发布链路第 2、3 步，2026-09-20 真号定位）。"""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.collectors.alibaba import executor as ex_mod
from app.collectors.alibaba.executor import AlibabaExecutor
from app.collectors.alibaba.workspace import CollectWorkspace
from app.collectors.base import SourcedProduct
from app.models import Base, Goods
from app.listing.publisher import ItemPublisher, UploadedImage, publish_goods


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _sourced(offer_id: str, price_cent: int) -> SourcedProduct:
    return SourcedProduct(
        source="1688",
        source_offer_id=offer_id,
        title="厂家直销type-c数据线【爆款】快充",
        description="好物",
        price_cent=price_cent,
        image_urls=["https://cbu01.alicdn.com/x.jpg"],
        raw={"specs": {"长度": "1m"}},
    )


async def _ok(v):
    return v


def _jpeg_bytes(w: int = 200, h: int = 100) -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


class _FakeCollectHttp:
    """详情页抓取被 monkeypatch 掉，这里只服务 workspace.fetch_images。"""

    async def get(self, url, **kwargs):
        return SimpleNamespace(content=_jpeg_bytes(), status_code=200,
                               raise_for_status=lambda: None)

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_collect_stays_sourced_until_approval(session_factory, monkeypatch, tmp_path):
    """§9-2 + §8.1 验收：采集后 pool_status=SOURCED（**不得直接 MAPPED**，否则定时会自己发出去）；
    标题去噪、价格=源价×倍率只是打底，源价留存。"""
    monkeypatch.setattr(ex_mod, "parse_offer", lambda html, oid: _sourced(oid, 1000))
    monkeypatch.setattr(
        AlibabaExecutor, "_fetch_detail", lambda self, oid: _ok("<html>x</html>")
    )
    workspace = CollectWorkspace(tmp_path)
    executor = AlibabaExecutor(
        http=_FakeCollectHttp(), request_delay_range=(0, 0), workspace=workspace
    )
    async with session_factory() as db:
        result = await executor.collect_by_offer_ids(db, 1, ["728001"])
    await executor.aclose()

    assert {k: v for k, v in result.items() if k != "batch_id"} == {
        "added": 1, "skipped": 0, "failed": 0, "blocked": 0, "not_attempted": 0, "aborted": False
    }
    # M1 验收：工作区落盘 product.json + 图片，Goods.local_path 指向 offer 目录
    product_json = workspace.offer_dir(result["batch_id"], "728001") / "product.json"
    assert product_json.is_file()
    assert (product_json.parent / "images" / "1.jpg").is_file()
    async with session_factory() as db:
        goods = (await db.execute(select(Goods))).scalars().one()
        assert goods.pool_status == "SOURCED"
        assert goods.title == "type-c数据线快充"  # clean_title 生效
        assert goods.source_price_cent == 1000
        assert goods.price_cent == 1290  # 10×1.3=13 元 → 取整 −0.1
        assert goods.source_offer_id == "728001"
        assert goods.local_path == str(product_json.parent)
        assert goods.raw_payload["source_title"].startswith("厂家直销")


@pytest.mark.asyncio
async def test_recollect_same_offer_skipped(session_factory, monkeypatch, tmp_path):
    """§9-1 验收：source_offer_id 唯一键去重，同 offer 二次采集走跳过不报错。"""
    monkeypatch.setattr(ex_mod, "parse_offer", lambda html, oid: _sourced(oid, 1000))
    monkeypatch.setattr(
        AlibabaExecutor, "_fetch_detail", lambda self, oid: _ok("<html>x</html>")
    )
    executor = AlibabaExecutor(
        http=_FakeCollectHttp(),
        request_delay_range=(0, 0),
        workspace=CollectWorkspace(tmp_path),
    )
    async with session_factory() as db:
        await executor.collect_by_offer_ids(db, 1, ["728001"])
        result = await executor.collect_by_offer_ids(db, 1, ["728001"])
    await executor.aclose()
    assert result["skipped"] == 1 and result["added"] == 0


@pytest.mark.asyncio
async def test_no_source_price_stays_sourced(session_factory, monkeypatch, tmp_path):
    """拿不到成本价 → 保持 SOURCED 待人工定价（§7.1 第 [3] 级），不写垃圾价。"""
    monkeypatch.setattr(ex_mod, "parse_offer", lambda html, oid: _sourced(oid, 0))
    monkeypatch.setattr(
        AlibabaExecutor, "_fetch_detail", lambda self, oid: _ok("<html>x</html>")
    )
    executor = AlibabaExecutor(
        http=_FakeCollectHttp(),
        request_delay_range=(0, 0),
        workspace=CollectWorkspace(tmp_path),
    )
    async with session_factory() as db:
        await executor.collect_by_offer_ids(db, 1, ["728002"])
    await executor.aclose()
    async with session_factory() as db:
        goods = (await db.execute(select(Goods))).scalars().one()
        assert goods.pool_status == "SOURCED"
        assert goods.price_cent == 0


# ---------- 风控壳：blocked 而不是「采集成功」 ----------

PUNISH_HTML = (
    '<html><script>var _config_ ={"url":".../_____tmd_____/punish?'
    'x5secdata=xxx"};rgv587_flag:"sm";</script></html>'
)


@pytest.mark.asyncio
async def test_risk_shell_blocks_batch_and_stays_out_of_pool(
    session_factory, monkeypatch, tmp_path
):
    """匿名抓详情页必撞 x5sec 壳：判 blocked、连续 3 件即中止，且**一条 Goods 都不许写**。

    旧实现把壳 HTML 解析成空标题也计数 added，面板显示成功、下游全是脏数据。
    """
    monkeypatch.setattr(
        AlibabaExecutor, "_fetch_detail", lambda self, oid: _ok(PUNISH_HTML)
    )
    workspace = CollectWorkspace(tmp_path)
    executor = AlibabaExecutor(
        http=_FakeCollectHttp(), request_delay_range=(0, 0), workspace=workspace
    )
    async with session_factory() as db:
        result = await executor.collect_by_offer_ids(
            db, 1, [str(728001 + i) for i in range(5)]
        )
    await executor.aclose()

    assert result["blocked"] == 3
    assert result["added"] == result["skipped"] == result["failed"] == 0
    assert result["aborted"] is True and result["not_attempted"] == 2

    async with session_factory() as db:
        assert (await db.execute(select(Goods))).scalars().all() == []

    for oid in ("728001", "728002", "728003"):
        product = workspace.read_product(result["batch_id"], oid)
        assert product["status"]["stage"] == "blocked"
        assert product["status"]["history"][0]["reason"] == "risk_shell"
        assert "x5secdata" in product["collect"]["raw_excerpt"]
    # 未尝试的两件既不落盘也不入库
    assert not workspace.offer_dir(result["batch_id"], "728004").exists()

    ledger_line = [
        json.loads(p)
        for p in (workspace.root / "batches.jsonl").read_text(encoding="utf-8").splitlines()
    ][-1]
    assert ledger_line["batch_id"] == result["batch_id"]
    assert ledger_line["status"] == "blocked"
    assert ledger_line["count"] == {"added": 0, "skipped": 0, "failed": 0, "blocked": 3}


@pytest.mark.asyncio
async def test_parse_empty_is_failed_not_added(session_factory, monkeypatch, tmp_path):
    """页拿到了但解析不出标题 = 页面改版：判 failed 并留痕，不入库。"""
    monkeypatch.setattr(ex_mod, "parse_offer", lambda html, oid: SourcedProduct(
        source="1688", source_offer_id=oid, title="", description="", price_cent=0,
    ))
    monkeypatch.setattr(
        AlibabaExecutor, "_fetch_detail", lambda self, oid: _ok("<html>改版了</html>")
    )
    workspace = CollectWorkspace(tmp_path)
    executor = AlibabaExecutor(
        http=_FakeCollectHttp(), request_delay_range=(0, 0), workspace=workspace
    )
    async with session_factory() as db:
        result = await executor.collect_by_offer_ids(db, 1, ["728009"])
    await executor.aclose()

    assert result["failed"] == 1 and result["added"] == 0
    assert result["aborted"] is False and result["not_attempted"] == 0
    assert workspace.read_product(result["batch_id"], "728009")["status"]["stage"] == "blocked"
    async with session_factory() as db:
        assert (await db.execute(select(Goods))).scalars().all() == []


# ---------- §9-3 本地图上传 ----------

class _FakeUploadHttp:
    def __init__(self):
        self.posts = []

    async def post(self, url, params=None, files=None, headers=None):
        self.posts.append({"url": url, "params": params, "files": files, "headers": headers})
        return SimpleNamespace(
            json=lambda: {"object": {"url": "https://img.alicdn.com/up.png", "pix": "1440x1440"}}
        )


@pytest.mark.asyncio
async def test_upload_image_path_local_file(tmp_path):
    img = tmp_path / "1.jpg"
    img.write_bytes(b"\x89PNG fake bytes")
    fake = _FakeUploadHttp()
    pub = ItemPublisher(SimpleNamespace(cookie="ck=1"), http=fake)
    out = await pub.upload_image_path(img)
    assert out.url == "https://img.alicdn.com/up.png"
    assert (out.width, out.height) == (1440, 1440)
    name, content, mime = fake.posts[0]["files"]["file"]
    assert name == "1.jpg" and content == img.read_bytes()
    assert mime == "image/png"  # 已验证可过的约定，勿改


@pytest.mark.asyncio
async def test_upload_image_path_missing_file(tmp_path):
    pub = ItemPublisher(SimpleNamespace(cookie="ck=1"), http=_FakeUploadHttp())
    with pytest.raises(FileNotFoundError):
        await pub.upload_image_path(tmp_path / "nope.jpg")


# ---------- §3.4 类目推荐请求体（发布链路第 2 步） ----------

class _FakeMtop:
    """只记账不联网：断言的是**发出去的 data 长什么样**，不是响应解析。"""

    def __init__(self, result: dict | None = None):
        self.calls = []
        self._result = result or {"categoryPredictResult": {"catId": 50023914, "catName": "数据线"}}

    async def call(self, api, version, data, *, is_write=False):
        self.calls.append({"api": api, "version": version, "data": data})
        return self._result


@pytest.mark.asyncio
async def test_recommend_category_sends_image_objects():
    """`imageInfos` 必须是**图对象数组**。传 URL 字符串数组 = 服务端反序列化 DTO 时抛
    `FAIL_SYS_SERVICE_INNER_FAULT::服务内部故障`（2026-09-20 真号上烧掉 3 次写失败额度）。
    """
    mtop = _FakeMtop()
    pub = ItemPublisher(SimpleNamespace(cookie="ck=1", mtop=mtop))
    cat = await pub.recommend_category(
        "Type-C 快充线 一米",
        [
            UploadedImage(url="https://img.alicdn.com/a.jpg", width=1440, height=1440),
            UploadedImage(url="https://img.alicdn.com/b.jpg", width=800, height=600),
        ],
    )
    infos = mtop.calls[0]["data"]["imageInfos"]
    assert all(isinstance(i, dict) for i in infos), "不能再传 URL 字符串"
    assert infos[0]["url"] == "https://img.alicdn.com/a.jpg"
    assert (infos[0]["widthSize"], infos[0]["heightSize"]) == (1440, 1440)
    assert (infos[1]["widthSize"], infos[1]["heightSize"]) == (800, 600)
    assert infos[0]["major"] is True and infos[1]["major"] is False
    assert infos[0]["status"] == "done" and infos[0]["isQrCode"] is False
    assert (cat.catId, cat.catName) == ("50023914", "数据线")


@pytest.mark.asyncio
async def test_publish_goods_refuses_empty_images():
    """无图直接本地拒发：空 imageInfos 到服务端也是「服务内部故障」，白烧熔断额度。"""
    mtop = _FakeMtop()
    session = SimpleNamespace(cookie="ck=1", mtop=mtop)
    with pytest.raises(ValueError):
        await publish_goods(SimpleNamespace(), SimpleNamespace(image_urls=[], account_id=1), session)
    assert mtop.calls == []


# ---------- §3.5 发布所在地（发布链路第 3 步，2026-09-20 真机取证） ----------

POI_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "xianyu" / "poi_get.json").read_text(encoding="utf-8")
)


@pytest.mark.asyncio
async def test_default_address_reads_top_level_selected_poi():
    """真机响应顶层**只有** `selectedPoi`（没有 `commonAddresses`），字段名是
    `poi` / `latitude`+`longitude`，`divisionId` 是 int。

    旧实现去 `commonAddresses` 里逐条找 `selectedPoi` ⇒ 恒空 ⇒ 把"账号有地址"
    报成「账号无默认收货地址」并硬失败，白烧一次发布额度。fixture 见
    `tests/fixtures/xianyu/poi_get.json`（真机结构，门牌/POI 名/坐标已换成示例值）。
    """
    mtop = _FakeMtop(result=POI_FIXTURE)
    pub = ItemPublisher(SimpleNamespace(cookie="ck=1", mtop=mtop))
    addr = await pub.default_address()
    assert mtop.calls[0]["api"] == "mtop.taobao.idle.local.poi.get"
    assert (addr.prov, addr.city, addr.area) == ("上海市", "上海市", "徐汇区")
    assert addr.divisionId == "310104"  # 服务端给 int，DTO 要 str
    assert addr.gps == "121.473701,31.230416"  # 经度在前，与 goofish-cli 同拼法
    assert (addr.poiId, addr.poiName) == ("B0SYNTH0001", "示例小区东区")


@pytest.mark.asyncio
async def test_default_address_without_poi_returns_blank_not_raise():
    """没地址不是拦路虎：参考实现在无地址时把 `itemAddrDTO` 传空对象、发布照样成功。
    在这里抛错＝账号没设地址也要占一次 publish 写失败额度（3 次 → 24h 熔断）。"""
    mtop = _FakeMtop(result={"selectedPoi": None, "commonAddresses": []})
    pub = ItemPublisher(SimpleNamespace(cookie="ck=1", mtop=mtop))
    addr = await pub.default_address()
    assert (addr.prov, addr.city, addr.area, addr.divisionId) == ("", "", "", "")
    assert (addr.gps, addr.poiId, addr.poiName) == (None, None, None)


class _TransportFailHttp:
    """源图下载/CDN 上传这一跳就断连（2026-09-20 真机在 mtop 那跳踩过同款）。"""

    async def get(self, url, **kwargs):
        raise httpx.ConnectError("")

    async def post(self, url, **kwargs):
        raise httpx.ConnectError("")

    async def aclose(self):
        pass


class _FakeDb:
    async def get(self, model, pk):
        return None


@pytest.mark.asyncio
async def test_publish_network_error_is_readable_and_costs_nothing(tmp_path, monkeypatch):
    """网络类失败：折算成 kind=NETWORK（路由据此回 502 而不是裸 500），**且不占止损额度**——
    请求根本没到闲鱼，三次本地抖动不该换来 24h 熔断。
    """
    import app.listing.publisher as pub
    from app.api_client import ErrorKind, XianyuApiError
    from app.core.failure_guard import FailureGuard, guard_key

    guard = FailureGuard(tmp_path / "g.json", threshold=3, pause_seconds=60)
    monkeypatch.setattr(pub, "failure_guard", guard)
    goods = SimpleNamespace(
        id=7, account_id=1, image_urls=["https://cbu1.jpg"], title="t",
        description="d", price_cent=100, category="A",
    )
    session = SimpleNamespace(cookie="ck=1", mtop=_FakeMtop())

    with pytest.raises(XianyuApiError) as exc_info:
        await pub.publish_goods(_FakeDb(), goods, session, http=_TransportFailHttp())

    assert exc_info.value.kind == ErrorKind.NETWORK
    assert "ConnectError" in str(exc_info.value)
    assert guard.status(guard_key("publish", 1)) == {}, "网络抖动不该记进止损额度"


@pytest.mark.asyncio
async def test_publish_local_throttle_costs_no_strike(tmp_path, monkeypatch):
    """自己令牌桶没排到号（写操作 1 次/分钟）＝请求没出门，同样不占止损额度。

    真机 2026-09-20：auto_listing 连排两次队，因折成 UNKNOWN 记成 2/3 次写失败，
    下一轮就能把 publish 域锁 24h。
    """
    import app.listing.publisher as pub
    from app.api_client import ErrorKind, XianyuApiError
    from app.core.failure_guard import FailureGuard, guard_key

    class _UploadOkHttp:
        async def get(self, url, **kwargs):
            return SimpleNamespace(content=b"x", raise_for_status=lambda: None)

        async def post(self, url, params=None, files=None, headers=None):
            return SimpleNamespace(
                json=lambda: {"object": {"url": "https://i/1.png", "pix": "800x800"}}
            )

        async def aclose(self):
            pass

    class _ThrottledMtop(_FakeMtop):
        async def call(self, api, version, data, *, is_write=False):
            result = await super().call(api, version, data, is_write=is_write)
            if is_write:
                raise XianyuApiError(ErrorKind.LOCAL_THROTTLE, "写操作限流：30s 后重试")
            return result

    guard = FailureGuard(tmp_path / "g.json", threshold=3, pause_seconds=60)
    monkeypatch.setattr(pub, "failure_guard", guard)
    goods = SimpleNamespace(
        id=8, account_id=1, image_urls=["https://cbu1.jpg"], title="t",
        description="d", price_cent=100, category="A",
    )
    session = SimpleNamespace(cookie="ck=1", mtop=_ThrottledMtop())

    with pytest.raises(XianyuApiError) as exc_info:
        await pub.publish_goods(_FakeDb(), goods, session, http=_UploadOkHttp())

    assert exc_info.value.kind == ErrorKind.LOCAL_THROTTLE
    assert guard.status(guard_key("publish", 1)) == {}, "本地排队不该记进止损额度"
