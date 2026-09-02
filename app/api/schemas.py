"""API の入出力スキーマ。

ドメインモデルをそのまま外に出すのは読み取り系のみ。書き込み系は
「収集しない情報」を型として受け取らないための最小フィールドにする（設計書 §7-2）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.models import EventType


class RegisterUserRequest(BaseModel):
    uid: str
    home_station: str
    size: str = "M"
    display_name: str = ""


class ChatRequest(BaseModel):
    """自前チャットへの発言。"""

    uid: str = "demo-user"
    text: str = Field(min_length=1, max_length=500)


class CreateEventRequest(BaseModel):
    uid: str
    ceremony_start_at: datetime
    venue_name: str
    venue_station: str
    ceremony_end_at: datetime | None = None
    # type を省略した場合は message からシーン判定する。
    type: EventType | None = None
    message: str | None = None


class ProposeOutfitsRequest(BaseModel):
    # 画像そのものではなく、アップロード済み一時領域への参照だけを受け取る。
    image_ref: str | None = None
    limit: int = Field(default=3, ge=1, le=5)


class ProposeReservationRequest(BaseModel):
    outfit_id: str


class ConsentDecisionRequest(BaseModel):
    approved: bool
    note: str | None = None


class MoviePhotoInput(BaseModel):
    """素材写真。画像そのものではなく一時領域への参照を受け取る（設計書 §7-1）。"""

    image_ref: str
    caption: str | None = None
    # 設計書 §11 ガバナンス: 第三者が写るなら利用同意の確認が要る
    contains_third_party: bool = False


class AddMoviePhotosRequest(BaseModel):
    photos: list[MoviePhotoInput] = Field(min_length=1, max_length=20)


class PhotoConsentRequest(BaseModel):
    photo_ids: list[str] = Field(min_length=1)
    confirmed: bool = True


class ProposeMovieRequest(BaseModel):
    theme: str = Field(min_length=1, max_length=100)


class DisruptRequest(BaseModel):
    line: str
    delay_minutes: int = Field(ge=0, le=180)


class SeedRequest(BaseModel):
    """デモ用シナリオの投入。"""

    scenario: EventType = EventType.WEDDING
    uid: str = "demo-user"
    hours_until_ceremony: int = Field(default=5, ge=1, le=72)
