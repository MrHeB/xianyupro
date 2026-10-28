"""本地文件夹工作区（设计文档 §3 / M1）：product.json 四段契约 + 图片落盘 + 批次台账。

目录布局（§3.1）：

    data/collect/
    ├── batches.jsonl                       # 批次台账（追加式，每批收尾一行）
    └── <batch_id>/                         # batch_id = 时间戳_关键词slug
        ├── batch.json                      # 本批元信息 + 触发来源 + 原始自然语言
        └── offers/<offer_id>/              # 目录名 = offer_id，天然幂等键
            ├── product.json                # 唯一数据契约（§3.2 四段制）
            └── images/1.jpg … 9.jpg        # 原图落盘（长边压 1440，§13-Q3 已决定盘）

硬性规则（§3.3）：
1. collect 段只由采集器写，LLM 永不改；
2. decision 段由 LLM 写一次（M2 已接线：ai/decision.py → write_decision）；
3. 重采同一 offer：collect 更新、decision 保留；
4. 原子写（tmp + os.replace），只存 raw_excerpt 不存整页 HTML。
"""

import asyncio
import io
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.core.config import settings

logger = logging.getLogger(__name__)

PRODUCT_SCHEMA = "xianyu-collect-product/1"
DETAIL_URL_TEMPLATE = "https://detail.1688.com/offer/{offer_id}.html"

# 闲鱼单商品 9 图上限；落盘前长边压到 1440（§3.1 体积约定）
MAX_IMAGES = 9
MAX_EDGE = 1440


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_batch_id(keyword: str | None = None) -> str:
    """batch_id = 时间戳_关键词slug（无关键词按触发方式占位）。"""
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M")
    slug = ""
    if keyword:
        slug = re.sub(r"[^0-9a-zA-Z一-鿿]+", "-", keyword.strip()).strip("-")[:40]
    return f"{ts}_{slug or 'manual'}"


class CollectWorkspace:
    """data/collect 工作区读写门面（root 可注入 tmp_path 供测试）。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or settings.collect_workspace_path)

    # ---------- 批次 ----------

    def batch_dir(self, batch_id: str) -> Path:
        return self.root / batch_id

    def offer_dir(self, batch_id: str, offer_id: str) -> Path:
        return self.batch_dir(batch_id) / "offers" / offer_id

    def start_batch(
        self,
        batch_id: str,
        *,
        trigger: str,
        instruction: str | None = None,
        offer_ids: list[str],
    ) -> Path:
        """写 batch.json（R7 追溯起点：触发来源 + 用户原始自然语言）。"""
        d = self.batch_dir(batch_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "offers").mkdir(exist_ok=True)
        _write_json_atomic(
            d / "batch.json",
            {
                "batch_id": batch_id,
                "trigger": trigger,  # scheduled | manual | nl
                "instruction": instruction,
                "offer_ids": offer_ids,
                "started_at": _now_iso(),
            },
        )
        return d

    def list_offer_ids(self, batch_id: str) -> list[str]:
        """本批已落盘的 offer_id（目录名即幂等键），按名称排序。"""
        offers = self.batch_dir(batch_id) / "offers"
        if not offers.is_dir():
            return []
        return sorted(p.name for p in offers.iterdir() if (p / "product.json").is_file())

    def read_batch(self, batch_id: str) -> dict:
        """batch.json 元信息；缺失时按目录名兜底，保证详情页不炸。"""
        path = self.batch_dir(batch_id) / "batch.json"
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("batch.json 损坏，按目录兜底 batch_id=%s", batch_id)
        return {"batch_id": batch_id, "trigger": "unknown", "offer_ids": []}

    def list_batches(self, *, limit: int = 50) -> list[dict]:
        """批次台账（§3.1 batches.jsonl）倒序，并与 batch.json 元信息合并。

        batches.jsonl 只记收尾数，trigger/instruction 在 batch.json——前端列表两处都要看，
        所以在这里合成一份视图（缺 jsonl 行说明该批未跑完，count 置 None）。
        """
        ledger: dict[str, dict] = {}
        log = self.root / "batches.jsonl"
        if log.is_file():
            for line in log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("batch_id"):
                    ledger[row["batch_id"]] = row  # 同批多次收尾取最后一行

        dirs = sorted(
            (p.name for p in self.root.iterdir() if p.is_dir() and (p / "batch.json").is_file()),
            reverse=True,
        ) if self.root.is_dir() else []
        for d in dirs:
            ledger.setdefault(d, {"batch_id": d})

        out = []
        for bid in sorted({*ledger}, reverse=True)[:limit]:
            row = ledger[bid]
            meta = self.read_batch(bid) if bid in dirs else {}
            out.append(
                {
                    "batch_id": bid,
                    "started_at": meta.get("started_at") or row.get("at"),
                    "trigger": meta.get("trigger"),
                    "instruction": meta.get("instruction"),
                    "offer_count": len(meta.get("offer_ids") or []) or None,
                    "count": row.get("count"),
                    "status": row.get("status"),
                }
            )
        return out

    def close_batch(
        self, batch_id: str, *, added: int, skipped: int, failed: int, blocked: int = 0
    ) -> None:
        """batches.jsonl 追加一行收尾台账。"""
        self.root.mkdir(parents=True, exist_ok=True)
        if blocked and not added:
            status = "blocked"  # 整批撞风控：一件都没采到，不是"部分成功"
        elif failed or blocked:
            status = "partial"
        else:
            status = "done"
        line = json.dumps(
            {
                "batch_id": batch_id,
                "at": _now_iso(),
                "count": {"added": added, "skipped": skipped, "failed": failed, "blocked": blocked},
                "status": status,
            },
            ensure_ascii=False,
        )
        with open(self.root / "batches.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ---------- product.json ----------

    def build_collect_section(
        self,
        *,
        offer_id: str,
        title_raw: str,
        price_cent: int,
        image_urls: list[str] | None,
        raw: dict[str, Any] | None,
        parser: str = "regex-window-context",
        raw_excerpt: str | None = None,
    ) -> dict:
        """collect 段 = §3.2 契约字段（解析失败的 FAILED 件也要能落 raw_excerpt）。"""
        raw = raw or {}
        return {
            "source": "1688",
            "offer_id": offer_id,
            "url": DETAIL_URL_TEMPLATE.format(offer_id=offer_id),
            "title_raw": title_raw,
            "source_price_cent": price_cent,
            "price_available": price_cent > 0,
            "stock": raw.get("stock"),
            "seller": raw.get("supplier"),
            "category_raw": raw.get("category"),
            "specs": raw.get("specs") or {},
            "image_urls": image_urls or [],
            "fetched_at": _now_iso(),
            "parser": parser,
            **({"raw_excerpt": raw_excerpt[:2000]} if raw_excerpt else {}),
        }

    def write_collect(self, batch_id: str, collect: dict, *, local_images: list[str]) -> Path:
        """写 collect 段（幂等规则 §3.3-3：重采更新 collect，decision/status 保留）。"""
        path = self.offer_dir(batch_id, collect["offer_id"]) / "product.json"
        product = {
            "schema": PRODUCT_SCHEMA,
            "batch_id": batch_id,
            "collect": collect,
            "decision": None,  # M2 定价接线前保持空段
            "media": {"local_images": local_images, "uploaded": []},
            "status": {"stage": "sourced", "goods_id": None, "item_id": None,
                       "history": [{"stage": "sourced", "at": collect["fetched_at"]}]},
        }
        if path.is_file():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old = {}
            if old.get("decision") is not None:
                product["decision"] = old["decision"]  # decision 不被重采覆盖
            if old.get("status"):
                old_history = (old["status"].get("history") or []) + [
                    {"stage": "re-collected", "at": collect["fetched_at"]}
                ]
                product["status"] = {**old["status"], "history": old_history}
        _write_json_atomic(path, product)
        return path

    def write_blocked(self, batch_id: str, offer_id: str, html: str, *, reason: str) -> Path:
        """风控壳 / 解析不出标题的件也落一份 product.json（stage=blocked）。

        不入库 Goods，但必须留痕：面板要能回答"这件为什么没有"，而不是凭空少一件。
        """
        collect = self.build_collect_section(
            offer_id=offer_id,
            title_raw="",
            price_cent=0,
            image_urls=None,
            raw=None,
            parser="none",
            raw_excerpt=html,
        )
        path = self.offer_dir(batch_id, offer_id) / "product.json"
        _write_json_atomic(
            path,
            {
                "schema": PRODUCT_SCHEMA,
                "batch_id": batch_id,
                "collect": collect,
                "decision": None,
                "media": {"local_images": [], "uploaded": []},
                "status": {
                    "stage": "blocked",
                    "goods_id": None,
                    "item_id": None,
                    "history": [
                        {"stage": "blocked", "at": collect["fetched_at"], "reason": reason}
                    ],
                },
            },
        )
        return path

    def read_product(self, batch_id: str, offer_id: str) -> dict:
        path = self.offer_dir(batch_id, offer_id) / "product.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def write_decision(self, batch_id: str, offer_id: str, decision: dict) -> Path:
        """decision 段写入（§3.3-2 只写一次的约束在 service 层用 force 控制）。

        collect 段原样保留；stage sourced→decided，history 追加。
        """
        path = self.offer_dir(batch_id, offer_id) / "product.json"
        product = json.loads(path.read_text(encoding="utf-8"))
        product["decision"] = decision
        status = product.get("status") or {}
        history = (status.get("history") or []) + [
            {"stage": "decided", "at": decision["decided_at"]}
        ]
        product["status"] = {**status, "stage": "decided", "history": history}
        _write_json_atomic(path, product)
        return path

    # ---------- 图片 ----------

    async def fetch_images(
        self,
        batch_id: str,
        offer_id: str,
        urls: list[str],
        *,
        http: httpx.AsyncClient | None = None,
    ) -> list[str]:
        """下载 ≤9 张 → 长边压 1440 → images/N.jpg（相对 product.json 的路径）。

        单张失败跳过不阻塞（collect.image_urls 仍保留完整远程清单可重跑）。
        """
        own_client = http is None
        client = http or httpx.AsyncClient(timeout=30.0, headers=_IMAGE_HEADERS)
        dest = self.offer_dir(batch_id, offer_id) / "images"
        dest.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        try:
            for i, url in enumerate(urls[:MAX_IMAGES]):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    name = await _save_image(resp.content, dest, i + 1)
                    if name:
                        saved.append(f"images/{name}")
                except Exception as exc:
                    logger.warning("1688 图片下载失败 offer_id=%s url=%s: %s", offer_id, url, exc)
        finally:
            if own_client:
                await client.aclose()
        return saved


_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://detail.1688.com/",
}


async def _save_image(content: bytes, dest: Path, index: int) -> str | None:
    """解码 → 长边压 1440 → 统一存 JPEG（闲鱼侧一律 image/png 上传声明不受影响）。"""

    def _process() -> str | None:
        try:
            img = Image.open(io.BytesIO(content))
        except Exception:
            return None
        img = img.convert("RGB")  # PNG/WEBP 带透明通道也归一为 JPEG
        if max(img.size) > MAX_EDGE:
            ratio = MAX_EDGE / max(img.size)
            img = img.resize((max(1, round(img.width * ratio)), max(1, round(img.height * ratio))))
        name = f"{index}.jpg"
        img.save(dest / name, "JPEG", quality=90)
        return name

    return await asyncio.to_thread(_process)


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
