from datetime import datetime, timezone, tzinfo
from math import ceil
from zoneinfo import ZoneInfo


class TimeUtil:
    DEFAULT_FMT = "%Y-%m-%d %H:%M:%S"
    DEFAULT_TZ = ZoneInfo("Asia/Shanghai")
    UTC = timezone.utc

    @staticmethod
    def parse(time_input: str | datetime) -> datetime:
        """
        解析时间
        - 支持 datetime 或 ISO/普通字符串
        - 字符串不带时区 → 默认 Asia/Shanghai
        - 返回 UTC 时间
        """
        if isinstance(time_input, datetime):
            dt = time_input
        else:
            try:
                # 支持 Z / +08:00
                dt = datetime.fromisoformat(
                    time_input.replace("Z", "+00:00")
                )
            except ValueError:
                dt = datetime.strptime(time_input, TimeUtil.DEFAULT_FMT)

        # 没有时区 → 直接 replace
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TimeUtil.DEFAULT_TZ)

        # 统一转 UTC
        return dt.astimezone(TimeUtil.UTC)

    @staticmethod
    def now(tz: tzinfo | None = None) -> datetime:
        """
        获取当前时间（默认 Asia/Shanghai）
        """
        tz = tz or TimeUtil.DEFAULT_TZ
        return datetime.now(tz)

    @staticmethod
    def now_utc() -> datetime:
        """
        获取当前 UTC 时间。
        """
        return TimeUtil.now(TimeUtil.UTC)

    @staticmethod
    def add_minutes(dt: datetime, minutes: int) -> datetime:
        """
        在给定时间上增加/减少分钟。
        """
        from datetime import timedelta

        return dt + timedelta(minutes=minutes)

    @staticmethod
    def add_days(dt: datetime, days: int) -> datetime:
        """
        在给定时间上增加/减少天数。
        """
        from datetime import timedelta

        return dt + timedelta(days=days)

    @staticmethod
    def ensure_utc(dt: datetime) -> datetime:
        """
        确保时间为 UTC。
        - 无时区：按 UTC 解释
        - 有时区：转换到 UTC
        """
        if dt.tzinfo is None:
            return dt.replace(tzinfo=TimeUtil.UTC)
        return dt.astimezone(TimeUtil.UTC)

    @staticmethod
    def is_expired(expire_utc: datetime) -> bool:
        """
        是否已过期（传入 UTC 时间）
        """
        now_utc = TimeUtil.now(TimeUtil.UTC)
        return now_utc >= expire_utc

    @staticmethod
    def is_valid(expire_utc: datetime) -> bool:
        now_utc = TimeUtil.now(TimeUtil.UTC)
        return expire_utc >= now_utc

    @staticmethod
    def remaining_days(expire_utc: datetime) -> int:
        """
        剩余天数
        - 已过期返回 0
        - 不足 1 天按 1 天算
        """
        now_utc = TimeUtil.now(TimeUtil.UTC)

        if expire_utc <= now_utc:
            return 0

        delta = expire_utc - now_utc
        return ceil(delta.total_seconds() / 86400)

    @staticmethod
    def to_local_tz_long(utc_time: datetime | str, tz_name: str = "Asia/Shanghai") -> datetime:
        """
        UTC → 指定时区
        """
        if isinstance(utc_time, str):
            utc_time = TimeUtil.parse(utc_time)
        tz = ZoneInfo(tz_name)
        return utc_time.astimezone(tz)

    @staticmethod
    def to_utc(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        """
        datetime → 字符串（不显示时区）
        """
        return dt.strftime(fmt)

    @staticmethod
    def to_local_tz(
        utc_time: datetime|str,
        tz_name: str = "Asia/Shanghai",
        fmt: str = "%Y-%m-%d %H:%M:%S"
    ) -> str:
        """
        UTC 时间 → 指定时区字符串
        """
        local_time = TimeUtil.to_local_tz_long(utc_time, tz_name)
        return local_time.strftime(fmt)

