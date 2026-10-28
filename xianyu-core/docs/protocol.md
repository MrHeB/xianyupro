# 闲鱼自动化 —— 协议文档（protocol.md）

> 版本：v0.1（阶段 2 产出，基于 5 个参考项目源码盘点交叉验证）
> 状态：草稿 · 待实测验证
> ⚠️ 本文档为**逆向协议整理**，非官方文档。接口名/字段/签名随时可能随闲鱼前端改版失效——**凡是未实测的项都标注了 [待验证]**。
> 参考来源：goofish-cli(协议层最全)、xianyu-pilot 旧版、xianyu-pilot-pro（交叉验证）、FishClaw_MCP(RPA 兜底)、pds-tool(1688 转换，独立部分见文末)。

---

## 0. 阅读导览

| 章节 | 内容 | 自建落地优先级 |
|------|------|--------------|
| §1 基础约定 | 端点/签名/公共参数/错误码/风控 | ★★★ 必须先做 |
| §2 登录态 | 扫码登录、Cookie 存储、三轨保活 | ★★★ 第一步 |
| §3 商品接口 | 发布/上下架/编辑/类目/详情/在售列表 | ★★★ |
| §4 媒体上传 | 图片上传到闲鱼 CDN | ★★★ |
| §5 订单接口 | 已售订单/发货确认/退款 | ★★（品类 A 用） |
| §6 搜索接口 | 站内搜索（商机选品用） | ★（品类 B 边缘） |
| §7 IM 协议 | WebSocket 收发消息/付款事件 | ★★★（消息+发货都靠它） |
| §8 数据模型 | 账号/商品/订单/消息/会话 | ★★★ 建表蓝本 |
| §9 依赖边界 | 哪些要 Cookie/签名/浏览器 | ★★ 架构约束 |
| §10 1688 侧 | pds-tool 采集转换（独立） | ★★ 品类 B |
| §11 时效与风险 | 已知坑、验证清单 | ★★★ |

---

## 1. 基础约定

### 1.1 HTTP 网关（mtop H5 风格）

- **基址**：`https://h5api.m.goofish.com/h5/{api}/{version}/`
- **方法**：POST，body 为表单 `data=<URL编码的紧凑JSON>`
- **公共 query 参数**：

| 参数 | 值 | 说明 |
|------|-----|------|
| jsv | 2.7.2 | |
| appKey | `34839810` | 固定 |
| t | 真实毫秒时间戳 | `int(time.time()*1000)`，**不要抹零** |
| sign | 见 §1.2 | |
| v | 1.0（部分接口 2.0/3.0/4.0，见各接口） | |
| type | originaljson（部分接口 json） | |
| accountSite | xianyu | |
| dataType | json | |
| timeout | 20000 | |
| api | {api} | |
| sessionOption | AutoLoginOnly | |
| spm_cnt | 各接口不同 | 部分接口带 |

- **HTTP headers**：全套 Chrome UA + `Referer: https://www.goofish.com/`（鱼小铺接口用 `seller.goofish.com`）+ `Cookie` + `Origin` + `sec-ch-ua`。

### 1.2 签名算法（★ 最核心，已从三处源码交叉验证一致）

```
sign = md5( token + "&" + t + "&" + appKey + "&" + data )
其中：
  token  = Cookie _m_h5_tk 中第一个 "_" 之前的部分
  t      = int(time.time() * 1000)   # 真实毫秒
  appKey = "34839810"
  data   = json.dumps(data_map, separators=(",", ":"))   # 紧凑序列化，字段序敏感！
```

要点（每个都是实战踩过的坑）：
1. **data 必须紧凑 JSON 且字段序敏感**——任何排序/加空格都会导致签名失败；
2. **token 前置条件**：`_m_h5_tk` 需要先通过"空 token 请求"触发服务端 Set-Cookie 下发（探活），否则无 token 可用；
3. `_m_h5_tk` 有效期约 **10 分钟**，长跑必须做自动续期（见 §2.3）；
4. 无 salt、无二次混淆，比想象简单。

### 1.3 返回结构与错误码

- 返回：dict（非类），`{success, data|error, ret}`；以 `ret[0]` 是否以 `SUCCESS` 前缀判成功。
- 错误码分类（自建请求层必须按此分派）：

| 错误特征 | 含义 | 处理策略 |
|---------|------|---------|
| `RGV587_ERROR` / `FAIL_SYS_USER_VALIDATE` / `哎哟喂` / `punish` / `baxia` | **风控/滑块** | 不可重试 → 进人工/滑块流程；IP 熔断 |
| `FAIL_SYS_SESSION_EXPIRED` | 会话过期 | 触发免密续期（§2.3） |
| `FAIL_SYS_TOKEN_EXOIRED`（**服务端真实拼写**） | token 过期 | 从响应 Set-Cookie 提取新值，重试 1 次 |
| `FAIL_SYS_ILLEGAL_ACCESS` | 风控层非法访问 | **不可自动救**，停手 |
| `FAIL_BIZ_*`（如 `FAIL_BIZ_SKU_PRICE_ILLEGAL`） | 业务参数错误 | 本地先校验防触发 |
| `NOT_FOUND` / `不存在` | 商品不存在 | 记录，跳过 |
| `FAIL_SYS_SERVICE_INNER_FAULT::服务内部故障` | **服务端抛异常**，但闲鱼对部分接口**根本不做参数校验**——请求体形状不对（如 §3.4 把 `imageInfos` 传成 URL 字符串数组）也回这一条 | **别当成"平台挂了"**：先照参考实现逐字段对 body 形状，一次只改一个假设（这类调用在发布链路内，失败照样占写失败止损额度） |
| HTTP 超时/网络异常（`ConnectError` / `TimeoutException` / 响应体非 JSON） | 请求多半**没送达**闲鱼 | 指数退避重试（可恢复白名单）。⚠️ 实现要求：传输层异常**必须**在请求层折算成 `kind=network` 再抛，冒到路由外就是裸 500＋一屏栈；本项目口径——只读调用自动补发 1 次，**写调用绝不补发**（断连≠没生效，重发＝重复发布），且 network 类**不计入风控手册 §4 R-2 的写失败止损额度**。 |
| `LOCAL_THROTTLE`（**本项目自己的**令牌桶没排到号，非闲鱼返回） | 请求**根本没出门** | 回 `kind=local_throttle`、HTTP `429`，等 `wait_seconds` 后重试；与 network 同口径：不占写失败止损额度、不改 `pool_status`（真机 2026-09-20：折成 unknown 让两次本地排队吃掉 2/3 次熔断额度） |

### 1.4 风控友好请求框架（自建必抄）

1. **写操作限流**：令牌桶 **1 次/分钟**（发布/发货/回复等写操作），跨进程共享状态；⚠️ 令牌桶拒绝是**本项目自己的**排队，必须用独立错误类别（`local_throttle`）表达，绝不能混进"平台写失败"——否则网络/队列毛刺三次就把自己止损 24h；
2. **熔断**：命中 RGV587 → 熔断 10 分钟拒绝后续请求；
3. **错误分层**：可自动恢复（TOKEN/SESSION 过期）vs 不可恢复（ILLEGAL_ACCESS）分开处理；
4. **人性化节奏**：随机延迟、逐张图片间隔 0.3-0.8s、同账号串行（asyncio.Semaphore(1)）。

---

## 2. 登录态

### 2.1 扫码登录（纯 HTTP 逆向三阶段）

> 参考：xianyu-pilot/pro `xianyu_qr_login.py`。会话在进程内存，多 worker 需换 Redis。

1. **取 `_m_h5_tk`**：两次 POST h5api 首页 mtop 触发 Set-Cookie；
2. **取登录参数**：GET `https://passport.goofish.com/mini_login.htm`，正则提取 `window.viewData → loginFormData`（补 `umidTag` 等）；
3. **生成二维码**：`qrcode/generate.do`（newlogin 域）→ `codeContent` → 渲染 PNG 给用户扫；
4. **轮询状态**：状态机 `new/scaned/confirmed/expired/cancelled/verification_required`，确认后取 Cookie 落库。

设计要点：会话归属 owner + 容量上限（每用户≤5/总≤1 万）+ 幂等轮询 + Set-Cookie 头合并补全 `havana_lgc2_77` 等字段。

### 2.2 Cookie 存储与加密

- 三件套判据：`unb`（用户 ID）+ `_m_h5_tk` + `cookie2` 齐全才算有效会话。
- 加密：**AES-256-GCM**，格式 `enc:v1:{b64url(iv)}:{b64url(ct+tag)}`，密钥 = SHA-256(`COOKIE_CRYPTO_SECRET`)。
- 表设计（账号 5 表家族，见 §8.1）：主数据/认证密文（encrypted_cookie/encrypted_token/cookie_status）/运行态/健康快照/会员。

### 2.3 三轨保活（★ 每个账号三个独立刷新节奏）

| 轨道 | 间隔 | 接口 | 失败语义 |
|------|------|------|---------|
| Cookie 保活 | 30min | POST `passport.goofish.com/newlogin/hasLogin.do` | 不判死，仅记录 |
| `_m_h5_tk` | 1.5–2.5h 随机 | mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get | 过期→重取 |
| WS accessToken | 10–14h 随机 | mtop.taobao.idlemessage.pc.login.token | 过期→重取 |

- 调度范围含 `cookie_status=1`（正常保活轨，30min）与 `=0`（**低频自恢复轨，15min**）；账号间 2-5s 抖动 + 每账号锁；
- 风控分级：RGV587/滑块 → 只入滑块队列**不误标失效**；`SESSION_EXPIRED`/登入失败 → 才置 cookie_status=0 + 通知 + 停 WS；
- ⚠️ **hasLogin 的「登录态已失效」文案不是终态判据**（真机 2026-09-20：11:32:51 同一份 Cookie 刚 `publish` 成功，11:34:13 就被 hasLogin 标 0）。置 0 前必须过一次**只读 mtop 复核**——`mtop.taobao.idlemessage.pc.loginuser.get` v1.0（只读、必须带登录态、体积极小，WS 侧本就 600s 打一次）：只有它也回 `FAIL_SYS_SESSION_EXPIRED` 才置 0；网络抖动/风控/未识别错误一律「未判定」，**不动状态**（宁可少标一次，也不要造一个不可逆的 0）。
- **误标恢复循环**：`cookie_status=0` 的号不再打 hasLogin（它就是误标来源），只按 15min 复核；复核通过 ⇒ 自动回 1 **并把会话重挂回内存会话池**。少了后半句的话，一次误标只能靠人工重新扫码解，且 `auto_listing` 每轮都会因会话池里没有该账号而整域跳过发布（重启 backend 后必现）。

---

## 3. 商品接口

### 3.1 发布（普通号单规格）

- API：`mtop.idle.pc.idleitem.publish` v1.0 [待验证]
- 前置三步：①刷新 `_m_h5_tk` → ②类目推荐（§3.4）→ ③图片传 CDN（§4）逐个拿 URL。
- 请求体 DTO（自建照抄模板）：

```jsonc
{
  "imageInfoDOList": [{ "url": "<CDN图>", "major": true, "type": 0, "status": "done",
                        "isQrCode": false, "heightSize": 0, "widthSize": 0,
                        "extraInfo": { "isH": "false", "isT": "false", "raw": "false" } }],
  "itemTextDTO": { "title": "...", "desc": "...", "titleDescSeparate": true },
  "itemPriceDTO": { "priceInCent": "1990",          // 元×100，转字符串！
                    "origPriceInCent": "2990" },     // 可选，划线价
  "itemPostFeeDTO": { /* 四态选一，见下 */ },
  "itemAddrDTO": { "prov": "", "city": "", "area": "", "divisionId": "",
                   "gps": "lng,lat", "poiId": "", "poiName": "" },
  "itemCatDTO": { "catId": "", "catName": "", "channelCatId": "", "tbCatId": "" },
  "itemSkuList": [],                 // 单规格空数组也行（pro 也构造空 property）
  "quantity": "1",
  "itemTypeStr": "b",
  "simpleItem": "true",
  "onlyTakeSelf": true,
  "freebies": false,
  "userRightsProtocols": [],
  "sourceId": "", "bizcode": "", "publishScene": "pcMainPublish",
  "uniqueCode": "1775897582791680"    // 硬编码常量 [待验证]，勿改成随机值
}
```

- **运费四态**（itemPostFeeDTO）：

| shippingMode | 语义 | 字段 |
|-------------|------|------|
| 包邮 | 卖家包邮 | `canFreeShipping:true, supportFreight:true` |
| 按距离 | 闲鱼计费 | `supportFreight:true, templateId:"-100"` |
| 一口价 | 固定运费 | `postPriceInCent: 运费×100, templateId:"0"` |
| 无需邮寄（**虚拟商品用**） | 虚拟交付 | `templateId:"0"`，配合虚拟类目 catId |

- **虚拟商品关键**：虚拟交付 = `shippingMode=none（templateId:"0"）+ 虚拟类目`（pro 默认 catId 50025461 软件类 [待验证]）；可议价无显式入参。

### 3.2 发布（鱼小铺多规格，可选二期）

- API：`mtop.idle.pc.backend.idleitem.publish|edit|editdetail`（Origin: seller.goofish.com）
- 相对单规格增加：`itemProperties`（≤2 组，1 组可 supportImage）、`propertyImageList`、多 SKU `itemSkuList[{priceInCent, quantity, propertyList}]`（笛卡尔积组合）；顶层 quantity=Σ、price=min。
- **双层序列化**：内部对象 → JSON → `{inputJson: 该JSON}` → 再序列化，签名用同一串 [待验证]。
- SKU 回填：按 `property_key` 乱序匹配响应中的 skuId/inventoryId。

### 3.3 商品管理

| 操作 | API | 版本 | 说明 |
|------|-----|------|------|
| 下架（普通号） | mtop.taobao.idle.item.downshelf | 1.0 [待验证] | |
| 删除（普通号） | com.taobao.idle.item.delete | 1.0 [待验证] | |
| 下架（鱼小铺） | mtop.alibaba.idle.seller.pc.item.offline | — | |
| 删除（鱼小铺） | mtop.alibaba.idle.seller.pc.item.delete | — | |
| 改价（鱼小铺） | mtop.alibaba.idle.seller.pc.item.info.update | — | |
| 商品详情 | mtop.taobao.idle.pc.detail | 1.0 | 富字段在 itemDO/sellerDO/itemLabelExtList |
| 在售列表 | mtop.idle.web.xyh.item.list | 1.0 | userId=unb、pageSize 20、cardList[].cardData |

### 3.4 类目推荐（AI 服务端）

- API：`mtop.taobao.idle.kgraph.property.recommend` v2.0
- 入参：`{title, lockCpv:false, multiSKU:false, publishScene:"mainPublish", scene:"newPublishChoice", description:title, imageInfos:[…], uniqueCode:"1775905618164677"}`
- **`imageInfos` 是图对象数组，不是 URL 字符串数组**（2026-09-20 真号首跑定位；两个参考实现一致：goofish-cli `commands/category/recommend.py`、xianyu-pilot `services/auto_category.py`）。每项与发布体的 `imageInfoDOList` 同构：`{url, major, type:0, status:"done", isQrCode:false, widthSize, heightSize, extraInfo:{isH:"false", isT:"false", raw:"false"}}`。
- ⚠️ 形状不对时 mtop **不给参数校验错**，而是回 `FAIL_SYS_SERVICE_INNER_FAULT::服务内部故障`——看着像闲鱼挂了，实则我们的 body 不对；`imageInfos: []`（无图）同样故障。该调用虽只读，但位于发布链路内，失败照样计入 `publish` 域写失败额度（3 次 → 24h 熔断），所以这条链排错必须一次只改一个假设。
- 出参：`data.categoryPredictResult` → catId/catName/channelCatId/tbCatId/confidence
- 兜底链：图分类 API → AI 标题分类 → 手动/默认（pro 有 3.4 万行类目树 JSON `categories.json`）。本项目**不做**自动兜底：类目猜错＝红线（`category_mismatch`），宁可发布失败也不静默换类目。

### 3.5 发布所在地（地址）

- API：`mtop.taobao.idle.local.poi.get` v1.0（只读）
- 入参：`{}` 即可。**2026-09-20 真机：传 `{}`、传 float 经纬度、传字符串经纬度，三种返回完全一致**——参考实现（goofish-cli `location/default.py`）带的 `longitude/latitude` 对本接口无影响。
- 出参（**真机实测，与旧版本文档相反**）：`data` 顶层**只有 `selectedPoi` 一个键**，`commonAddresses` 在此账号上**根本不存在**。字段名与类型：
  `prov/city/area: str`、`divisionId: int`（如 310104）、`poi: str`（POI 名，**不是 `poiName`**）、`poiId/aoiId: str`、`aoi: str`、`isAoi: bool`、`latitude/longitude: str`（**没有 `gps` 字段**，要自己拼）。
- ⇒ 旧实现「`commonAddresses[]` 里逐条找 `selectedPoi`」必然恒空，会把"账号其实有地址"误报成「账号无默认收货地址」。取地址的正确形状：`data.selectedPoi or (data.commonAddresses or [None])[0]`。
- 发布体的 `itemAddrDTO` 需要 prov/city/area/divisionId/gps/poiId/poiName 全套；`gps` 拼法是 **`经度,纬度`**（goofish-cli `item/publish.py`，其测试锁在 `"116.4,39.9"`）；`divisionId` 服务端给 int、DTO 要 str。参考库：pro `china_address_dict`（adcode/division_id=闲鱼字段）。
- ⚠️ **没地址不是失败条件**：goofish-cli 在无 `division_id` 时把 `itemAddrDTO` 传成空对象 `{}`，发布照样成功。所以本项目"取不到地址"→ 记 warning + 空所在地继续发，**不抛错**（抛错＝白占一次 publish 写失败额度，见 §4 R-2 与手册）。
- 回归 fixture：`backend/tests/fixtures/xianyu/poi_get.json`（真机结构，门牌/POI 名/坐标已换示例值），单测锁"顶层 selectedPoi + poi→poiName + 经纬度拼 gps + int divisionId"。

---

## 4. 媒体上传

- **端点**：`https://stream-upload.goofish.com/api/upload.api`（非 mtop、无签名）
- **鉴权**：复用登录态 Cookie（requests.Session），无独立字段
- **请求**：POST multipart，`files={"file": (basename, f, "image/png")}`（**一律声明 image/png**）；query `floderId=0&appkey=xy_chat&_input_charset=utf-8`；headers 带 origin/referer=www.goofish.com + UA；timeout 60s
- **返回**：`object.{url, pix:"WxH", size}` → 解析 width/height
- **约束**：只支持图片（无视频）；≤1920²/JPEG/5MB；发布图 ≤9 张、首图 1:1、≥3 张（skill 层建议）
- **上传后拿 CDN url 喂给发布 payload 的 imageInfoDOList**

---

## 5. 订单接口

| 接口 | API | 入参要点 | 出参 |
|------|-----|---------|------|
| 已售订单分页 | mtop.taobao.idle.trade.merchant.sold.get | `{needGroupInfo, pageNumber, pageSize, lastEndRow, userId}` | 订单列表 |
| 退款列表 | mtop.taobao.idle.merchant.refund.list | `{queryType:"refund", refundSearchParam:{disputeStatus, queryCode}}` | 退款单 |
| 确认发货（普通） | mtop.taobao.idle.logistic.consign.dummy | `{orderId, tradeText, picList:[], newUnconsign:true}` | — |
| 确认发货（小刀/免邮） | mtop.idle.groupon.activity.seller.freeshipping | `{itemId, buyerId}`（强转 int） | — |
| 同意退款 | mtop.taobao.idle.merchant.agree.refund [待验证] | 需校验 rightVO 存在 agreeRefundApply 按钮 | — |

- **订单状态映射**（平台文案 → 本地）：0待付款/1已付款/2待发货/3已发货/4已完成/5已关闭；
- 小刀单检测：从按钮文案判断（`_detect_bargain_from_buttons`）；
- **订单 ID 是超 bigint 的大整数，数据库必须 VARCHAR 存储**（§8）。

---

## 6. 搜索接口（商机/选品用，边缘）

- 快速：`mtop.taobao.idlemtopsearch.pc.search`（带 Cookie + 签名；先 `_warmup_session` 预热）[待验证]
- 慢速兜底：crawler 浏览器搜索（无公开 API 时用，DOM 易碎）
- 出参字段：title/price/imageUrl/itemId/seller/area/soldCount/wantCount/description
- 无状态（回内存列表，不落库）

---

## 7. IM WebSocket 协议（★ 消息 + 发货触发都靠它）

### 7.1 连接

- **URL**：`wss://wss-goofish.dingtalk.com/`
- 每账号一条连接 + 单接收循环串行；心跳 15s；重连退避 5→300s 指数。

### 7.2 帧类型（lwp 路由）

| lwp | 用途 |
|-----|------|
| `/reg` | 注册鉴权帧 |
| `/!` | 心跳 / ACK |
| `/r/SyncStatus/ackDiff` | 增量同步（长轮询，pts 游标） |
| `/r/MessageSend/sendByReceiverScope` | 发送消息（文本/图片） |
| `/r/SingleChatConversation/create` | 无会话时建会话 |
| `/r/MessageManager/listUserMessages` | 拉历史消息 |
| `/s/para` `/s/sync` `/s/vulcan` | 服务端推送包 |

### 7.3 注册帧（/reg）

```jsonc
{
  "lwp": "/reg",
  "headers": {
    "cache-header": "...",
    "app-key": "444e9908a51d1cb236a27862abc769c9",   // IM appKey
    "token": "<accessToken>",                          // mtop.taobao.idlemessage.pc.login.token 取的
    "did": "<UUID>-<unb>",                             // device_id 格式
    "dt": "j", "wv": "im:3,au:3,sy:6", "mid": "..."
  }
}
```

### 7.4 增量同步与 ACK

- 同步：`{"lwp":"/r/SyncStatus/ackDiff", ...}`，pts=微秒时间戳游标；收到包后更新 maxPts/maxHighPts 并立刻发下一轮；
- ACK：`{"lwp":"/!","body":{"pnmId":...},"code":200}`——**先 ACK 再处理**；
- 心跳：`/!` 空 body，15s 一次。

### 7.5 发送消息

```
lwp: /r/MessageSend/sendByReceiverScope
body: [message_body, {"actualReceivers": [to_id, from_id]}]
⚠️ actualReceivers 必须包含发送者自己，否则 code=500
```

- message_body：`content.contentType=101` + `custom: {type:1(text)/type:2(image), data: base64(JSON)}`
- 内层 contentType：1 = text.text，2 = image.url + pics[]
- cid/to/from 格式：`xxx@goofish`
- 图片必须是**闲鱼图床 URL**（WS 协议不支持裸传外部图）

### 7.6 收包解码与消息字段

- 推送包 Base64 + MessagePack（三层解码：明文 JSON → base64(JSON) → JS decrypt(base64+msgpack)）；
- 规范化字段：`pnmId/sId/senderUserId/senderUserName/msgContent/contentType/messageTime/reminderContent/reminderUrl/xyGoodsId/imageUrls`
- **contentType 语义**：1文本 / 2图片 / 25已拍 / **26付款** / 28发货 / 32已读（+ 卡片、轻量变体）

### 7.7 付款事件 → 发货触发（★ 品类 A 自动发货核心信号）

- 付款消息判定：contentType=26 且文案含"已付款/等待你发货"等正关键词，且未命中负向（"待付款/拍下待付"）；
- ⚠️ 关键词硬编码在代码里，闲鱼文案一变即失效——自建做成**可配置正则表**；
- 节流：同账号同 pnm_id 60s 一次（防周期性推送重复发货）。

---

## 8. 数据模型（自建建表蓝本，来自 pro schema + 旧版交叉）

### 8.1 账号（5 表家族）

| 表 | 关键字段 |
|----|---------|
| account | id, tenant_id, user_id, external_uid(=unb), status, fish_shop_user, proxy_* |
| account_auth | encrypted_cookie, encrypted_token(_m_h5_tk), cookie_status(0待校验/1正常/2过期), ws_token, last_login_* |
| account_runtime | online/ws/cookie 状态, last_heartbeat_time, ws_latency_ms |
| account_health_snapshot | health_score, api_success_rate, avg_response_ms |
| account_membership | level, expired_time |

### 8.2 商品

- `goods`：external_goods_id(VARCHAR), title, price, image_urls(JSON), raw_payload(JSON 快照), status(1在售/0下架/2已售), auto_reply_enabled, auto_relist_enabled, original_quantity, next_relist_goods_id, has_snapshot
- `goods_sku`：property_key(VARCHAR(512)) + uk(external_goods_id, property_key), price_in_cent, quantity
- `goods_edit_snapshot`：snapshot_json（全量发布快照，source=publish/edit/detail_api/relist）

### 8.3 订单

- `trade_order`：**order_id + external_order_id 双 VARCHAR(120)**（防 bigint 溢出静默 NULL！）, buyer_id/buyer_name, total_amount(分), order_status(0-5), is_bargain, is_unregistered
- `trade_order_item`：明细

### 8.4 消息与会话

- `chat_message`：pnm_id/message_uid（幂等唯一）, content_type(1文本/2图片), direction(IN/OUT), is_auto_reply, message_time(ms), raw_payload(JSON), read_status
- `conversation`：peer_key, auto_reply_paused, manual_disabled, last_manual_reply_at, last_auto_reply_at（会话级暂停状态机）
- 消息相关辅助：auto_reply_rule / auto_reply_log / quick_reply_template / default_reply / message_filter / personal_blacklist

### 8.5 卡密与发货

- `card_group`：total/used/remain/available 四冗余计数, alert_threshold
- `card_item`：card_key, card_value, content, status(0未用/2已用), used_order_id
- `delivery_record`：order_id, delivery_mode, timing, status, receiver_info(JSON), card_item_id（幂等：每单最多一条成功）
- `delivery_rule`：trigger_on_pay, trigger_keyword, delivery_mode(kami/text/card/api), max_delivery_per_day

### 8.6 任务/调度

- `scheduled_task`：task_type, cron_expression, config_json, lease_token/lease_owner/lease_expires_at（DB 租约抢单）, run_attempt_count, consecutive_failure_count

### 8.7 通用约定（自建硬性规则）

1. 所有外部 ID（orderId/itemId/offerId）一律 **VARCHAR(64-200)**，禁止 BIGINT——闲鱼 ID 超 bigint 会被 MySQL 静默写 NULL 导致去重失效重复发货；
2. 软删 `deleted TINYINT NOT NULL DEFAULT 0`，且进唯一键 `uk(tenant, 业务键, deleted)`；
3. 金额用"分"BIGINT（price_cent）或 DECIMAL 分层，别混成一个 DECIMAL(10,2)；
4. collation 全库统一（混用 JOIN 报 1267）；
5. 幂等键：`event_key`/`request_id`/`content_hash` 模式贯穿（发货/计费/AI 学习全部用）。

---

## 9. 依赖边界

| 能力 | 只要 Cookie | 要 mtop 签名 | 必须浏览器(RPA) | 备注 |
|------|:---:|:---:|:---:|------|
| 扫码登录取 Cookie | — | 部分 | 可选 | HTTP 逆向为主，crawler QR 兜底 |
| 保活三轨 | ✅ | ✅ | 否 | hasLogin 只要 Cookie |
| 发布/下架/改价 | ✅ | ✅ | 否 | 图片上传只要 Cookie |
| 类目推荐 | ✅ | ✅ | 否 | |
| 图片上传 | ✅ | 否 | 否 | stream-upload 非 mtop |
| 订单查询/发货确认 | ✅ | ✅ | 否 | |
| 消息收发 | ✅ | ✅(token) | 否 | 走 WS 不是 HTTP |
| 搜索 | ✅ | ✅ | 慢速兜底 | DOM 抓取易碎 |
| 滑块/验证码 | ✅ | — | ✅ | **自建不做对抗**，人工介入 |
| 1688 采集 | ✅（要 1688 货源账号） | 否 | 详情页否 / 搜索页是 | 匿名打详情页只拿得到 2.3KB x5sec 壳（2026-09-19 实测） |
| 商品详情富字段 | ✅ | ✅ | view.py 用浏览器调同款 | RPA 可选 |

---

## 10. 1688 侧（品类 B 采集转换，独立于闲鱼协议）

- **采集**：httpx 直连 1688 详情页，**必须带 1688 货源账号 Cookie**（扫码登录入库那套；匿名只有 2.3KB 的 x5sec 壳，`executor.is_risk_shell` 认出后判 blocked 不入库）。
  商品 JSON 内嵌在 `window.context = (function(b,d){…})(window.contextPath, {"result":{"data":{…}}})` 的第二个实参里，两种模板（`/default` 大市场、`/processing` 加工定制 jgdz）皆如此；
  **不能整体 `json.loads`**（对象里有 `"skuWeight":{625…:0.27}` 这种未加引号的数字键），所以按字段定向正则提取（`app/collectors/alibaba/parser.py`）。
  旧文档说的 `window.__INIT_DATA__` / `window.runParams` 在 2026-09 的真实页面上**都不存在**，认它们的解析结果一律空字段。
  回归样本：`tests/fixtures/alibaba/detail_*.html`（真实页面已脱去账号痕迹，覆盖上述两种模板）。
- **搜索列表页**：前端渲染壳，DOM 里 0 条 offer 链接，商品 ID 只在页面自发的 `mtop.relationrecommend.wirelessrecommend.recommend` JSON 里 → httpx 裸取不可行，要 Playwright 拦接口（取证见 `scripts/spike_1688_search_auth.py`）。一期仍不做。
- **转换规则**（pds-tool transform 直接抄）：
  - 标题去噪：剔除"厂家直销/一手货源/批发价/一件代发/包邮"等 + `【】[]`；
  - 加价：`售价 = 源价 × 1.3`（可配），>10 元取整 −0.1；
  - 类目映射：关键词规则表 → 目标类目。
- 输出 CSV 或 API 进商品池 → 走 §3 发布到闲鱼。

---

## 11. 时效与风险（交付前必读）

### 11.1 已知时效性风险
1. **全部逆向协议为 2024-2026 快照**：接口名/字段/签名/类目 catId/滑块选择器随时失效；
2. 代码里多处"对标商业版/旧版兼容协议"注释 = 协议在演进；
3. pro 仓库时间戳（2026-07/08）为虚构时间轴，与真实版本关系未核实。

### 11.2 立项前必须实测的验证清单（按顺序）
- [ ] **V1 登录**：扫码登录能取到完整三件套（unb/_m_h5_tk/cookie2）
- [ ] **V2 只读**：用 `mtop.taobao.idle.pc.detail` 查一个商品详情成功（验证签名正确）
- [ ] **V3 发消息**：WS 连上 + /reg 成功 + 发一条文本给自己测试会话
- [ ] **V4 发商品**：小号发一个测试商品（先 draft 截图确认再 publish）
- [ ] **V5 收付款事件**：模拟买家拍下 → 收到 contentType=26 消息
- [ ] **V6 自动发货**：付款事件 → 发卡密 → confirm_shipment
- 任一步失败 = 该接口已改版，需重新抓包逆向；**不要带着全部假设直接写完整系统**，先跑通 V1→V3。

### 11.3 合规提醒
- 逆向/自动化违反闲鱼平台规则，有封号风险；小号测试、控制频率；
- 风控只做"减速 + 人工介入"，不做对抗（滑块/设备伪造/防封）；
- 若商用/开源需自查各参考项目 LICENSE（goofish-cli Apache-2.0，其余待查）。
