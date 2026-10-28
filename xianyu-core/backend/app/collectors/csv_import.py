"""CSV 手动导入：采集/上架未就绪时的顶替通道（流程方案 阶段 4 顺序 5）。

前端交互与卡密导入一致（粘贴文本，不依赖文件上传）：
- 列约定（首行可为表头，自动识别）：title,price,image_urls[,description,source_offer_id]
  - price 单位「元」（人工粘贴源价更自然），内部转分；
  - image_urls 用分号分隔（URL 里不会出现分号）；
  - 空行 / # 注释行忽略；
- 导入产物为品类 B（人工履约），即采即转（clean_title/markup_price）入商品池
  pool_status=MAPPED，走同一套审核 → 上架流水线；
- 去重：同账号同来源同标题已存在则跳过（CSV 无强外部 ID，源 offer id 存 raw_payload）。
"""

import csv
import io
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.alibaba.transform import transform_product
from app.collectors.base import PoolStatus, SourcedProduct
from app.models import Goods

logger = logging.getLogger(__name__)

# 支持的列（按序）；表头行任意子集按名识别，无表头按前 3 列必须处理
_COLUMNS = ("title", "price", "image_urls", "description", "source_offer_id")


def parse_csv_text(text: str) -> list[SourcedProduct]:
    """解析粘贴的 CSV 文本 → SourcedProduct 列表（纯函数）。

    非法行（缺标题/价格非数字）跳过并记 warning，不中断整批。
    """
    reader = csv.reader(io.StringIO(text.strip()))
    rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    if not rows:
        return []

    # 表头识别：首行包含列名（大小写不敏感）则按名映射，否则按固定顺序
    header = [cell.strip().lower() for cell in rows[0]]
    if set(header) & set(_COLUMNS):
        idx = {name: i for i, name in enumerate(header) if name in _COLUMNS}
        rows = rows[1:]
    else:
        idx = {name: i for i, name in enumerate(_COLUMNS)}

    products: list[SourcedProduct] = []
    for lineno, row in enumerate(rows, start=2):  # 行号从 2 起（1 是表头/首行）
        cells = [cell.strip() for cell in row]

        def col(name: str) -> str:
            i = idx.get(name)
            return cells[i] if i is not None and i < len(cells) else ""

        if not col("title") or col("title").startswith("#"):
            continue
        try:
            price_cent = round(float(col("price")) * 100) if col("price") else 0
        except ValueError:
            logger.warning("CSV 第 %d 行价格非法，跳过：%r", lineno, col("price"))
            continue
        image_urls = [u for u in col("image_urls").split(";") if u] or None
        products.append(
            SourcedProduct(
                source="csv",
                source_offer_id=col("source_offer_id") or f"csv-l{lineno}",
                title=col("title"),
                description=col("description") or None,
                price_cent=price_cent,
                image_urls=image_urls,
            )
        )
    return products


async def import_csv_to_pool(
    db: AsyncSession, account_id: int, csv_text: str, *, rate: float = 1.3
) -> dict:
    """CSV 批量导入商品池（同账号同标题去重），返回 {added, skipped}。"""
    existing = set(
        (await db.execute(select(Goods.title).where(Goods.account_id == account_id)))
        .scalars()
        .all()
    )
    added = skipped = 0
    for product in parse_csv_text(csv_text):
        transformed = transform_product(product, rate=rate)
        if transformed.title in existing:
            skipped += 1
            continue
        existing.add(transformed.title)
        db.add(
            Goods(
                account_id=account_id,
                category="B",
                source=product.source,
                title=transformed.title,
                description=transformed.description,
                price_cent=transformed.price_cent,
                source_price_cent=product.price_cent,
                image_urls=transformed.image_urls,
                pool_status=PoolStatus.MAPPED.value,
                raw_payload={
                    "source_offer_id": product.source_offer_id,
                    "source_title": product.title,
                    "markup_rate": rate,
                },
            )
        )
        added += 1
    await db.commit()
    logger.info("CSV 导入完成 accountId=%d added=%d skipped=%d", account_id, added, skipped)
    return {"added": added, "skipped": skipped}
