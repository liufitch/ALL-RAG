from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus

from omegaconf import OmegaConf
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
APP_ENV = os.getenv("APP_ENV", "dev").lower()
VALID_ENVS = {"dev", "qa", "prod"}
DBType = Literal["sqlite", "postgresql", "mysql", "oceanbase", "seekdb"]
VectorStoreType = Literal["milvus", "pgvector", "qdrant", "weaviate", "opensearch", "elasticsearch"]

if APP_ENV not in VALID_ENVS:
    raise ValueError(f"APP_ENV must be one of {sorted(VALID_ENVS)}, got: {APP_ENV}")


def _load_yaml_defaults() -> dict[str, Any]:
    config_path = BASE_DIR / f"config_{APP_ENV}.yaml"
    if not config_path.exists():
        return {}
    config = OmegaConf.load(config_path)
    data = OmegaConf.to_container(config, resolve=True)
    return data if isinstance(data, dict) else {}


class DatabaseSettings(BaseModel):
    type: DBType = "sqlite"
    host: str = "localhost"
    port: int = 0
    database: str = "graph_rag"
    username: str = ""
    password: str = ""
    charset: str = "utf8mb4"
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout: int = 30
    pool_recycle: int = 1800
    extra_params: dict[str, Any] = Field(default_factory=dict)
    sqlite_path: str = "./data/graph_rag.db"

  # 判断数据库使用
    @property
    def scheme(self) -> str:
        return {
            "sqlite": "sqlite+aiosqlite",
            "postgresql": "postgresql+asyncpg",
            "mysql": "mysql+asyncmy",
            "oceanbase": "mysql+asyncmy",
            "seekdb": "postgresql+asyncpg",
        }[self.type]

   #根据scheme 决定使用mysql还是pg数据库
    @property
    def uri(self) -> str:
        if self.type == "sqlite":
            return f"{self.scheme}:///{self.sqlite_path}"

        user = quote_plus(self.username)
        password = quote_plus(self.password)
        auth = user
        if password:
            auth = f"{auth}:{password}"

        query = ""
        params = dict(self.extra_params)
        if self.type in {"mysql", "oceanbase"} and self.charset:
            params.setdefault("charset", self.charset)
        if params:
            query = "?" + "&".join(f"{key}={value}" for key, value in params.items())
        return f"{self.scheme}://{auth}@{self.host}:{self.port}/{self.database}{query}"

    @property
    def engine_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"echo": self.echo}
        if self.type != "sqlite":
            options.update(
                pool_size=self.pool_size,
                max_overflow=self.max_overflow,
                pool_timeout=self.pool_timeout,
                pool_recycle=self.pool_recycle,
            )
        return options


class VectorStoreSettings(BaseModel):
    provider: VectorStoreType = "milvus"
    host: str = "localhost"
    port: int = 19530
    database: str = "default"
    collection_prefix: str = "graph_rag"
    connect_timeout: int = 5
    enabled: bool = True
    user: str = ""
    password: str = ""
    token: str = ""
    uri: str = ""
    extra_params: dict[str, Any] = Field(default_factory=dict)


_yaml_defaults = _load_yaml_defaults()

#- BaseSettings 负责“从环境变量/.env 里取值”
#- Pydantic 负责“把这些值按字段名塞进 Settings 对象”
class Settings(BaseSettings):
    app_name: str = "Graph RAG"
    secret_key: str = "dev-secret-key"
    debug: bool = False
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    vector_store: VectorStoreSettings = Field(default_factory=VectorStoreSettings)

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env" if APP_ENV == "dev" else None,
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore", #- 环境变量里多出来、模型里没有定义的字段直接忽略，不报错
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            cls._yaml_source(),
            file_secret_settings,
        )

    @classmethod
    def _yaml_source(cls):
        def inner() -> dict[str, Any]:
            return _yaml_defaults

        return inner

    @property
    def database_type(self) -> DBType:
        return self.database.type

    @property
    def sqlalchemy_database_uri(self) -> str:
        return self.database.uri

    @property
    def sqlalchemy_engine_options(self) -> dict[str, Any]:
        return self.database.engine_options


settings = Settings()
