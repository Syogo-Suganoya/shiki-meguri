"""API の入出力スキーマ。

ドメインモデルをそのまま外に出すのは読み取り系のみ。書き込み系は
「収集しない情報」を型として受け取らないための最小フィールドにする。
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.domain.models import EventType
from app.infra.clock import JST


def to_jst(value: datetime | None) -> datetime | None:
    """受け取った時刻を JST に揃える。

    画面は ISO 文字列（UTC）で送ってくるが、提案文も逆算タイムラインも
    `%H:%M` をそのまま日本時間として読ませる。ここで直しておかないと、
    保存値は正しいのに文面だけ 9 時間ずれる。タイムゾーンなしは JST とみなす。
    """

    if value is None:
        return None
    return value.replace(tzinfo=JST) if value.tzinfo is None else value.astimezone(JST)


def plain_text(value: str) -> str:
    """自由入力の文字列から改行などの制御文字を落とす。

    会場名はチャットの文面や Gemini への材料に入るので、行を分けて
    別の指示に見せかける書き方を入口で潰しておく。
    """

    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()


class RegisterUserRequest(BaseModel):
    uid: str
    home_station: str
    size: str = "M"
    display_name: str = Field(default="", max_length=30)

    _plain = field_validator("display_name")(plain_text)


class ChatRequest(BaseModel):
    """自前チャットへの発言。"""

    uid: str = "demo-user"
    text: str = Field(min_length=1, max_length=500)


class CreateEventRequest(BaseModel):
    uid: str
    ceremony_start_at: datetime
    venue_name: str = Field(max_length=40)
    venue_station: str
    ceremony_end_at: datetime | None = None
    # type を省略した場合は message からシーン判定する。
    type: EventType | None = None
    message: str | None = None

    _jst = field_validator("ceremony_start_at", "ceremony_end_at")(to_jst)
    _plain = field_validator("venue_name")(plain_text)


class StartIntakeRequest(BaseModel):
    """入力フォームからの受付。利用者登録と式の登録をまとめて受ける。"""

    uid: str = "demo-user"
    type: EventType
    ceremony_start_at: datetime
    venue_station: str
    venue_name: str = Field(default="", max_length=40)
    home_station: str
    size: str = "M"

    _jst = field_validator("ceremony_start_at")(to_jst)
    _plain = field_validator("venue_name")(plain_text)


class ProposeOutfitsRequest(BaseModel):
    limit: int = Field(default=3, ge=1, le=5)


class ProposeReservationRequest(BaseModel):
    outfit_id: str
    # 受取場所の指名。省略すると総コスト最良の案を起案する。
    pickup_id: str | None = None
    # 予約確定済みのものを差し替える意思表示。既存の予約は取り消される。
    replace: bool = False


class ConsentDecisionRequest(BaseModel):
    approved: bool
    note: str | None = None


class SeedRequest(BaseModel):
    """デモ用シナリオの投入。"""

    scenario: EventType = EventType.WEDDING
    uid: str = "demo-user"
    hours_until_ceremony: int = Field(default=5, ge=1, le=72)
