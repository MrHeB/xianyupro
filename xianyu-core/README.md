# xianyu-core —— 闲鱼自动化系统（自建）

> 依据 `../流程方案.md`（v1.1）阶段 3 搭建的项目骨架。
> 目标：1688 采集 → 自动上架闲鱼 → AI 自动回复 → 自动发货（双品类两线独立）。
> 协议知识见 `docs/protocol.md`（阶段 2 核心资产，随项目维护）。

## 目录结构

```
xianyu-core/
├── backend/
│   ├── app/
│   │   ├── auth/          # 统一会话池：多账号登录、Cookie 存取、自动续期
│   │   ├── api_client/    # mtop 签名、请求封装、重试、限频、风控策略
│   │   ├── collectors/    # 采集插件（1688 / CSV 导入）
│   │   ├── listing/       # 商品构建、发布、上下架、定时上架
│   │   ├── messaging/     # 消息监听 + 事件总线
│   │   ├── ai/            # LLM 接入：自动回复、标题/描述生成
│   │   ├── fulfillment/   # 发货：卡密、订单状态流转
│   │   ├── scheduler/     # 调度中心（Redis 队列）串起全流水线
│   │   ├── api/           # REST + WebSocket 对外接口
│   │   ├── core/          # 配置、数据库、通用基础设施
│   │   └── models/        # SQLAlchemy 数据模型（protocol.md §8）
│   ├── migrations/        # Alembic 数据库迁移
│   ├── tests/
│   └── pyproject.toml
├── frontend/               # Vue 3 管理后台（第一阶段做薄）
├── docker-compose.yml      # MySQL + Redis + 后端 + 前端
└── docs/protocol.md        # 闲鱼非官方 API 协议文档
```

## 模块移植顺序（流程方案 阶段 4）

| 顺序 | 模块 | 验收标准 |
|------|------|---------|
| 1 | auth | 多账号稳定登录、掉线自动重登 |
| 2 | api_client | 用只读接口（查商品/订单）验证签名正确 |
| 3 | messaging + ai | 监听消息 → LLM 生成回复 → 发出 |
| 4 | fulfillment | 虚拟卡密自动发货、订单状态流转 |
| 5 | collectors + listing | CSV 手动导入顶替，采集/上架后置 |

## 快速开始

```bash
# 1. 准备环境变量
cp .env.example .env   # 修改 MYSQL 密码、COOKIE_CRYPTO_SECRET 等

# 2. 启动基础设施 + 后端 + 前端（backend 容器启动时会自动执行 alembic upgrade head）
docker compose up -d

# 3. 手动补迁移（可选，仅在容器没跑迁移或需要单独执行时用）
docker compose exec backend alembic upgrade head

# 4. 本地开发（不用 Docker 时）
cd backend
uv sync                # 或 pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

- 后端 API 文档：`http://localhost:8000/docs`（Swagger，前端与 AI Agent 共用）
- 前端面板：`http://localhost:8080`

## 硬性规则（自建约定，来自 protocol.md §8.7）

1. 所有外部 ID（orderId/itemId/offerId）一律 **VARCHAR**，禁止 BIGINT；
2. 软删 `deleted TINYINT NOT NULL DEFAULT 0`；
3. 金额统一用"分"（BIGINT `*_cent`）；
4. 幂等键贯穿：`event_key` / `request_id` / `content_hash`；
5. 写操作限流 1 次/分钟 + RGV587 熔断 10 分钟（api_client 已内置）。

## 品类边界（两线独立，互不交叉）

- **品类 A（虚拟商品）**：手动上架 + 卡密库存，下单自动发货；
- **品类 B（1688 采集）**：自动上架售卖，履约人工，系统只推送订单通知。
