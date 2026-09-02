"""式ムービー工房（設計書 §11 追加案 / GMI Cloud）。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.agents.deps import Deps
from app.agents.orchestrator import Orchestrator
from app.domain.models import ConsentStatus, EventType, MovieStatus
from tests.conftest import NOW

CEREMONY = NOW + timedelta(hours=5)


async def _event(orchestrator: Orchestrator) -> str:
    await orchestrator.register_user("u1", home_station="吉祥寺")
    event = await orchestrator.create_event(
        uid="u1",
        event_type=EventType.WEDDING,
        ceremony_start_at=CEREMONY,
        ceremony_end_at=CEREMONY + timedelta(hours=3),
        venue_name="ベイサイド迎賓館",
        venue_station="品川",
    )
    return event.event_id


async def _with_photos(orchestrator: Orchestrator, third_party: bool = False):
    event_id = await _event(orchestrator)
    return await orchestrator.add_movie_photos(
        event_id,
        [
            ("photo-1", "出会いのころ", False),
            ("photo-2", "旅行", third_party),
            ("photo-3", "プロポーズ", False),
        ],
    )


# ---------------------------------------------------------------- 素材と同意


async def test_写真の実体は保持せず参照だけを持つ(orchestrator: Orchestrator, deps: Deps):
    event = await _with_photos(orchestrator)

    assert event.movie is not None
    assert [p.image_ref for p in event.movie.photos] == ["photo-1", "photo-2", "photo-3"]
    log = next(log for log in await deps.audit.list() if log.action == "add_photos")
    assert log.payload["source_image_retained"] is False


async def test_第三者が写る写真は同意が確認されるまで制作に進めない(
    orchestrator: Orchestrator,
):
    event = await _with_photos(orchestrator, third_party=True)
    assert event.movie.status is MovieStatus.CONSENT_REQUIRED
    assert len(event.movie.pending_consent_photos()) == 1

    with pytest.raises(ValueError, match="利用同意"):
        await orchestrator.propose_movie(event.event_id, "生い立ち")


async def test_同意を確認すれば制作に進める(orchestrator: Orchestrator, deps: Deps):
    event = await _with_photos(orchestrator, third_party=True)
    photo_id = event.movie.pending_consent_photos()[0].photo_id

    event = await orchestrator.confirm_movie_photo_consent(event.event_id, [photo_id])
    assert event.movie.status is MovieStatus.DRAFT
    assert event.movie.pending_consent_photos() == []

    log = next(
        log for log in await deps.audit.list() if log.action == "photo_consent_confirmed"
    )
    assert log.payload["photo_ids"] == [photo_id]


# ---------------------------------------------------------------- 同意ゲート


async def test_承認前は生成されない(orchestrator: Orchestrator, deps: Deps):
    event = await _with_photos(orchestrator)
    event, consent = await orchestrator.propose_movie(event.event_id, "生い立ち")

    assert consent.action == "movie"
    assert consent.status is ConsentStatus.PENDING
    # 3シーン × 300円 + BGM 500円
    assert consent.amount_yen == 3 * 300 + 500
    assert event.movie.status is MovieStatus.AWAITING_CONSENT
    assert len(event.movie.scenes) == 3
    assert all(s.video_url is None for s in event.movie.scenes)
    assert event.movie.bgm_url is None

    log = next(log for log in await deps.audit.list() if log.action == "propose_movie")
    assert log.payload["autonomous_execution"] is False


async def test_承認するとGMI_Cloudで生成される(orchestrator: Orchestrator, deps: Deps):
    event = await _with_photos(orchestrator)
    event, consent = await orchestrator.propose_movie(event.event_id, "生い立ち")
    event = await orchestrator.decide_consent(event.event_id, consent.consent_id, True)

    movie = event.movie
    assert movie.status is MovieStatus.COMPLETED
    assert all(s.video_url for s in movie.scenes)
    assert all(p.restored_url for p in movie.photos)
    assert movie.bgm_url is not None

    # 使ったモデルが監査ログから追える
    actions = [log.action for log in await deps.audit.list()]
    assert actions.count("render_scene") == 3
    assert "movie_completed" in actions
    scene_log = next(
        log for log in await deps.audit.list() if log.action == "render_scene"
    )
    assert scene_log.payload["watermark"] == "AI生成"


async def test_却下すると生成されない(orchestrator: Orchestrator):
    event = await _with_photos(orchestrator)
    event, consent = await orchestrator.propose_movie(event.event_id, "生い立ち")
    event = await orchestrator.decide_consent(event.event_id, consent.consent_id, False)

    assert event.movie.status is MovieStatus.DRAFT
    assert all(s.video_url is None for s in event.movie.scenes)
    assert event.movie.bgm_url is None


# ---------------------------------------------------------------- ガバナンス


async def test_生成物はAI生成の表示を持つ(orchestrator: Orchestrator):
    event = await _with_photos(orchestrator)
    event, consent = await orchestrator.propose_movie(event.event_id, "生い立ち")
    event = await orchestrator.decide_consent(event.event_id, consent.consent_id, True)

    assert event.movie.watermark == "AI生成"


async def test_素材と生成物は本体のTTLで消える(orchestrator: Orchestrator, deps: Deps, clock):
    """設計書 §11「式後の素材自動削除（本体のTTLポリシーに準拠）」。"""

    event = await _with_photos(orchestrator)
    event, consent = await orchestrator.propose_movie(event.event_id, "生い立ち")
    event = await orchestrator.decide_consent(event.event_id, consent.consent_id, True)

    # ムービーは Event の中にあるので、別の削除経路を持たない
    assert event.movie.delete_after == event.ttl_at

    clock.set(event.ttl_at + timedelta(minutes=1))
    result = await orchestrator.sweep()
    assert event.event_id in result["purged"]
    assert await deps.repo.get_event(event.event_id) is None


async def test_チャットからムービーの制作費を承認できる(orchestrator: Orchestrator):
    event = await _with_photos(orchestrator)
    event, consent = await orchestrator.propose_movie(event.event_id, "生い立ち")

    messages, event = await orchestrator.handle_message("u1", "承認します")
    assert event.movie.status is MovieStatus.COMPLETED
    assert any("ムービーができました" in m.text for m in messages if m.role == "agent")
