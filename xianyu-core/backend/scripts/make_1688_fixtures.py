"""把真实详情页样本脱敏后落成单测 fixture。

为什么要脱敏：页面里嵌着 `window.FE_GLOBALS = { loginId: "…", clientIP: "…", userid=… }`，
那是**我们自己的货源账号**（卖家/商品侧字段本来就是公开的，保留）。parser 不读这些字段，
打码不影响它当回归样本。

为什么最后是整串替换：同一个账号痕迹在页面里有三种写法——JSON 转义（`\"loginId\":\"…\"`）、
裸数字（`"buyerUserId":3663…`）、URL 编码参数（`c%5flid%3d…`）。按键名写正则只能覆盖第一种。
"""

import re
import sys
from pathlib import Path

# 只打自己账号的痕迹
REDACTIONS = (
    (re.compile(r'(loginId\s*:\s*")[^"]*(")'), r"\g<1>REDACTED\g<2>"),
    (re.compile(r'(clientIP\s*:\s*")[^"]*(")'), r"\g<1>0.0.0.0\g<2>"),
    (re.compile(r'(clientDeviceId\s*:\s*")[^"]*(")'), r"\g<1>\g<2>"),
    (re.compile(r"(userid=)\d+"), r"\g<1>REDACTED"),
    (re.compile(r"(unb=)\d+"), r"\g<1>REDACTED"),
)

# 长串先替换，否则子串会先把它们拆碎
TOKENS = (
    "b2b-36636987700321f",
    "tb86348119",
    "3663698770",
    "114.86.99.168",
)

OUT = Path("tests/fixtures/alibaba")
OUT.mkdir(parents=True, exist_ok=True)

for src in sys.argv[1:]:
    p = Path(src)
    html = p.read_text(encoding="utf-8", errors="replace")
    for rx, rep in REDACTIONS:
        html = rx.sub(rep, html)
    for token in TOKENS:
        html = html.replace(token, "REDACTED")
    oid = p.stem.split("_")[-1]
    dst = OUT / f"detail_{oid}.html"
    dst.write_text(html, encoding="utf-8")
    leaks = [t for t in TOKENS if t in html]
    print(f"{p.name} -> {dst} ({len(html)}B) 残留敏感串={leaks or '无'}")
