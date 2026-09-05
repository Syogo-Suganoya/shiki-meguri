"""チャット入力の解釈（設計書 §4 Orchestrator「会話の受付」）。

会話の途中経過を別に持たず、**利用者の発言履歴を毎回読み直して**必要な情報
（シーン・会場・日時）を組み立てる。状態を二重に持たない分、
「式が終わって返すまで」の長い会話でも復元がずれない。

`GEMINI_MODE=live` では Gemini による抽出に置き換えられるが、MVP は
外部サービスなしで完結させるため規則ベースで実装する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from app.adapters.ekispert import STATION_COORDS
from app.domain.models import EventType

# 数字の全角・漢数字を候補番号として拾う。
_INDEX_CHARS = {
    "1": 1, "１": 1, "一": 1,
    "2": 2, "２": 2, "二": 2,
    "3": 3, "３": 3, "三": 3,
    "4": 4, "４": 4, "四": 4,
    "5": 5, "５": 5, "五": 5,
}

APPROVE_WORDS = ("承認", "はい", "お願い", "おねがい", "確定", "ok", "了解", "いいよ")
REJECT_WORDS = ("却下", "いいえ", "やめ", "キャンセル", "見送")
RETURN_WORDS = ("返却", "返す", "返し")
STATUS_WORDS = ("状況", "確認", "いま", "今", "予定")

_HOME_MARKERS = ("最寄り", "最寄", "自宅", "家から", "うち")
_TIME_RE = re.compile(r"(\d{1,2})\s*(?:時|:|：)\s*(\d{1,2})?")
_MD_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_VENUE_NAME_RE = re.compile(r"会場は\s*([^\s。、]+)")


@dataclass(frozen=True)
class Slots:
    """会話から復元した申し込み内容。"""

    scene: EventType | None = None
    scene_basis: str | None = None
    home_station: str | None = None
    venue_station: str | None = None
    venue_name: str | None = None
    start_at: datetime | None = None

    @property
    def ready(self) -> bool:
        return self.venue_station is not None and self.start_at is not None

    def missing(self) -> list[str]:
        lack = []
        if self.venue_station is None:
            lack.append("会場の最寄り駅")
        if self.start_at is None:
            lack.append("式の日時")
        return lack


def parse_history(texts: list[str], now: datetime, detect_scene) -> Slots:
    """利用者の発言を古い順に畳み込む。後の発言が前の発言を上書きする。"""

    slots = Slots()
    for text in texts:
        slots = _merge(slots, text, now, detect_scene)
    return slots


def _merge(slots: Slots, text: str, now: datetime, detect_scene) -> Slots:
    update: dict = {}

    if _has_scene_keyword(text, detect_scene):
        scene, basis = detect_scene(text)
        update["scene"] = scene
        update["scene_basis"] = basis

    home, venue = find_stations(text)
    if home:
        update["home_station"] = home
    if venue:
        update["venue_station"] = venue

    name = _VENUE_NAME_RE.search(text)
    if name:
        update["venue_name"] = name.group(1)

    when = parse_datetime(text, now)
    if when:
        update["start_at"] = when

    return replace(slots, **update) if update else slots


def _has_scene_keyword(text: str, detect_scene) -> bool:
    _, basis = detect_scene(text)
    return "キーワード" in basis


def find_stations(text: str) -> tuple[str | None, str | None]:
    """既知の駅名を拾い、「最寄り／自宅」の語が直前にあれば自宅側とみなす。"""

    home: str | None = None
    venue: str | None = None
    for station in sorted(STATION_COORDS, key=len, reverse=True):
        index = text.find(station)
        if index < 0:
            continue
        prefix = text[max(0, index - 8) : index]
        if any(marker in prefix for marker in _HOME_MARKERS):
            home = home or station
        else:
            venue = venue or station
    return home, venue


def parse_datetime(text: str, now: datetime) -> datetime | None:
    """「明日16時」「10月10日 14:30」などを絶対時刻に直す。"""

    time_match = _TIME_RE.search(text)
    if not time_match:
        return None
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    md = _MD_RE.search(text)
    if md:
        base = now.replace(month=int(md.group(1)), day=int(md.group(2)))
    elif "明後日" in text or "あさって" in text:
        base = now + timedelta(days=2)
    elif "明日" in text or "あした" in text:
        base = now + timedelta(days=1)
    elif "今日" in text or "本日" in text or "きょう" in text:
        base = now
    else:
        base = now

    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if md is None and not _has_day_word(text) and candidate <= now:
        # 日付の指定がなく、その時刻を今日はもう過ぎている場合は翌日とみなす。
        candidate += timedelta(days=1)
    return candidate


def _has_day_word(text: str) -> bool:
    return any(w in text for w in ("今日", "本日", "きょう", "明日", "あした", "明後日", "あさって"))


def parse_choice(text: str, count: int) -> int | None:
    """「2番」「2つ目」などから候補の添字（0 起点）を得る。"""

    for char, value in _INDEX_CHARS.items():
        if char in text and 1 <= value <= count:
            return value - 1
    return None


def contains(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)
