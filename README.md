# xianyupro

闲鱼自动化系统（自建）：**1688 采集 → 自动上架闲鱼 → AI 自动回复 → 自动发货**（双品类两线独立）。

主体代码在 [`xianyu-core/`](xianyu-core/)，分为 FastAPI 后端与 Vue 3 管理后台，用 `docker compose` 一键拉起 MySQL + Redis + 后端 + 前端。

## 目录结构

```
.
├── xianyu-core/            # 主体工程
│   ├── backend/            # FastAPI 后端
│   │   ├── app/
│   │   │   ├── auth/           # 统一会话池：多账号登录、Cookie 存取、自动续期
│   │   │   ├── api_client/     # mtop 签名、请求封装、重试、限频、风控策略
│   │   │   ├── collectors/     # 采集插件（1688 / CSV 导入）
│   │   │   ├── listing/        # 商品构建、发布、上下架、定时上架
│   │   │   ├── messaging/      # 消息监听 + 事件总线
│   │   │   ├── ai/             # LLM 接入：自动回复、标题/描述生成
│   │   │   ├── fulfillment/    # 发货：卡密、订单状态流转
│   │   │   ├── scheduler/      # 调度中心（Redis 队列）串起全流水线
│   │   │   ├── api/            # REST + WebSocket 对外接口
│   │   │   ├── core/           # 配置、数据库、通用基础设施
│   │   │   └── models/         # SQLAlchemy 数据模型
│   │   ├── migrations/         # Alembic 数据库迁移
│   │   └── tests/
│   ├── frontend/           # Vue 3 管理后台
│   ├── docker-compose.yml  # MySQL + Redis + 后端 + 前端
│   └── docs/               # 协议与接口文档
├── docs/                   # 调研、需求设计与方案文档
└── 流程方案.md              # 总体流程方案（v1.1）
```

## 快速开始

```bash
# 1. 准备环境变量（务必修改里面的密码与密钥）
cd xianyu-core
cp .env.example .env

# 2. 启动基础设施 + 后端 + 前端（backend 容器启动时会自动执行 alembic upgrade head）
docker compose up -d

# 3. 手动补迁移（可选）
docker compose exec backend alembic upgrade head

# 4. 本地开发（不用 Docker 时）
cd backend
uv sync                # 或 pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

- 后端 API 文档：<http://localhost:8000/docs>（Swagger，前端与 AI Agent 共用）
- 前端面板：<http://localhost:8080>

> `.env` 中的 `MYSQL_*`、`COOKIE_CRYPTO_SECRET`、`JWT_SECRET` 出厂为示例占位值，**部署前必须改成随机值**。`COOKIE_CRYPTO_SECRET` 丢失会导致所有已存登录态作废。

## 模块移植顺序

| 顺序 | 模块 | 验收标准 |
|------|------|---------|
| 1 | auth | 多账号稳定登录、掉线自动重登 |
| 2 | api_client | 用只读接口（查商品/订单）验证签名正确 |
| 3 | messaging + ai | 监听消息 → LLM 生成回复 → 发出 |
| 4 | fulfillment | 虚拟卡密自动发货、订单状态流转 |
| 5 | collectors + listing | CSV 手动导入顶替，采集/上架后置 |

## 硬性规则

1. 所有外部 ID（orderId/itemId/offerId）一律 **VARCHAR**，禁止 BIGINT；
2. 软删 `deleted TINYINT NOT NULL DEFAULT 0`；
3. 金额统一用"分"（BIGINT `*_cent`）；
4. 幂等键贯穿：`event_key` / `request_id` / `content_hash`；
5. 写操作限流 1 次/分钟 + RGV587 熔断 10 分钟（`api_client` 已内置）。

## 品类边界（两线独立，互不交叉）

- **品类 A（虚拟商品）**：手动上架 + 卡密库存，下单自动发货；
- **品类 B（1688 采集）**：自动上架售卖，履约人工，系统只推送订单通知。

## 文档索引

- [`流程方案.md`](流程方案.md) —— 总体流程方案
- [`docs/`](docs/) —— 1688 采集与定时上架调研、自然语言采集上架需求设计、接口知识盘点、闲鱼风控操作手册等
- [`xianyu-core/docs/protocol.md`](xianyu-core/docs/protocol.md) —— 闲鱼侧协议文档
- [`xianyu-core/docs/后端接口文档.md`](xianyu-core/docs/后端接口文档.md) —— 后端接口说明
