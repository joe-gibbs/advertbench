import json
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .db import connection
from .settings import ROOT_DIR


class AdvertModel(BaseModel):
    slug: str
    display_name: str = Field(alias="displayName")
    release_date: str = Field(alias="releaseDate")
    settings: dict[str, Any] = Field(default_factory=dict)


class AdvertAd(BaseModel):
    key: str
    prompt: str

    def render_prompt(self) -> str:
        return self.prompt


class AdSize(BaseModel):
    key: str
    label: str
    width: int
    height: int
    ratio: str


class AdvertConfig(BaseModel):
    models: list[AdvertModel]
    ads: list[AdvertAd]
    ad_sizes: list[AdSize] = Field(alias="adSizes")

    model_config = {"populate_by_name": True}


def load_advert_config() -> AdvertConfig:
    raw = (ROOT_DIR / "config" / "advertbench.json").read_text(encoding="utf-8")
    return AdvertConfig.model_validate(json.loads(raw))


def config_as_json(config: AdvertConfig) -> dict[str, Any]:
    return config.model_dump(by_alias=True)


def sync_models_from_config() -> AdvertConfig:
    config = load_advert_config()
    with connection() as conn:
        for model in config.models:
            conn.execute(
                """
                INSERT INTO models (slug, display_name, metadata)
                VALUES (%s, %s, %s)
                ON CONFLICT (slug)
                DO UPDATE SET
                  display_name = EXCLUDED.display_name,
                  metadata = EXCLUDED.metadata,
                  updated_at = now()
                """,
                (
                    model.slug,
                    model.display_name,
                    Jsonb({"releaseDate": model.release_date, "settings": model.settings}),
                ),
            )
        conn.commit()
    return config
