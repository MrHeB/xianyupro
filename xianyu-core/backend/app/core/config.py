"""全局配置（环境变量 > .env 默认值）。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ==== MySQL ====
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_database: str = "xianyu_core"
    mysql_user: str = "xianyu"
    mysql_password: str = "xianyu123"

    # ==== Redis ====
    redis_url: str = "redis://localhost:6379/0"

    # ==== 安全 ====
    # Cookie AES-256-GCM 加密密钥（SHA256 后使用），丢失 = 所有已存登录态作废
    cookie_crypto_secret: str = "change-me-in-production"

    # ==== 用户鉴权（系统登录 JWT，阶段 F1） ====
    # 至少 32 字节，否则 PyJWT 告警 InsecureKeyLengthWarning
    jwt_secret: str = "change-me-jwt-secret-at-least-32-bytes-long"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24  # 24h
    # 首次启动（sys_user 空表）时 seed 的默认管理员
    admin_seed_username: str = "admin"
    admin_seed_password: str = "admin123"

    # ==== AI（OpenAI 兼容端点，可换 DeepSeek/通义） ====
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    ai_request_timeout: float = 30.0

    # ==== AI 自动回复（阶段 4-3，protocol.md §7/§8.4） ====
    # auto / hybrid / manual（hybrid 暂按 auto 处理，前端确认按钮后置）
    ai_reply_mode: str = "auto"
    ai_max_daily_replies: int = 200
    # 命中即不自动回复，留给人工（可写正则，如 "转人工|投诉|客服电话"）
    ai_blocked_keywords: str = "转人工,投诉,客服电话,加微信"
    # 工作时段（0-23 含；空 = 全天）
    ai_work_hour_start: int | None = None
    ai_work_hour_end: int | None = None
    # 超过该秒数的旧消息不回（防首次连接全量同步时回一堆历史消息）
    ai_max_message_age_seconds: int = 600
    # 拼进 prompt 的近 N 条历史
    ai_history_max_messages: int = 10
    ai_system_prompt: str = (
        "你是一名闲鱼卖家的客服助手，帮卖家回复买家咨询。"
        "要求：口语化、简短（一般不超过两句话）、礼貌热情；"
        "只回答与商品和交易相关的问题；涉及价格让步时不要直接答应，"
        "回复'我问下卖家哈'之类的话术；不确定的信息宁可说不知道，不要编造。"
    )

    # ==== 自动发货（阶段 4-4，品类 A 卡密） ====
    # 卡密消息送达后是否自动调平台确认发货（consign.dummy）；关闭则停在 message_confirmed
    delivery_auto_consign: bool = True

    # ==== 1688 采集（设计文档 §9-2：转换倍率从 settings 读） ====
    collect_markup_rate: float = 1.3
    # 本地文件夹工作区根（§3.1；整体进 .gitignore）
    collect_workspace_path: str = "data/collect"
    # 单次关键词检索的 offer_id 上限（搜索页比详情页更敏感，宁小勿大）
    collect_search_max_items: int = 20
    # 单次 pipeline 串跑最多处理几件：乘上"每件 2 次 LLM 调用 + 最多 2 轮改写"就是成本上界
    collect_pipeline_max_items: int = 20
    # 每个 1688 货源账号每小时允许开几次检索浏览器（只管定时路径，需求文档 §4.5 / N7）
    collect_search_hourly_quota: int = 6
    # 检索原始响应体落盘目录（排障/补测试 fixture 用）。**空 = 不落盘**，默认关。
    # 刻意不放工作区里：`workspace.list_batches` 只认带 `batch.json` 的目录，所以放进去不会
    # 凭空多出批次，但 `data/collect/` 是"人能直接打开核对的工作区"（§3.1），调试产物混进去只让现场变脏。
    collect_debug_dump_path: str = ""

    # ==== 写操作止损熔断（风控手册 §1.1，参数照抄 ai-goofish-monitor） ====
    task_failure_threshold: int = 3
    task_failure_pause_seconds: int = 24 * 60 * 60
    failure_guard_state_path: str = "logs/task-failure-guard.json"

    # ==== 闲鱼协议常量（protocol.md §1） ====
    h5_api_base: str = "https://h5api.m.goofish.com/h5"
    mtop_app_key: str = "34839810"
    mtop_jsv: str = "2.7.2"
    ws_url: str = "wss://wss-goofish.dingtalk.com/"
    ws_heartbeat_interval: int = 15  # 秒
    # IM accessToken 内存缓存时长（token 接口风控敏感，真实过期约 2h）
    im_token_ttl_seconds: float = 30 * 60
    # WS 断线退避重连区间（秒，指数）
    ws_reconnect_min_seconds: float = 5.0
    ws_reconnect_max_seconds: float = 300.0
    # WS 常驻时 loginuser.get 保活间隔（秒）
    ws_refresh_interval: float = 600.0

    @property
    def mysql_dsn(self) -> str:
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
