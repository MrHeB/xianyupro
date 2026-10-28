"""把真号检索落下来的 mtop 响应体脱敏后落成单测 fixture。

用法（在 `backend/` 下跑；先临时开 `COLLECT_DEBUG_DUMP_PATH` 抓一次真号检索）：

    # 容器侧：COLLECT_DEBUG_DUMP_PATH=/app/data/search_debug，前端「只检索，不入库」跑一个关键词
    .venv/Scripts/python.exe scripts/make_1688_search_fixture.py \
        data/search_debug/20260920T005513228374Z  wireless_mouse

第二个参数是样本名（落到 `tests/fixtures/alibaba/search_mtop_<名>.jsonp`）——**手写，不要**
从关键词推导：关键词是用户输入，直接拼进文件名等于把输入当路径用。

为什么按键脱敏而不是整串替换：详情页那边（`make_1688_fixtures.py`）的账号痕迹在页面里有
转义 / 裸数字 / URL 编码三种写法，只能按值整串打；这份是**结构良好的 JSON**，键名可靠，
按键置灰既能保住结构（样本还得当"接口长这样"的证据），又不会漏掉同一键的不同写法。
被置灰的键**原样保留键名**——删键会让样本失去保真度。

为什么截断 items：一页 60 条 ≈ 780KB，全留会让单测仓库背一个巨大的样本，而断言只需要
"确切的 ID 集合"。`found`/`hasMore` **不跟着改**，保留整页原值——它们是接口给的事实，
跟着截断就成了假样本。
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collectors.alibaba.search_browser import harvest_offer_ids  # noqa: E402

# 身份 / 追踪凭证。测试里的 `DENY_KEYS` 是"这份样本实际命中了哪些"的子集。
REDACT_KEYS = frozenset(
    {
        "loginId", "memberId", "sessionId", "requestId", "pvid", "tpp_trace", "serverTrackId",
        "eurl", "impressionEurl", "wangwang", "nick", "nickname", "phone", "mobile", "email",
        "utdid", "cna", "cookie", "accessToken", "userId", "buyerId", "sellerId", "ownerId",
    }
)
PLACEHOLDER = "__REDACTED__"
KEEP_ITEMS = 6
OUT = Path("tests/fixtures/alibaba")


def _redact(node: object, hits: dict) -> None:
    if isinstance(node, dict):
        for key in list(node):
            if key in REDACT_KEYS:
                hits[key] = hits.get(key, 0) + 1
                # 值是对象时不能塞字符串占位——那会把整棵子树的结构改掉
                node[key] = PLACEHOLDER if not isinstance(node[key], (dict, list)) else {}
            else:
                _redact(node[key], hits)
    elif isinstance(node, list):
        for item in node:
            _redact(item, hits)


def build(src: Path, name: str, keep: int = KEEP_ITEMS) -> Path:
    raw = src.read_text(encoding="utf-8")
    # group(1) 故意允许前导空白：真响应就是 " mtopjsonp…({...})"，丢掉它样本就少了一层保真
    wrapped = re.match(r"^([^()]*?)\((.*)\)\s*$", raw, re.S)
    if not wrapped:
        raise SystemExit(f"{src} 不是 JSONP 包裹的响应体，接口外层格式变了——先去看抽取还能不能用")
    callback, payload = wrapped.groups()

    obj = json.loads(payload)
    hits: dict = {}
    _redact(obj, hits)
    if not hits:
        raise SystemExit("一个身份字段都没命中：要么接口改版，要么 REDACT_KEYS 该重看了")

    offer = obj["data"]["data"]["OFFER"]
    items = offer["items"]
    offer["items"] = items[:keep]

    out = f"{callback}({json.dumps(obj, ensure_ascii=False, separators=(',', ':'))})"
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / f"search_mtop_{name}.jsonp"
    dst.write_text(out, encoding="utf-8", newline="\n")

    # 样本必须能被**生产抽取代码**认出来，否则它只是个漂亮的文件，不是回归契约。
    ids = harvest_offer_ids(out)
    expected = [i["data"]["offerId"] for i in offer["items"]]
    if ids != expected:
        raise SystemExit(f"抽取结果与样本不符：{ids} != {expected}——OFFER_RES 与该接口对不上了")

    print(f"{src} -> {dst}（{len(out.encode('utf-8'))}B，items {len(items)} -> {len(offer['items'])}）")
    print(f"置灰计数: {hits}")
    print(f"keywords: {offer.get('keywords')!r}  found: {offer.get('found')!r}  hasMore: {offer.get('hasMore')!r}")
    print("把下面这几行贴进 tests/test_collect_search_s1.py 的 REAL_MTOP_IDS：")
    print(",\n    ".join(f'"{i}"' for i in ids) + ",")
    return dst


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    arg, sample_name = Path(sys.argv[1]), sys.argv[2]
    if arg.is_dir():
        bodies = sorted(arg.glob("*_mtop_*.txt"))
        if not bodies:
            raise SystemExit(f"{arg} 里没有 *_mtop_*.txt（本次检索没抓到目标接口响应？）")
        arg = bodies[0]
    build(arg, sample_name)
