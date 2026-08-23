"""Application settings, loaded from environment / .env.

Keep this the single source of configuration. Everything else imports `settings`.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GitHub App auth (preferred). PAT is a local-dev fallback only.
    gh_app_id: str | None = None
    gh_app_private_key_path: str | None = None
    gh_app_installation_id: str | None = None
    gh_pat: str | None = None

    # Storage
    database_url: str = "sqlite:///./data/explorer.db"

    # Rate limiter (requests/minute per bucket). `search` is the system ceiling.
    # GitHub's `code_search` resource allows 10 req/min authenticated (verified from
    # x-ratelimit-limit headers) — stay safely under it or every collection drowns
    # in 429s and secondary abuse cooldowns.
    rate_search_rpm: int = 8
    rate_core_rpm: int = 80
    rate_graphql_rpm: int = 80

    # Discovery limits. `sample_above` is the reported-match count past which a query is
    # too broad to collect in full: we keep the first page-set and tell the user to
    # narrow it, rather than spending the whole call budget proving it can't be done.
    discovery_call_budget: int = 300
    discovery_sample_above: int = 25_000

    # API server
    api_host: str = "127.0.0.1"
    api_port: int = 8787

    # Download manager (aria2). RPC binds to loopback; secret is generated per run.
    download_dir: str = "~/Data/datasets/unity-repo-corpus"
    aria2_rpc_port: int = 6801
    aria2_max_connections: int = 8  # per server; keep modest to respect GitHub limits
    downloads_autostart: bool = True  # tests set false to avoid spawning a real aria2c

    @property
    def has_app_auth(self) -> bool:
        return bool(self.gh_app_id and self.gh_app_private_key_path and self.gh_app_installation_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
