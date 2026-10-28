"""写操作止损熔断器（风控手册 §1.1 / §4 R-2~R-6，参数照抄 ai-goofish-monitor failure_guard）。

与 api_client 现有两层节流**叠加而非替换**：
- 令牌桶 1 次/分钟 + RGV587 冷却 10 分钟 = 接口级短期节流（已有）；
- 本模块 = 账号级止损：连续 3 次写失败 → 暂停该域 24h。

要点（对齐手册）：
- guard key 按操作域拆分 `publish|reply|delivery:account_id`，三域独立计数（R-3）；
- 状态文件原子写 + fsync；损坏改名保留现场再继续，不崩溃；
- 暂停通知每天最多 1 次（只记日志，前端可见性走 R-9 面板）；
- 解锁三路径：① 该账号 Cookie 的 DB updated_at 变新（重新扫码即恢复）
  ② 暂停到期 ③ 一次 record_success；
- 风控/登录态类错误 immediate=True，阈值降为 1，立即熔断不浪费重试（手册 §1.2）。

单进程 asyncio 常驻部署（scheduler 同进程），进程内锁 + 文件持久即够；
多副本部署前状态迁 Redis（同令牌桶的既定待办）。
"""

import asyncio
import json
import logging
import os
import random
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

# 显式东八区（手册 §1.1：不依赖系统时区；Windows 无 tzdata，固定偏移足够）
TZ_SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")

STATE_VERSION = 1


class WriteGuardPaused(RuntimeError):
    """熔断期内拒绝写操作。retryable=False 是硬返回：LLM/上层拿不到重试余地（R-10）。"""

    retryable = False

    def __init__(self, key: str, remaining_seconds: int):
        super().__init__(
            f"写操作已熔断（{key}）：剩余 {remaining_seconds // 60} 分钟；"
            "重新扫码更新 Cookie 可自动解锁，禁止重试绕过"
        )
        self.key = key
        self.remaining_seconds = remaining_seconds


@dataclass
class PauseInfo:
    key: str
    until: datetime
    remaining_seconds: int


def _now() -> datetime:
    return datetime.now(TZ_SHANGHAI)


def _aware(dt: datetime | str | None) -> datetime | None:
    """DB 里是本地 naive 时钟（models/base.py 约定），统一按东八区解释。"""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ_SHANGHAI)


class FailureGuard:
    """文件持久熔断器：key → {连续失败数, 暂停截止, Cookie 基线}。"""

    def __init__(
        self,
        state_path: str | Path | None = None,
        *,
        threshold: int | None = None,
        pause_seconds: int | None = None,
    ):
        self.state_path = Path(state_path or settings.failure_guard_state_path)
        self.threshold = max(1, threshold if threshold is not None else settings.task_failure_threshold)
        # 暂停下限 60s：防误配 0 导致熔断风暴（手册 §1.1）
        self.pause_seconds = max(60, pause_seconds if pause_seconds is not None else settings.task_failure_pause_seconds)
        self._lock = threading.Lock()

    # ---------- 状态读写 ----------

    def _load(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
                raise ValueError("结构非法")
            return data
        except FileNotFoundError:
            return {"version": STATE_VERSION, "keys": {}}
        except Exception as exc:
            # 损坏不崩溃：改名保留现场，从零开始
            try:
                self.state_path.rename(self.state_path.with_name(
                    f"{self.state_path.name}.corrupt-{_now():%Y%m%dT%H%M%S}"
                ))
                logger.error("熔断状态文件损坏，已改名保留现场并重置：%s", exc)
            except OSError:
                logger.error("熔断状态文件损坏且无法保留现场，按空状态继续：%s", exc)
            return {"version": STATE_VERSION, "keys": {}}

    def _save(self, data: dict) -> None:
        data["updated_at"] = _now().isoformat()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _entry(data: dict, key: str) -> dict:
        return data["keys"].setdefault(
            key, {"failures": 0, "paused_until": None, "last_notified_date": None, "cookie_updated_at": None}
        )

    # ---------- 对外 API ----------

    def is_paused(self, key: str, *, cookie_updated_at: datetime | str | None = None) -> PauseInfo | None:
        """是否处于熔断。Cookie updated_at 比基线新 → 自动解锁（重扫码即恢复，无需人工按钮）。"""
        with self._lock:
            data = self._load()
            e = data["keys"].get(key)
            if not e:
                return None
            changed = False
            baseline = _aware(e.get("cookie_updated_at"))
            current = _aware(cookie_updated_at)
            if baseline and current and current > baseline:
                data["keys"].pop(key)  # 三条解锁路径①：Cookie 变新
                changed = True
            until = _aware(e.get("paused_until")) if e.get("paused_until") else None
            if changed:
                self._save(data)
                logger.info("熔断自动解锁（Cookie 已更新）key=%s", key)
                return None
            if until is None:
                return None
            remaining = int((until - _now()).total_seconds())
            if remaining <= 0:
                # 到期放行：开新窗口（计数清零），否则下一次失败立即复发熔断
                e["paused_until"] = None
                e["failures"] = 0
                self._save(data)
                return None
            today = _now().strftime("%Y-%m-%d")
            if e.get("last_notified_date") != today:
                e["last_notified_date"] = today
                self._save(data)
                logger.warning("写操作熔断中 key=%s 剩余 %d 分钟", key, remaining // 60)
            return PauseInfo(key=key, until=until, remaining_seconds=remaining)

    def record_failure(
        self,
        key: str,
        *,
        cookie_updated_at: datetime | str | None = None,
        immediate: bool = False,
    ) -> PauseInfo | None:
        """记一次写失败。immediate=确定性错误（风控/登录态），阈值降为 1 立即熔断。

        返回非 None 表示本次失败刚触发/已处于熔断 → 调用方执行 R-6 随机静默。
        """
        with self._lock:
            data = self._load()
            e = self._entry(data, key)
            e["failures"] += 1
            if cookie_updated_at is not None:
                e["cookie_updated_at"] = _aware(cookie_updated_at).isoformat()
            if immediate or e["failures"] >= self.threshold:
                until = _now() + timedelta(seconds=self.pause_seconds)
                e["paused_until"] = until.isoformat()
                logger.warning(
                    "熔断触发 key=%s 连续失败=%d immediate=%s 暂停至=%s",
                    key, e["failures"], immediate, until.isoformat(),
                )
                self._save(data)
                return PauseInfo(key=key, until=until, remaining_seconds=self.pause_seconds)
            self._save(data)
            return None

    def record_success(self, key: str) -> None:
        with self._lock:
            data = self._load()
            if data["keys"].pop(key, None):
                self._save(data)

    def status(self, key: str | None = None) -> dict:
        """R-9 可观测：面板/Dashboard 读熔断状态（key 为空返回全部）。"""
        with self._lock:
            data = self._load()
            if key is None:
                return dict(data["keys"])
            return dict(data["keys"].get(key, {}))


async def risk_silence() -> None:
    """R-6 熔断/风控抛错前随机静默 30-300s：防上层循环立刻重试打回（手册 §1.3 借 :1002）。"""
    await asyncio.sleep(random.randint(30, 300))


# 全局单例（发布/回复/发货域共用一个状态文件，按 key 隔离）
failure_guard = FailureGuard()


def guard_key(domain: str, account_id: int) -> str:
    assert domain in ("publish", "reply", "delivery"), f"未知操作域 {domain}"
    return f"{domain}:{account_id}"
