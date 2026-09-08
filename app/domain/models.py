"""シキめぐりのドメインモデル（設計書 §6 に対応）。

設計書 §7-2「慶弔情報の最小化」に従い、「誰の式か」「誰が亡くなったか」は
型として持たない。保持するのは日時・会場・服装区分のみ。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 式の区分


class EventType(str, Enum):
    WEDDING = "wedding"  # 結婚式お呼ばれ
    SEIJIN = "seijin"  # 成人式・卒業式
    TOURISM = "tourism"  # 観光着物
    FUNERAL = "funeral"  # 葬儀

    @property
    def label(self) -> str:
        return {
            EventType.WEDDING: "結婚式お呼ばれ",
            EventType.SEIJIN: "成人式・卒業式",
            EventType.TOURISM: "観光着物",
            EventType.FUNERAL: "葬儀",
        }[self]

    @property
    def is_mourning(self) -> bool:
        """弔事。画像保持・提案文の慶弔マナーを切り替える（設計書 §7-1）。"""
        return self is EventType.FUNERAL


class OutfitCategory(str, Enum):
    DRESS = "dress"
    FURISODE = "furisode"
    HAKAMA = "hakama"
    KIMONO = "kimono"
    MOFUKU = "mofuku"

    @property
    def label(self) -> str:
        return {
            OutfitCategory.DRESS: "ドレス",
            OutfitCategory.FURISODE: "振袖",
            OutfitCategory.HAKAMA: "袴",
            OutfitCategory.KIMONO: "着物",
            OutfitCategory.MOFUKU: "喪服",
        }[self]

    @property
    def dressing_minutes(self) -> int:
        """着替え・着付けに要する時間。逆算タイムラインの固定コスト。"""
        return {
            OutfitCategory.DRESS: 20,
            OutfitCategory.FURISODE: 60,
            OutfitCategory.HAKAMA: 50,
            OutfitCategory.KIMONO: 40,
            OutfitCategory.MOFUKU: 15,
        }[self]

    @property
    def needs_dresser(self) -> bool:
        """着付け師の枠取りが要る（＝受取地点が着付け可能店舗に限定される）。"""
        return self in {
            OutfitCategory.FURISODE,
            OutfitCategory.HAKAMA,
            OutfitCategory.KIMONO,
        }


DEFAULT_CATEGORY: dict[EventType, OutfitCategory] = {
    EventType.WEDDING: OutfitCategory.DRESS,
    EventType.SEIJIN: OutfitCategory.FURISODE,
    EventType.TOURISM: OutfitCategory.KIMONO,
    EventType.FUNERAL: OutfitCategory.MOFUKU,
}


# ---------------------------------------------------------------- 利用者


class PersonalColor(str, Enum):
    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"

    @property
    def label(self) -> str:
        return {
            PersonalColor.SPRING: "イエベ春",
            PersonalColor.SUMMER: "ブルベ夏",
            PersonalColor.AUTUMN: "イエベ秋",
            PersonalColor.WINTER: "ブルベ冬",
        }[self]


class PersonalColorResult(BaseModel):
    """YouCam Facial Color Tones の結果。

    設計書 §7-1 に従い、解析元の顔画像は保持せずスコアのみを残す。
    """

    season: PersonalColor
    scores: dict[str, float] = Field(default_factory=dict)
    analyzed_at: datetime
    source_image_destroyed_at: datetime | None = None


class UserProfile(BaseModel):
    """users/{uid}.profile — 自宅住所は保持せず最寄り駅まで。"""

    uid: str
    display_name: str = ""
    home_station: str
    size: str = "M"
    personal_color: PersonalColorResult | None = None


class ChatMessage(BaseModel):
    """messages/{messageId} — 自前チャットの 1 発言。

    外部メッセージングに依存せず、会話も通知も同じコレクションに載せる。
    """

    message_id: str
    uid: str
    role: Literal["user", "agent"]
    text: str
    kind: Literal["info", "consent", "timeline", "return", "error"] = "info"
    event_id: str | None = None
    created_at: datetime


# ---------------------------------------------------------------- 衣装


class RentalState(str, Enum):
    PROPOSED = "proposed"  # 起案（金銭未確定）
    AWAITING_CONSENT = "awaiting_consent"  # 本人承認待ち
    RESERVED = "reserved"  # 予約確定
    PICKED_UP = "picked_up"
    RETURNING = "returning"
    RETURNED = "returned"
    OVERDUE = "overdue"


class OutfitCandidate(BaseModel):
    """試着エージェントが提示する候補。"""

    outfit_id: str
    name: str
    category: OutfitCategory
    color: str
    size: str
    provider: str
    rental_fee_yen: int
    match_score: float = 0.0
    rationale: str | None = None
    tryon_image_url: str | None = None
    image_destroyed_at: datetime | None = None


class Outfit(BaseModel):
    """events/{eventId}.outfit — 選定衣装とレンタル状態。"""

    candidate: OutfitCandidate
    state: RentalState = RentalState.PROPOSED
    reservation_id: str | None = None
    reserved_at: datetime | None = None


# ---------------------------------------------------------------- 受取・返却


PickupKind = Literal["home_delivery", "store", "locker"]


class PickupOption(BaseModel):
    """受取場所の候補（自宅配送 / 店舗 / ロッカー）。"""

    pickup_id: str
    kind: PickupKind
    name: str
    station: str
    walk_minutes: int = 0
    handling_minutes: int = 10  # 受取手続きに要する時間
    fee_yen: int = 0
    opens_at_hour: int = 9
    closes_at_hour: int = 21
    supports_dressing: bool = False  # 着付け対応
    accepts_return: bool = True

    @property
    def label(self) -> str:
        return {
            "home_delivery": "自宅配送",
            "store": "店舗受取",
            "locker": "ロッカー受取",
        }[self.kind]


class ReturnMethod(str, Enum):
    CONVENIENCE_STORE = "convenience_store"
    STORE = "store"
    LOCKER = "locker"
    PICKUP_SERVICE = "pickup_service"

    @property
    def label(self) -> str:
        return {
            ReturnMethod.CONVENIENCE_STORE: "コンビニ発送",
            ReturnMethod.STORE: "店舗返却",
            ReturnMethod.LOCKER: "ロッカー返却",
            ReturnMethod.PICKUP_SERVICE: "集荷依頼",
        }[self]


class ReturnPlan(BaseModel):
    """events/{eventId}.return — 返却期限と手段。"""

    due_at: datetime
    method: ReturnMethod = ReturnMethod.CONVENIENCE_STORE
    place: str | None = None
    completed_at: datetime | None = None
    extension_proposed_minutes: int | None = None
    extension_fee_yen: int | None = None

    def remaining_minutes(self, now: datetime) -> int:
        return int((self.due_at - now).total_seconds() // 60)


class ReturnAlert(BaseModel):
    """返却監視エージェントの起案（提案までで実行はしない）。"""

    alert_id: str
    level: Literal["info", "warn", "critical"]
    message: str
    remaining_minutes: int
    suggested_method: ReturnMethod | None = None
    suggested_place: str | None = None
    extension_fee_yen: int | None = None
    created_at: datetime


# ---------------------------------------------------------------- 動線


class TransitLeg(BaseModel):
    """駅すぱあとから受け取る 1 区間。"""

    from_station: str
    to_station: str
    duration_minutes: int
    fare_yen: int = 0
    transfers: int = 0
    lines: list[str] = Field(default_factory=list)

    @property
    def total_minutes(self) -> int:
        return self.duration_minutes


StepKind = Literal["depart", "transit", "pickup", "dressing", "arrive", "ceremony", "return"]


class TimelineStep(BaseModel):
    """逆算タイムラインの 1 ステップ。"""

    kind: StepKind
    label: str
    place: str
    starts_at: datetime
    ends_at: datetime
    detail: str | None = None

    @property
    def duration_minutes(self) -> int:
        return int((self.ends_at - self.starts_at).total_seconds() // 60)


class RoutePlan(BaseModel):
    """events/{eventId}.route — 開式から逆算した当日タイムライン。"""

    steps: list[TimelineStep] = Field(default_factory=list)
    legs: list[TransitLeg] = Field(default_factory=list)
    generated_at: datetime
    feasible: bool = True
    warning: str | None = None

    @property
    def departure_at(self) -> datetime | None:
        return self.steps[0].starts_at if self.steps else None


# ---------------------------------------------------------------- 同意ゲート


class ConsentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"  # 別の案に差し替えたため取り下げ


class ConsentRequest(BaseModel):
    """金銭・手配の同意ゲート（設計書 §7-3）。

    起案 → 本人承認 → 実行。エージェントは自分で確定できない。
    """

    consent_id: str
    event_id: str
    action: Literal["reserve", "extend", "ship"]
    summary: str
    amount_yen: int
    breakdown: dict[str, int] = Field(default_factory=dict)
    requested_by: str
    requested_at: datetime
    status: ConsentStatus = ConsentStatus.PENDING
    decided_at: datetime | None = None
    note: str | None = None


# ---------------------------------------------------------------- 集約


class EventStatus(str, Enum):
    DRAFT = "draft"
    OUTFIT_PROPOSED = "outfit_proposed"
    AWAITING_CONSENT = "awaiting_consent"
    RESERVED = "reserved"
    ROUTED = "routed"
    IN_PROGRESS = "in_progress"
    RETURN_PENDING = "return_pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Schedule(BaseModel):
    """events/{eventId}.schedule — 式日時・会場・受取・返却期限。"""

    ceremony_start_at: datetime
    ceremony_end_at: datetime | None = None
    venue_name: str
    venue_station: str
    pickup: PickupOption | None = None
    return_due_at: datetime | None = None

    @property
    def effective_end_at(self) -> datetime:
        return self.ceremony_end_at or (self.ceremony_start_at + timedelta(hours=3))


class Event(BaseModel):
    """events/{eventId} の集約ルート。"""

    event_id: str
    uid: str
    type: EventType
    status: EventStatus = EventStatus.DRAFT
    schedule: Schedule
    candidates: list[OutfitCandidate] = Field(default_factory=list)
    outfit: Outfit | None = None
    pickup_options: list[PickupOption] = Field(default_factory=list)
    route: RoutePlan | None = None
    consents: list[ConsentRequest] = Field(default_factory=list)
    return_plan: ReturnPlan | None = None
    alerts: list[ReturnAlert] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    ttl_at: datetime | None = None  # 式終了 + 7日で自動削除

    def pending_consent(self) -> ConsentRequest | None:
        for c in self.consents:
            if c.status is ConsentStatus.PENDING:
                return c
        return None

    def find_consent(self, consent_id: str) -> ConsentRequest | None:
        for c in self.consents:
            if c.consent_id == consent_id:
                return c
        return None


class AuditLog(BaseModel):
    """audit/{logId} — 追記専用（設計書 §6 / §7-4）。"""

    log_id: str
    ts: datetime
    event_id: str | None = None
    agent: str
    action: str
    basis: str
    consent_ref: str | None = None
    image_destroyed_at: datetime | None = None
    payload: dict = Field(default_factory=dict)
