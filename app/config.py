"""環境変数だけで mock / live を切り替える（設計書 §8）。

キーが揃うまでは全て mock で完結し、`docker compose up` だけで
デモシナリオが最後まで通ることを保証する。
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["mock", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    port: int = 8080

    # 外部サービスの実接続切替
    gemini_mode: Mode = "mock"
    youcam_mode: Mode = "mock"
    ekispert_mode: Mode = "mock"
    rental_mode: Mode = "mock"
    # 設計書 §11 追加案（式ムービー工房）
    gmi_mode: Mode = "mock"

    # memory はテスト専用。開発はエミュレータ、本番は Firestore を使う。
    db_driver: Literal["memory", "firestore"] = "firestore"
    google_cloud_project: str = "shiki-meguri-local"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    youcam_api_key: str = ""
    youcam_secret_key: str = ""
    ekispert_api_key: str = ""
    ekispert_mcp_url: str = ""

    # --- GMI Cloud（設計書 §11）------------------------------------------
    gmi_api_key: str = ""
    gmi_base_url: str = "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey/requests"
    gmi_restore_model: str = "bria-fibo-restore"
    gmi_restyle_model: str = "bria-fibo-restyle"
    gmi_video_model: str = "Kling-Image2Video-V2.1-Pro"
    gmi_music_model: str = "minimax-music-2.5"
    # 動画生成は非同期。ポーリングの上限（秒）。
    gmi_poll_timeout_seconds: int = 300

    # API から agent サービスを呼ぶ経路。inproc はローカル/テスト用。
    agent_transport: Literal["inproc", "http"] = "inproc"
    agent_base_url: str = "http://agent:8081"

    # --- ガバナンス（設計書 §7）-------------------------------------------
    # 金銭確定はエージェント権限外。ここを超える提案は必ず同意ゲートを通す。
    agent_spend_limit_yen: int = 0
    # 試着画像の保持上限（秒）。弔事はさらに短縮する。
    tryon_image_ttl_seconds: int = 300
    mourning_image_ttl_seconds: int = 60
    # events の TTL（式終了 + N日で自動削除）
    event_ttl_days: int = 7
    # 会場到着の余裕（分）
    arrival_buffer_minutes: int = 15

    # --- 式ムービー工房の料金（設計書 §11。モックの想定単価）----------------
    movie_scene_price_yen: int = 300
    movie_bgm_price_yen: int = 500
    movie_scene_seconds: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
