"""mtop 错误分类（protocol.md §1.3）。

错误分层是重试策略的依据：
- 可自动恢复（TOKEN/SESSION 过期）→ 续期后重试；
- 不可恢复（风控/非法访问）→ 停手，转人工；
- 业务错误 → 本地校验防触发，不重试；
- 本地限流（自己令牌桶没拿到 token）→ 请求没出门，等一会再试，不计入止损。
"""

from enum import Enum


class ErrorKind(str, Enum):
    RISK_CONTROL = "risk_control"        # RGV587/滑块 → 不可重试，熔断 + 人工
    ILLEGAL_ACCESS = "illegal_access"    # FAIL_SYS_ILLEGAL_ACCESS → 不可自动救，停手
    SESSION_EXPIRED = "session_expired"  # 触发免密续期后重试
    TOKEN_EXPIRED = "token_expired"      # FAIL_SYS_TOKEN_EXOIRED（服务端真实拼写如此）
    BIZ_ERROR = "biz_error"              # FAIL_BIZ_* 业务参数错误
    NOT_FOUND = "not_found"              # 商品不存在，跳过
    NETWORK = "network"                  # 超时/网络异常 → 指数退避重试
    LOCAL_THROTTLE = "local_throttle"    # 本项目自己的令牌桶没拿到 token：请求根本没出门
    UNKNOWN = "unknown"


# 风控/滑块特征串（protocol.md §1.3 表）
_RISK_PATTERNS = ("RGV587_ERROR", "FAIL_SYS_USER_VALIDATE", "哎哟喂", "punish", "baxia")


class XianyuApiError(Exception):
    """带错误分类的闲鱼 API 异常。"""

    def __init__(self, kind: ErrorKind, message: str, *, ret: list | None = None):
        super().__init__(message)
        self.kind = kind
        self.ret = ret or []

    @property
    def retryable(self) -> bool:
        return self.kind in (
            ErrorKind.SESSION_EXPIRED,
            ErrorKind.TOKEN_EXPIRED,
            ErrorKind.NETWORK,
            ErrorKind.LOCAL_THROTTLE,
        )

    @property
    def recoverable(self) -> bool:
        """是否可自动恢复（区别于风控类必须人工介入）。"""
        return self.retryable


def classify_error(ret: list, message: str = "") -> ErrorKind:
    """按返回的 ret 数组 / 错误文案分类（ret[0] 形如 `FAIL_SYS_TOKEN_EXOIRED_xxx`）。"""
    text = " ".join(str(r) for r in ret) + " " + message
    if any(p in text for p in _RISK_PATTERNS):
        return ErrorKind.RISK_CONTROL
    if "FAIL_SYS_ILLEGAL_ACCESS" in text:
        return ErrorKind.ILLEGAL_ACCESS
    if "FAIL_SYS_SESSION_EXPIRED" in text:
        return ErrorKind.SESSION_EXPIRED
    if "FAIL_SYS_TOKEN_EXOIRED" in text:  # 服务端真实拼写，勿"修复"
        return ErrorKind.TOKEN_EXPIRED
    if "FAIL_BIZ_" in text:
        return ErrorKind.BIZ_ERROR
    if "NOT_FOUND" in text or "不存在" in text:
        return ErrorKind.NOT_FOUND
    return ErrorKind.UNKNOWN
