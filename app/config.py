"""環境変数だけで mock / live を切り替える。

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
    ekispert_mode: Mode = "mock"

    # memory はテスト専用。開発はエミュレータ、本番は Firestore を使う。
    db_driver: Literal["memory", "firestore"] = "firestore"
    google_cloud_project: str = "shiki-meguri-local"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    ekispert_api_key: str = ""

    # API から agent サービスを呼ぶ経路。inproc はローカル/テスト用。
    agent_transport: Literal["inproc", "http"] = "inproc"
    agent_base_url: str = "http://agent:8081"

    # --- ガバナンス --------------------------------------------
    # 金銭が動く操作は金額にかかわらず本人承認を通す。上限の設定は持たない。
    # events の TTL（式終了 + N日で自動削除）
    event_ttl_days: int = 7
    # 会場到着の余裕（分）
    arrival_buffer_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
