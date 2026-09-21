"""Gemini アダプタの検証。実 API は叩かない。"""

from __future__ import annotations

from app.adapters.llm import GeminiLlmClient, StubLlmClient, build_llm_client
from app.config import Settings

CTX = {"name": "レースワンピース", "color": "ラベンダー", "size": "M", "event_label": "結婚式お呼ばれ"}


class _Broken:
    """generate_content が必ず失敗する Gemini クライアントの代役。"""

    class aio:  # noqa: N801 — genai.Client の形に合わせる
        class models:  # noqa: N801
            @staticmethod
            async def generate_content(**_):
                raise RuntimeError("429 RESOURCE_EXHAUSTED")


class _Empty:
    class aio:  # noqa: N801
        class models:  # noqa: N801
            @staticmethod
            async def generate_content(**_):
                return type("Res", (), {"text": None})()


async def test_生成に失敗しても定型文で返し手配を止めない():
    client = GeminiLlmClient("dummy-key", "gemini-3.7-flash")
    client._client = _Broken()

    text = await client.compose(purpose="outfit_rationale", context=CTX)

    stub = await StubLlmClient().compose(purpose="outfit_rationale", context=CTX)
    assert text == stub
    assert text  # 空で返さない


async def test_空の応答も定型文で埋める():
    client = GeminiLlmClient("dummy-key", "gemini-3.7-flash")
    client._client = _Empty()

    assert await client.compose(purpose="outfit_rationale", context=CTX)


def test_キーが無いうちはGeminiを使わない():
    assert isinstance(build_llm_client(Settings(gemini_mode="live")), StubLlmClient)
    live = build_llm_client(Settings(gemini_mode="live", gemini_api_key="k"))
    assert isinstance(live, GeminiLlmClient)
