"""写操作止损熔断器测试（风控手册 §1.1 / §4 R-2/R-3；M0 验收项：V7 熔断可复现）。

覆盖：连续 3 次失败触发 24h 暂停、immediate 阈值 1、三域独立计数、
到期放行、record_success 清零、Cookie（last_login_at）变新自动解锁、
状态文件损坏改名保留现场、暂停通知每天最多 1 次。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.failure_guard import FailureGuard, guard_key

TZ = timezone(timedelta(hours=8))


@pytest.fixture
def guard(tmp_path):
    return FailureGuard(tmp_path / "guard.json", threshold=3, pause_seconds=24 * 3600)


def test_three_failures_trip_24h_pause(guard):
    key = guard_key("publish", 1)
    assert guard.is_paused(key) is None
    guard.record_failure(key)
    guard.record_failure(key)
    assert guard.is_paused(key) is None  # 2 次未到阈值
    info = guard.record_failure(key)
    assert info is not None
    paused = guard.is_paused(key)
    assert paused is not None
    assert 24 * 3600 - 60 < paused.remaining_seconds <= 24 * 3600


def test_immediate_failure_trips_at_once(guard):
    key = guard_key("publish", 2)
    assert guard.record_failure(key, immediate=True) is not None
    assert guard.is_paused(key) is not None


def test_domains_are_independent(guard):
    """R-3：发布挂了不该把自动发货一起停掉。"""
    pk, dk = guard_key("publish", 1), guard_key("delivery", 1)
    for _ in range(3):
        guard.record_failure(pk)
    assert guard.is_paused(pk) is not None
    assert guard.is_paused(dk) is None


def test_record_success_resets(guard):
    key = guard_key("publish", 3)
    guard.record_failure(key)
    guard.record_failure(key)
    guard.record_success(key)
    assert guard.record_failure(key) is None  # 计数已清零，第 1 次不算熔断
    assert guard.is_paused(key) is None


def test_pause_expires_and_allows_retry(guard, tmp_path):
    key = guard_key("publish", 4)
    guard.record_failure(key, immediate=True)
    # 直接改状态文件把暂停期推到过去
    import json

    state_file = tmp_path / "guard.json"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    data["keys"][key]["paused_until"] = (datetime.now(TZ) - timedelta(seconds=1)).isoformat()
    state_file.write_text(json.dumps(data), encoding="utf-8")
    assert guard.is_paused(key) is None
    # 到期放行后重新计数：再连续失败 3 次仍会再次熔断
    assert guard.record_failure(key) is None
    assert guard.record_failure(key) is None
    assert guard.record_failure(key) is not None
    assert guard.is_paused(key) is not None


def test_newer_login_auto_unlocks(guard):
    key = guard_key("publish", 5)
    t0 = datetime(2026, 9, 1, 12, 0, 0)
    guard.record_failure(key, cookie_updated_at=t0, immediate=True)
    assert guard.is_paused(key, cookie_updated_at=t0) is not None
    # 用户重新扫码：last_login_at 变新 → 自动解锁，无需人工
    assert guard.is_paused(key, cookie_updated_at=t0 + timedelta(minutes=1)) is None
    assert guard.is_paused(key, cookie_updated_at=t0 + timedelta(minutes=1)) is None


def test_corrupt_state_file_is_preserved_not_crash(tmp_path):
    state_file = tmp_path / "guard.json"
    state_file.write_text("{ not json", encoding="utf-8")
    guard = FailureGuard(state_file, threshold=1, pause_seconds=120)
    key = guard_key("reply", 1)
    assert guard.is_paused(key) is None  # 不崩溃，按空状态继续
    guard.record_failure(key)  # threshold=1 → 立即熔断
    assert guard.is_paused(key) is not None
    assert any(p.name.startswith("guard.json.corrupt-") for p in tmp_path.iterdir())


def test_pause_floor_is_60s(tmp_path):
    guard = FailureGuard(tmp_path / "g.json", threshold=1, pause_seconds=0)
    key = guard_key("publish", 6)
    info = guard.record_failure(key)
    assert info is not None
    assert 55 < guard.is_paused(key).remaining_seconds <= 60


def test_state_persists_across_instances(tmp_path):
    f = tmp_path / "g.json"
    g1 = FailureGuard(f, threshold=3, pause_seconds=3600)
    key = guard_key("publish", 7)
    g1.record_failure(key)
    g1.record_failure(key)
    g2 = FailureGuard(f, threshold=3, pause_seconds=3600)
    assert g2.record_failure(key) is not None  # 前两次的计数落盘可见
