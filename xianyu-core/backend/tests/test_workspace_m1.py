"""M1 本地文件夹工作区测试（设计文档 §3 / R2/R3 验收，fixture 单测）。

覆盖：batch_id 规则、batch.json + batches.jsonl 台账、product.json 四段结构、
重采幂等（collect 更新 / decision 保留）、raw_excerpt 截断、
图片下载落盘（≤9 张、长边压 1440、坏图跳过不阻塞）。
"""

import io
import json
import re

import pytest
from PIL import Image

from app.collectors.alibaba.workspace import (
    MAX_EDGE,
    MAX_IMAGES,
    PRODUCT_SCHEMA,
    CollectWorkspace,
    make_batch_id,
)


def _jpeg_bytes(w: int = 2000, h: int = 300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


class _Resp:
    def __init__(self, content: bytes, fail: bool = False):
        self.content = content
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise RuntimeError("404")


class _FakeImgHttp:
    """按 URL 尾号决定成败/坏数据的假图床。"""

    def __init__(self):
        self.requested = []

    async def get(self, url):
        self.requested.append(url)
        if url.endswith("bad.jpg"):
            return _Resp(b"not an image")
        if url.endswith("dead.jpg"):
            return _Resp(b"", fail=True)
        return _Resp(_jpeg_bytes())

    async def aclose(self):
        pass


@pytest.fixture
def ws(tmp_path):
    return CollectWorkspace(tmp_path / "collect")


def _collect(offer_id: str = "688234567890", price: int = 350) -> dict:
    return {
        "source": "1688",
        "offer_id": offer_id,
        "url": f"https://detail.1688.com/offer/{offer_id}.html",
        "title_raw": "厂家直销type-c数据线",
        "source_price_cent": price,
        "price_available": price > 0,
        "stock": 99999,
        "seller": "深圳市XX电子",
        "category_raw": "数码配件",
        "specs": {"接口": "Type-C"},
        "image_urls": ["https://cbu01.alicdn.com/a.jpg"],
        "fetched_at": "2026-09-19T10:00:00+08:00",
        "parser": "regex-window-context",
    }


def test_make_batch_id_format():
    bid = make_batch_id("Type-C 数据线 1m!!")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}_", bid)
    assert make_batch_id(None).endswith("_manual")


def test_start_and_close_batch_ledger(ws):
    ws.start_batch("B1", trigger="manual", instruction="帮我采 3 款", offer_ids=["1", "2"])
    meta = json.loads((ws.batch_dir("B1") / "batch.json").read_text(encoding="utf-8"))
    assert meta["instruction"] == "帮我采 3 款" and meta["trigger"] == "manual"
    assert (ws.batch_dir("B1") / "offers").is_dir()

    ws.close_batch("B1", added=1, skipped=1, failed=0)
    lines = (ws.root / "batches.jsonl").read_text(encoding="utf-8").strip().splitlines()
    row = json.loads(lines[-1])
    assert row["batch_id"] == "B1" and row["status"] == "done" and row["count"]["added"] == 1


def test_write_collect_product_json_contract(ws):
    path = ws.write_collect("B1", _collect(), local_images=["images/1.jpg"])
    product = json.loads(path.read_text(encoding="utf-8"))
    assert product["schema"] == PRODUCT_SCHEMA
    assert product["status"]["stage"] == "sourced"
    assert product["decision"] is None  # M2 前 decision 留空
    assert product["media"]["local_images"] == ["images/1.jpg"]
    assert product["collect"]["offer_id"] == "688234567890"
    assert path.parent.name == "688234567890"  # 目录名 = offer_id 幂等键


def test_recollect_updates_collect_keeps_decision(ws):
    batch = "B1"
    path = ws.write_collect(batch, _collect(price=350), local_images=[])
    product = json.loads(path.read_text(encoding="utf-8"))
    product["decision"] = {"pricing": "llm", "price_cent": 1990}
    path.write_text(json.dumps(product), encoding="utf-8")

    ws.write_collect(batch, _collect(price=400), local_images=["images/1.jpg"])
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["collect"]["source_price_cent"] == 400  # collect 更新
    assert after["decision"]["price_cent"] == 1990  # §3.3-3 decision 不被覆盖
    assert any(h["stage"] == "re-collected" for h in after["status"]["history"])


def test_build_collect_raw_excerpt_truncated(ws):
    collect = ws.build_collect_section(
        offer_id="1", title_raw="", price_cent=0, image_urls=None, raw={},
        raw_excerpt="<html>" + "x" * 5000,
    )
    assert len(collect["raw_excerpt"]) <= 2000 + 10
    assert collect["price_available"] is False


async def test_fetch_images_resizes_caps_and_skips_bad(ws):
    urls = [f"https://cbu01.alicdn.com/i{n}.jpg" for n in range(12)]
    urls += ["https://cbu01.alicdn.com/bad.jpg", "https://cbu01.alicdn.com/dead.jpg"]
    fake = _FakeImgHttp()
    saved = await ws.fetch_images("B1", "999", urls, http=fake)

    assert len(saved) == MAX_IMAGES  # 超量截断，坏数据/失败不占坑但被上限拦截
    assert all(s.startswith("images/") and s.endswith(".jpg") for s in saved)
    imgs_dir = ws.offer_dir("B1", "999") / "images"
    files = sorted(imgs_dir.iterdir())
    assert len(files) == MAX_IMAGES
    with Image.open(files[0]) as img:
        assert max(img.size) <= MAX_EDGE  # 2000×300 → 长边 1440


async def test_fetch_images_bad_urls_skipped(ws):
    fake = _FakeImgHttp()
    saved = await ws.fetch_images(
        "B1", "888", ["https://cbu01.alicdn.com/dead.jpg", "https://cbu01.alicdn.com/bad.jpg",
                     "https://cbu01.alicdn.com/ok.jpg"], http=fake,
    )
    assert saved == ["images/3.jpg"]  # 序号跟随源位置，缺图留洞不重排
