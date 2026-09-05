"""時刻の単一入口。

デモでは「式当日の朝」を再現する必要があるため、時刻は必ずこの Clock 経由で
取得する。`FrozenClock` に差し替えれば当日の時刻を決定的に再現できる。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


class Clock:
    def now(self) -> datetime:
        return datetime.now(JST)


class FrozenClock(Clock):
    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, minutes: int) -> None:
        self._at += timedelta(minutes=minutes)

    def set(self, at: datetime) -> None:
        self._at = at
