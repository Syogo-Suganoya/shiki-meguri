"""Gemini アダプタ。

提案文の生成のみを担い、判断そのものはドメイン側で決める。LLM が落ちても
サービスが止まらないよう、stub は同じ purpose に対して常に文章を返す。

`mourning=True`（弔事）のときは、祝いの語彙を使わない文面に切り替える
（慶弔マナーを考慮する）。
"""

from __future__ import annotations

import abc
import logging

from app.config import Settings

logger = logging.getLogger(__name__)

Purpose = str


class LlmClient(abc.ABC):
    @abc.abstractmethod
    async def compose(
        self, *, purpose: Purpose, context: dict, mourning: bool = False
    ) -> str: ...


class StubLlmClient(LlmClient):
    async def compose(
        self, *, purpose: Purpose, context: dict, mourning: bool = False
    ) -> str:
        builder = _TEMPLATES.get(purpose)
        if builder is None:
            return ""
        return builder(context, mourning)


class GeminiLlmClient(LlmClient):
    """Gemini で文面を書く。失敗したら stub の文面に落とす。

    提案文は判断の添え物なので、生成に失敗しても手配の流れは止めない。
    """

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai  # noqa: PLC0415

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._fallback = StubLlmClient()

    async def compose(
        self, *, purpose: Purpose, context: dict, mourning: bool = False
    ) -> str:
        try:
            text = await self._generate(purpose, context, mourning)
        except Exception:  # noqa: BLE001 — 生成の失敗で手配を止めない
            logger.exception("Gemini の生成に失敗したため定型文に落とします: %s", purpose)
            text = ""
        return text or await self._fallback.compose(
            purpose=purpose, context=context, mourning=mourning
        )

    async def _generate(self, purpose: Purpose, context: dict, mourning: bool) -> str:
        tone = (
            "弔事です。祝意を示す語・華やかな語を使わず、簡潔で落ち着いた敬語で書いてください。"
            if mourning
            else "慶事です。前向きで簡潔な敬語で書いてください。"
        )
        prompt = (
            "あなたは冠婚葬祭レンタルの案内エージェントです。"
            f"{tone}\n"
            "個人名・故人・新郎新婦などの人物情報は与えられていないため触れないでください。\n"
            f"目的: {purpose}\n"
            f"事実: {context}\n"
            "120文字以内の日本語で出力してください。"
        )
        res = await self._client.aio.models.generate_content(
            model=self._model, contents=prompt
        )
        return (res.text or "").strip()


# ------------------------------------------------------------------ stub 文面


def _outfit_rationale(ctx: dict, mourning: bool) -> str:
    """なぜこれを薦めるかだけを書く。

    名前・色・サイズは画面のすぐ上に出ているので繰り返さない。
    """

    if mourning:
        return "式に適した無地の一式です。当日中の手配もできます。"
    # 3件が同じ文になると読む意味が無くなるので、候補ごとに違う点に触れる。
    return f"{ctx['color']}は{ctx['event_label']}で浮かない色です。{ctx['size']}サイズで用意できます。"


def _timeline_summary(ctx: dict, mourning: bool) -> str:
    head = "当日の流れです。" if mourning else "当日の逆算プランができました。"
    return (
        f"{head}{ctx['departure']}に{ctx['home_station']}を出発し、"
        f"{ctx['pickup_label']}で受け取り、{ctx['ceremony']}の開式に間に合います。"
    )


def _return_reminder(ctx: dict, mourning: bool) -> str:
    return (
        f"返却期限まで残り{ctx['remaining']}分です。"
        f"{ctx['method_label']}（{ctx['place']}）での返却をおすすめします。"
    )


def _consent_summary(ctx: dict, mourning: bool) -> str:
    return (
        f"{ctx['summary']} 合計{ctx['amount']:,}円です。"
        "内容をご確認のうえ、承認をお願いします（承認まで確定しません）。"
    )


_TEMPLATES = {
    "outfit_rationale": _outfit_rationale,
    "timeline_summary": _timeline_summary,
    "return_reminder": _return_reminder,
    "consent_summary": _consent_summary,
}


def build_llm_client(settings: Settings) -> LlmClient:
    if settings.gemini_mode == "live" and settings.gemini_api_key:
        return GeminiLlmClient(settings.gemini_api_key, settings.gemini_model)
    return StubLlmClient()
