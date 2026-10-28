# 多用户/账号 + 首页扫码 + LLM 配置 —— 需求与改动方案

> 版本：v0.3（F1、F2 已实现）
> 日期：2026-09
> 目的：补齐四个缺口——① 多用户/账号体系（含"一人多闲鱼号"与后期收费）② 扫码登录放首页 ③ LLM 配置页 ④ 数据隔离。现状核对见 §1，改动方案见 §2-§6。

## ✅ F1 实现记录（2026-09-18，v0.2）

**阶段 F1「用户体系 + JWT + 前端登录守卫」已完成**，改动如下：

| 层 | 文件 | 说明 |
|----|------|------|
| 模型 | `backend/app/models/user.py` | `sys_user` 表（username 唯一 / bcrypt hash / role / status / remark） |
| 归属 | `backend/app/models/account.py` | account 加 `user_id`（一人 N 闲鱼号） |
| 迁移 | `backend/migrations/versions/0003_users_auth.py` | 建 sys_user + account.user_id（SET NULL） |
| 配置 | `backend/app/core/config.py` | `jwt_secret/jwt_algorithm/jwt_expire_minutes/admin_seed_*`（默认密钥已 ≥32 字节防 PyJWT 告警） |
| 依赖 | `backend/app/users/security.py` | bcrypt(cost12) + PyJWT HS256 签发/解码（PyJWT+bcrypt 已加依赖，非 python-jose） |
| 依赖 | `backend/app/users/deps.py` | `get_current_user`（401）/ `require_admin`（403），HTTPBearer |
| 依赖 | `backend/app/users/scoping.py` | `visible_account_ids`/`ensure_account_scope`：admin 全量 / user 仅自己账号 |
| Seed | `backend/app/users/seed.py` | lifespan 首次启动建 seed admin（`admin_seed_username/password`） |
| 路由 | `backend/app/api/routes/auth.py` | register/user、login→JWT、me、logout |
| 路由×5 | accounts/goods/orders/messages/delivery/settings | 全部接 `get_current_user` + scoping 过滤；settings PUT 限 admin |
| 路由 | `backend/app/api/routes/accounts.py` | 扫码确认把闲鱼号绑到当前用户（`owner_user_id`，已绑他人 → 409） |
| 前端 | `frontend/src/api/index.js` | Authorization 头 + 401 自动清 token 回 `/`；token 存 localStorage |
| 前端 | `frontend/src/views/HomeLogin.vue` | 首页系统登录页（含可收起注册） |
| 前端 | `frontend/src/router/index.js` | `beforeEach` 守卫：未登录只在 `/`，已登录访问 `/` → `/goods`；`/login` 改「扫码绑号」 |
| 前端 | `frontend/src/layouts/MainLayout.vue` | `meta.blank` 登录空布局 + 顶栏用户名/角色/退出 |

验证：
- `backend`：`tests/test_auth.py` 6 用例（注册/登录/me、401、角色隔离、admin 全量、403）全过；全量 `pytest` **129 passed**
- `frontend`：`npm run build` 成功

F2 已完成（见 §3 落地）、F3（settings 表 + LLM 配置页）按 §5 顺序待做。

## ✅ F2 实现记录（2026-09-18，v0.3）

**阶段 F2「工作台 + 扫码绑号组件化」已完成**：

| 层 | 文件 | 说明 |
|----|------|------|
| 后端 | `backend/app/api/routes/stats.py` | `GET /api/stats/overview` 工作台聚合（账号健康/待上架/在售/待发货/今日订单/今日消息/发货），按 `visible_account_ids` 作用域 |
| 测试 | `backend/tests/test_stats.py` | alice 只见自己的账号/数据、admin 全量、未登录 401（全量 131 passed） |
| 前端 | `frontend/src/components/QrScanner.vue` | 扫码核心组件（生成/轮询/confirmed），props:size、emit:confirmed/error、`start()` exposed |
| 前端 | `frontend/src/views/QrLogin.vue` | 瘦身为 QrScanner 薄包装，「扫码绑号」语义 |
| 前端 | `frontend/src/views/AccountsView.vue` | 账号列表行内「扫码绑号」→ 弹窗复用 QrScanner，确认后自动刷新 |
| 前端 | `frontend/src/views/Dashboard.vue` | 工作台概览卡片（可点击跳对应页） |
| 前端 | `frontend/src/router/index.js` | 新增 `/dashboard`；已登录访问 `/` → `/dashboard` |
| 前端 | `frontend/src/layouts/MainLayout.vue` | 菜单新增「总览→工作台」 |
| 前端 | `frontend/src/views/HomeLogin.vue` | 登录成功跳 `/dashboard` |

验证：`npm run build` 成功。

---

## 0. 四个需求的现状核对（读码结论）

| 需求 | 现状 | 结论 |
|------|------|------|
| 多用户 | 后端**无任何鉴权**；`api/deps.py` 只有 DB 依赖；`auth/` 下的"会话池"是**闲鱼账号**会话，不是系统用户 | **全缺**，需从零加用户体系 |
| 扫码登录首页 | 默认 `/` 重定向 `/goods`；`/login`(QrLogin.vue) 只是侧栏"系统"组一个条目 | 需加**独立登录页 + 路由守卫 + 首页入口** |
| LLM 配置 | LLM 只读 `.env`（`core/config.py` openai_*）；运行时仅 `/settings/reply-mode` 切 auto/manual/hybrid，不持久化 | 需 **DB settings 表 + 配置 API + 页面 + 连通性测试** |
| 一人多闲鱼号 | `session_pool` 天然多闲鱼账号（`account` 表按 unb 唯一） | 已满足；缺的是**账号归属到用户** |

现状关键文件：
- 前端路由 `frontend/src/router/index.js`（`/` → `/goods`）；布局壳 `MainLayout.vue`（menuGroups 硬编码）
- 后端入口 `backend/app/main.py`（无鉴权中间件）；账号 API `api/routes/accounts.py`（无当前用户概念）

---

## 1. 多用户模型（先定方案，再动刀）

### 1.1 可选方案

| 方案 | 数据隔离 | 收费支撑 | 复杂度 | 评价 |
|------|---------|---------|--------|------|
| A. 单管理员 + 登录 | 无隔离 | 弱（服务级收费） | ★★☆ | 一人自用/小规模够用 |
| B. 多用户共享账号池 | 仅登录 | 中（按子账号售卖） | ★★★ | 「一人多闲鱼号」天然贴合 |
| C. 多租户完全隔离 | user→account 归属 + 全表 scope | 强（按席位/租户售卖） | ★★★★ | **面向后期收费，推荐基准** |

> 你提了"一人可能有多账号" + "后期可能收费" → 推荐 **方案 C 的渐进版**：先把「用户 + JWT + 数据归属」一次性搭对（数据隔离是收费的命根子），**权限/计费细节后期再加**。这样"一人多号"天然支持：一个用户下挂 N 个闲鱼 account，分给我的只有我的。

### 1.2 数据归属设计

- 新增 `user` 表 + 国家表关联：`account.user_id` 归属（现在 `account` 表没有 user 维度）
- 全部业务表依 `account` 反查归属：`goods/account_id`、`trade_order/account_id`、`conversation|chat_message/account_id`、`card_group|delivery*`、`scheduled_task/account_id` 均已带 `account_id` → **加 user 维度只需在 `account` 上加 `user_id`，再按 session 用户 → 可见 account 集合过滤即可**，各表无需加列。
- `scheduled_task` 目前是**全局任务**（无 account 归属）——定时采集/上架任务建议加 `user_id`/`account_id` 归属，避免 A 用户的任务把商品发到 B 的闲鱼号。

### 1.3 角色

- `admin`：全部账号可见、可管理用户、可改系统级配置（LLM 全局默认、风控参数）
- `user`：仅自己和属于自己的 account/商品/订单/卡密
- 参考 pds-tool 是 `admin`/`user` 两角色 + JWT claim（`auth.py:76-105`），够用。

---

## 2. 用户体系（后端第一优先）

### 2.1 表设计（新增迁移 `0003_users_auth.py`）

```python
class User(Base):
    username    # 唯一，登录名
    password_hash  # bcrypt（cost 12）
    role        # admin / user
    status      # 1 启用 / 0 停用
    remark      # 备注（会员等级预留位）
```

> 表名 `user`（避免保留字可用 `sys_user`）。参考 pds-tool `models.py:165-185`（User：username/password_hash/role/status）。

### 2.2 鉴权链（FastAPI）

- **python-jose HS256 JWT** + **bcrypt**（pds-tool `auth.py:20-39` 全套可抄：`get_password_hash` / `create_access_token` / `decode_token`）
- 依赖注入（比 pds 更稳）：`get_current_user` 解析 `Authorization: Bearer`（可加 httpOnly Cookie 双通道，pds `auth.py:76-105`），再封装 `require_admin`
- 签发接口：`POST /api/auth/login`（密码登录，token 签 24h）、`GET /api/auth/me`、`POST /api/auth/register`（默认 user 角色；**admin 注册关闭或白名单**）
- 登录审计（收费要）→ `login_record` 表留痕（pds `api/auth.py:156-175` + `login-records/stats`），v1 可后置
- **改动面**：`main.py` 加鉴权中间件/全局依赖；`api/deps.py` 增加 `get_current_user`；`accounts/goods/orders/messages/delivery/settings` 各路由注入当前用户并过滤。**前端 `api/index.js` 加 Authorization 头 + 401 跳登录**。

### 2.3 前端登录态

- `router/index.js`：加 `/login` 尾页，全路由加 `meta.requiresAuth`，`beforeEach` 无 token 跳登录
- `api/index.js`：`fetch` 统一带 `Authorization: Bearer <token>`；401 → 清 token 跳登录
- `MainLayout.vue`：顶栏加用户信息 + 退出；未登录时完全进不去

---

## 3. 扫码登录放首页

> 独立 → 明确边界：**系统登录页（进系统）** 与 **闲鱼扫码登录（绑账号）** 是两件事。

### 3.1 推荐页面结构

```
/                    → 系统登录页（HomeLogin.vue，也即首页）
  ├─ 未登录：居中「用户名/密码 + 登录」卡片，成功后进 /dashboard
  └─ 已登录：进入可选功能入口（若做首页）或直接 /dashboard
/dashboard（新）      → 工作台：账号健康/今日消息/待上架/待发货 概览卡片
/login（保留）        → 保留，作为「闲鱼扫码登录」专用页（账号页也嵌入同款二维码弹窗）
/accounts            → 账号列表 + 行内「扫码绑号」入口（复用 QrLogin 逻辑抽成组件）
```

### 3.2 具体改动

1. **`router/index.js`**：`/` 默认不再去 `/goods`，改为登录页组件；新增 `/dashboard`；`/login` 语义改为「闲鱼扫码绑号」
2. **抽 QrLogin 核心为组件** `src/components/QrScanner.vue`（生成/轮询/confirmed 回调），`QrLogin.vue` 与账号页共用，省两份代码
3. **MainLayout**：menuGroups「系统」组把 `/login` 文案改「扫码绑号」；若 `/dashboard` 是新首页，加到最前组
4. 前端构建验证：`npm run build` + 三态（未登录/已登录/绑定中）冒烟

> 收费考量：若把系统作为「SaaS 用户端首页」，`/dashboard` 放「我的账号/我的商品/套餐/订阅」入口，就是 user-web 的雏形（pro 就是 admin-web + user-web 双端）。

---

## 4. LLM 配置页

### 4.1 现状 vs 目标

- 现在：`openai_api_key/base_url/model`、`ai_*` 回复策略全在 `.env`；运行时只能切 reply-mode
- 目标：网页可配 `api_key/base_url/model/system_prompt/温度/限额/屏蔽词/工作时段` + **测试连通** + 持久化 + 权限（admin 可改全局，user 可看只读/或自己的）

### 4.2 后端

1. **新增 `settings` KV 表**（参考 pds `db.py:89-124 SettingsService` + 我们 `Settings` 模型）：
   - `llm_api_key / llm_base_url / llm_model`、`ai_reply_mode / ai_max_daily_replies / ai_blocked_keywords / ai_work_hour_start / ai_work_hour_end / ai_system_prompt / ai_history_max_messages / ai_max_message_age_seconds`、`delivery_auto_consign`
2. **API**：
   - `GET /api/settings/llm`（读，api_key 脱敏 `sk-***`)
   - `PUT /api/settings/llm`（写 → 落 DB + 更新内存 settings 单例，**免重启生效**）
   - `POST /api/settings/llm/test`（向 base_url 发一次最小 chat，返回 ok/耗时/错误，参考 pds `settings.py:49-105`）
3. **接入层**：`ai/provider.py` 传参顺序改为 **DB 覆盖 env**（用户配了优先，未配回退 `.env`）；LLM 配置变更后 `reply_engine` 重读（不重启）

### 4.3 前端

- 新页面 `/settings/llm` 或并入「系统设置」Tab：
  - 模型选择区（base_url / api_key / model 输入）
  - 人设与回复策略卡（system_prompt 多行 textarea、mode 单选 auto/manual/hybrid、日限额数字、屏蔽词 tags 输入、工作时段时间选择）
  - 右上角「测试连接」按钮（调 `/settings/llm/test` 显示结果）
  - 保存 → `PUT` → 提示「已生效（无需重启）」
- 菜单：MainLayout 新增「系统」组「LLM 配置」条目

---

## 5. 改动顺序建议（依赖排）

```
阶段 F1（地基，优先）      用户体系 + JWT + 前端登录守卫
        └─ 一切 API 先跑通鉴权（否则后面每加接口都要回头补）
阶段 F2                  首页登录页 + /dashboard + 扫码绑号组件化
阶段 F3                  settings 表 + LLM 配置页 + 连通测试
阶段 F4（与上并行）       1688 采集/定时上架 见《1688采集与定时上架调研.md》
```

> 理由：① 鉴权是"加了越早改得越少"的典型——现在所有路由都是裸奔，越晚补，每个已有接口都要动；② 数据归属（account.user_id）会影响采集/定时上架任务的归属设计，先定 user 模型能少返工。

---

## 6. 收费预留（设计时埋点，不实现）

| 埋点 | 位置 | 用途 |
|------|------|------|
| `user.role` / 计费字段 | 用户表预留（remark 或独立 `user_plan` 表） | 按用户卖套餐 |
| `login_record` 审计 | auth 登录时写 | 登录安全 + 计费对账 |
| `account.user_id` 归属 | account 表加列 | 用户级席位售卖（一人多号天然支持） |
| `scheduled_task.user_id` | 任务表加列 | 用户级定时任务配额限制 |
| admin/user 两角色依赖已建 | require_admin 依赖 | 管理员后台 vs 用户端拆分（pro 的 admin-web/user-web 模式） |