# goofish-cli 工具文档（本项目取用清单）

> 版本：v0.1
> 日期：2026-09-18
> 来源锚点：`reference/goofish-cli` @ git `7d6a162c16c286cfc95a2c679b7e53d8d87cf0dc`（123 files，见 `reference/VERSIONS.txt`）
> 定位：**我们不安装、不运行 goofish-cli**。它是闲鱼 mtop 接口的"已验证契约"——参数名、默认值、DTO 字段、风控行为的权威参照。发布能力已自研复刻在 `xianyu-core/backend/app/listing/publisher.py`。
> 分工：接口协议细节（签名/Cookie/请求框）见 `docs/protocol.md`；本文只回答"**工具层有哪些能力、签名是什么、我们要不要**"。

---

## 0. 结论速览

1. 共 **17 条命令**。发布主链路那 4 条（`media.upload` → `category.recommend` → `location.default` → `item.publish`）**我们已经复刻完了**，不需要再依赖它。
2. 本次唯一**必须新抄**的是 `media.upload` 的**本地文件上传**语义——我们的 `publisher.upload_image()` 只接受 URL，而"本地文件夹"方案要求能上传磁盘文件。
3. **价格单位有坑**：goofish-cli 的 `item.publish` 入参是**元**（`price: float`），我们的 DTO 是**分**（`price_cent`，字符串）。任何照抄参数默认值的地方都要换算。
4. 它的 5 个 `SKILL.md` 是我们**自然语言层的现成范本**，尤其 `goofish-risk-guard` 的发布前违规扫描清单，直接可搬。
5. 它的写操作护栏是 **`write=True` 标记 + 熔断冷却 + `auth reset-guard` 手动解除**（`core/guard.py:42`）；我们目前只有令牌桶限速，**缺"熔断后停止并需人工解除"这一层语义**。
6. **goofish-cli 不含任何 1688 能力**（全仓 grep `1688` 零命中）。采集层与它无关。

---

## 1. 命令全景与取用决策

`strategy` 三档：`PUBLIC`（无需登录态）/ `COOKIE`（需 Cookie）。`write` 为 registry 的写操作标记。

| 命令（namespace.name） | 关键入参 | strategy | write | 底层接口 | 本项目取用 | 我们的实现位置 |
|---|---|---|:---:|---|---|---|
| `item.publish` | title, desc, images, price, original_price, delivery, post_price, can_self_pickup | COOKIE | ✅ | `mtop.idle.pc.idleitem.publish` v1.0 | **已复刻** | `listing/publisher.py:178,191` |
| `media.upload` | **path（本地文件）** | COOKIE | ✅ | `stream-upload.goofish.com/api/upload.api` | **已复刻但缺本地图支** | `listing/publisher.py:101` |
| `category.recommend` | title, images_json | COOKIE | — | `mtop.taobao.idle.kgraph.property.recommend` v2.0 | **已复刻** | `listing/publisher.py:132` |
| `location.default` | longitude, latitude | COOKIE | — | `mtop.taobao.idle.local.poi.get` v1.0 | **已复刻** | `listing/publisher.py:160` |
| `item.list` | limit=50 | COOKIE | — | `mtop.idle.web.xyh.item.list` | 备用（在售核对） | 待接 |
| `item.get` | item_id | COOKIE | — | 同上族 | 备用（发布后回执核对） | 待接 |
| `item.delete` | item_id | COOKIE | ✅ | — | 二期（自动下架需要时） | 无 |
| `item.view` | item_id | COOKIE | — | 浏览器路径（Playwright） | ❌ 不用 | 走 HTTP 替代 |
| `search.items` | query, limit, pages | COOKIE | — | 浏览器路径（Playwright） | ⚠️ 竞品定价要数据源时再评估 | 无 |
| `auth.login` | source, --browser, --raw, --qr | PUBLIC | — | — | ❌ 不引入其单账号文件态 | DB 多账号 + 三轨保活（`protocol.md §2.3`） |
| `auth.status` | — | COOKIE | — | — | ✅ 概念需要（登录态校验） | 已有 session 校验 |
| `auth.reset-guard` | — | PUBLIC | — | — | ✅ **建议抄这层语义** | 缺（只有令牌桶） |
| `message.send` | cid, text/image | COOKIE | ✅ | IM | ❌ 已有自有实现 | `xianyu_ws/` |
| `message.list-chats` | fetch_num, watch_secs | COOKIE | — | `session.sync` + WS | ❌ | `xianyu_ws/` |
| `message.history` | cid, limit_per_page | COOKIE | — | IM | ❌ | 自有 |
| `message.watch` | — | COOKIE | — | IM 长连接 JSONL | ❌ 常驻模型不适合我们 | `xianyu_ws/` |
| `skills.install` | --dest | PUBLIC | — | — | 只抄内容不抄安装 | 手写 Skill |

> 判定：**发布域**要它当契约、**消息域**完全不用（我们的 WebSocket 实现更完整）、**认证域**只借语义不借实现。

---

## 2. 发布四步链路：参数契约

### 2.1 `media.upload(path)` —— 本地文件 → 闲鱼 CDN（★ 本次必抄）

源码事实（`commands/media/upload.py:19-51`）：

- 入参是**本地路径**，`os.path.expanduser(path)` 支持 `~`，不存在直接 `FileNotFoundError`；
- 端点 `https://stream-upload.goofish.com/api/upload.api`；
- query：`floderId=0`、`appkey=xy_chat`、`_input_charset=utf-8`；
- headers 固定带 `origin: https://www.goofish.com` + `referer: https://www.goofish.com/` + 统一 UA；
- multipart 字段名 `file`，文件名取 `os.path.basename`，**MIME 一律声明 `image/png`**（不管实际是不是 jpg）；
- 返回 `{url, width, height, size}`，宽高从 `object.pix` 的 `"WxH"` 串解析。

我们的差距：`publisher.py:101 upload_image(self, image_url: str)` 内部 `http.get(image_url)` 下载后 POST，**只吃远程 URL**。本地文件夹里的 1688 原图上传不了。

改造方案见需求设计文档 §8 修复项 3。注意保持一致点：声明 `image/png` 这条不要"顺手修正"成真实 MIME——闲鱼侧对 jpg 声明 png 已验证可过，改成真实类型反而引入未知风险。

### 2.2 `category.recommend(title, images_json="[]")`

返回列：`cat_id / cat_name / channel_cat_id / tb_cat_id / confidence`。
`images_json` 是**已上传图片 URL 的 JSON 字符串**，不是路径列表——顺序是先传图、再推类目。我们已在 `publisher.py:132` 实现，`confidence` 字段可用于低置信时回落人工选择。

### 2.3 `location.default(longitude=121.4737, latitude=31.2304)`

返回 `prov / city / area / poi / division_id`。默认经纬度是**写死的上海**——这是"就近解析一个地址"的兜底，不代表发布地。我们 `publisher.py:160` 已实现；多店铺场景需按账号配置真实坐标，否则全店同城同址。

### 2.4 `item.publish(...)` 完整签名

```
publish(
    title: str,
    desc: str,
    images: list[str],                       # 已上传的 CDN URL
    price: float,                            # ★ 单位：元
    original_price: float | None = None,     # 划线原价
    delivery: Literal["包邮","按距离计费","一口价","无需邮寄"] = "无需邮寄",
    post_price: float = 0,                   # 运费，单位元
    can_self_pickup: bool = True,
) -> {item_id, title, price, cat_name, ok}
```

标题+图片进来后它内部**自动**跑 2.1→2.3（描述里写的"自动识别类目 + 默认地址"），即四步是一体的。

运费 DTO 三态（`_build_publish_data`，`commands/item/publish.py:112-128`）：

| delivery | 生成字段 |
|---|---|
| 包邮 | `canFreeShipping=True`, `supportFreight=True` |
| 一口价 | `supportFreight=True`, `templateId="-100"` |
| 带运费 | `supportFreight=True`, `postPriceInCent=str(int(post_price*100))`, `templateId="0"` |
| 其他 | `templateId="0"` |

图片项 DTO（同文件 `:100-108`）：`extraInfo:{isH:false,isT:false,raw:false}`、`isQrCode:false`、`url`、`heightSize`、`widthSize`、`major:true`、`type:0`、`status:"done"`。
→ 与我们 `app/listing/dto.py` 逐字段对齐，**注意 `major` 恒 true 是它的写法**，多图时是否只首图 major 值得实测。

---

## 3. 登录态与 Cookie（仅对照，不引入）

- 状态存单文件 `~/.goofish-cli/cookies.json`，**单账号**，与我们多账号 + DB 存储模型不兼容；
- Cookie 关键字段校验：缺 `unb` / `_m_h5_tk` 直接报错——**`_m_h5_tk` 是 mtop 签名必需**，与 `protocol.md §1.2` 一致；
- 四种导入路径：本机浏览器自动抽取（Chrome/Edge/Brave/Safari/Firefox/LibreWolf/Arc）、`--browser` 指定、`"k=v; k=v"` + `--raw` 粘贴、`--qr` 扫码兜底；
- `auth login --qr` 存在的意义：**换机/首次/免密失效**时才需要人工扫码，其余全自动。这给我们"Cookie 过期后如何恢复"的产品交互提供了参照——我们应当有等价的"重新扫码"入口，而不是让用户去改配置。

---

## 4. 写操作与风控护栏

- 带 `write=True` 的仅 4 条：`item.publish`、`item.delete`、`media.upload`、`message.send`。**这个分类本身值得抄**：MCP 工具层就该按读/写/破坏性分级（见需求设计文档 §5）。
- `core/guard.py:42`：触发风控后进入熔断，拒绝执行并提示"剩余 Ns，可通过 `goofish auth reset-guard` 手动解除"。
- 我们现状：`api_client` 内置写操作令牌桶（1 次/分钟）+ `auto_listing_task` 每轮 5 条 + 10–30s 抖动（`scheduler/task_runner.py:182`）。**有节流、无熔断**。
- 取用建议：连续 N 次写失败 → 置熔断位 → 后续写操作直接拒绝并提示人工 `reset-guard` 等价命令。熔断是"防止把号练死"的关键，比限速更重要。

---

## 5. 可直接复用的 Skill 资产

路径 `reference/goofish-cli/skills/`，共 5 个：`goofish-overview`、`goofish-publish-item`、`goofish-reply-buyer`、`goofish-risk-guard`、`goofish-shop-diagnosis`。

| Skill | 对我们的价值 |
|---|---|
| `goofish-publish-item` | **上架工作流编排范本**：raw 描述 → 确认类目 → 生成标题候选 → 风控预检 → 批量传图 → 取默认地址 → 发布。我们 Skill 的骨架照这个抄。 |
| `goofish-risk-guard` | **发布前扫描清单**：绝对化词、类目偏差、降价改标、9 图合规、外联词、误导承诺、RGV587 后的恢复指引。自研文案模块直接搬这份检查表。 |
| `goofish-shop-diagnosis` | 经营看板/复盘参照。 |
| `goofish-reply-buyer` | 与自有 `ai/reply_engine.py` 对照话术。 |

它的两条硬约束，我们**必须保留**：

1. `goofish-publish-item`：「**发送前必须让用户确认文案，不自动提交**」，且发布前调 `goofish-risk-guard` 预检。
2. `goofish-risk-guard`：「本 skill 主要是知识库 + 检查清单，**不直接替用户改文案**」——检查与改写分离，由调用方决定怎么改。

### MCP 工具命名与返回信封（建议对齐）

`mcp_server.py:54`：`tool_name = f"{namespace}_{name}".replace("-", "_")`
→ 产出 `item_publish`、`media_upload`、`category_recommend`、`location_default`、`auth_status`、`item_list`、`search_items`。

返回统一信封（`mcp_server.py:56-65`）：成功 `{"ok": true, "data": ...}`；失败 `{"ok": false, "error_type": ..., "message": ...}`。

**我们的 MCP server 沿用同一命名和同一信封**，这样它这 5 个 Skill 可以近乎零改动直接跑在我们自建 MCP 上。

---

## 6. 明确不用的部分

| 能力 | 不用原因 |
|---|---|
| `search.items` / `item.view` | 走 Playwright 浏览器路径，重、脆、需常驻；我们的竞品数据另有来源（见需求设计文档 §7.2 待决 Q1） |
| `message.*` 四条 | 自有 `xianyu_ws/` 实现更完整（付款事件→发货触发） |
| `auth.login` 单文件 Cookie | 与多账号 DB 模型冲突 |
| `skills.install` | 只抄 Markdown 内容，不引入它的安装机制 |
| 1688 相关 | **它没有**，采集层零可复用 |

---

## 7. 复现这份文档

```bash
cd reference/goofish-cli && git rev-parse HEAD   # 7d6a162...
# 命令元数据：src/goofish_cli/commands/**（@command 装饰器 + 函数签名）
# MCP 注册规则：src/goofish_cli/mcp_server.py:36-70
# 写操作护栏：src/goofish_cli/core/guard.py
# Skill 正文：skills/*/SKILL.md
```
