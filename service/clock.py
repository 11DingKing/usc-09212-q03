"""时间工具：统一 UTC、ISO8601 解析与可替换时钟。"""
from datetime import datetime, timezone


class Clock:
    """系统时钟。"""

    def now(self):
        return datetime.now(timezone.utc)


class FixedClock:
    """测试用固定时钟，可手动推进。"""

    def __init__(self, dt):
        self.dt = dt.astimezone(timezone.utc)

    def now(self):
        return self.dt

    def advance(self, timedelta):
        self.dt += timedelta
        return self.dt


def to_iso(dt):
    """格式化为秒级 UTC ISO 字符串。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso(clock):
    return to_iso(clock.now())


def parse_iso(value):
    """解析兼容尾缀 Z 的 ISO8601 字符串，返回带时区 UTC datetime。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
