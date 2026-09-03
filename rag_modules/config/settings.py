from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus

from omegaconf import OmegaConf
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from rag_modules.upload_formats import SUPPORTED_UPLOAD_EXTENSIONS

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


class EmbeddingModelDefinition(BaseModel):
    id: str
    model: str
    display_name: str
    enabled: bool = True
    batch_size: int = Field(default=32, ge=1, le=512)
    max_input_characters: int = Field(default=8000, ge=100)
    request_timeout: int = Field(default=60, ge=1)
    dimensions: int | None = Field(default=None, ge=1, le=32768)


class EmbeddingSettings(BaseModel):
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = "http://localhost:8001/v1"
    api_key: SecretStr = SecretStr("")
    default_model: str = "bge-m3"
    models: list[EmbeddingModelDefinition] = Field(
        default_factory=lambda: [
            EmbeddingModelDefinition(
                id="bge-m3",
                model="bge-m3",
                display_name="BGE-M3",
            )
        ]
    )
    max_retries: int = Field(default=3, ge=0, le=10)

    @model_validator(mode="after")
    def validate_default_model(self):
        if not any(item.id == self.default_model and item.enabled for item in self.models):
            raise ValueError("default embedding model must exist and be enabled")
        return self

    def get_model(self, model_id: str) -> EmbeddingModelDefinition:
        for item in self.models:
            if item.id == model_id and item.enabled:
                return item
        raise ValueError(f"unknown or disabled embedding model: {model_id}")


class ObjectStorageSettings(BaseModel):
    provider: Literal["minio"] = "minio"
    endpoint: str = "localhost:9000"
    access_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    secure: bool = False
    bucket: str = "graph-rag-uploads"


class BrokerSettings(BaseModel):
    host: str = "localhost"
    port: int = Field(default=5672, ge=1, le=65535)
    username: SecretStr = SecretStr("graph_rag")
    password: SecretStr = SecretStr("")
    virtual_host: str = "graph_rag"

    @property
    def url(self) -> str:
        username = quote_plus(self.username.get_secret_value())
        password = quote_plus(self.password.get_secret_value())
        credentials = username
        if password:
            credentials = f"{credentials}:{password}"
        return f"amqp://{credentials}@{self.host}:{self.port}/{quote_plus(self.virtual_host)}"


class UploadSettings(BaseModel):
    max_file_size_mb: int = Field(default=50, ge=1)
    max_decompressed_size_mb: int = Field(default=200, ge=1)
    allowed_extensions: tuple[str, ...] = SUPPORTED_UPLOAD_EXTENSIONS

    @field_validator("allowed_extensions")
    @classmethod
    def validate_allowed_extensions(cls, extensions: tuple[str, ...]):
        normalized = tuple(
            dict.fromkeys(extension.strip().lower() for extension in extensions)
        )
        unsupported = sorted(set(normalized).difference(SUPPORTED_UPLOAD_EXTENSIONS))
        if unsupported:
            raise ValueError(
                f"unsupported upload extensions: {', '.join(unsupported)}"
            )
        return normalized


class ParserSettings(BaseModel):
    max_pdf_pages: int = Field(default=500, ge=1)
    max_rows: int = Field(default=100_000, ge=1)
    max_columns: int = Field(default=1_000, ge=1)
    max_cell_characters: int = Field(default=32_768, ge=1)
    max_spreadsheet_xml_nodes: int = Field(default=2_000_000, ge=1)
    max_physical_cells: int = Field(default=1_000_000, ge=1)
    max_row_coordinate: int = Field(default=100_000, ge=1)
    max_column_coordinate: int = Field(default=16_384, ge=1)
    max_merged_cell_area: int = Field(default=100_000, ge=1)
    max_total_merged_cell_area: int = Field(default=1_000_000, ge=1)

    @model_validator(mode="after")
    def validate_merged_cell_limits(self):
        if self.max_total_merged_cell_area < self.max_merged_cell_area:
            raise ValueError(
                "total merged-cell area must be greater than or equal to the single-range limit"
            )
        if self.max_total_merged_cell_area > self.max_physical_cells:
            raise ValueError(
                "total merged-cell area must be less than or equal to the physical-cell limit"
            )
        return self


class PreviewSettings(BaseModel):
    max_documents: int = Field(default=20, ge=1)
    max_chunks: int = Field(default=100, ge=1)
    timeout_seconds: int = Field(default=30, ge=1)


class IndexingSettings(BaseModel):
    default_indexing_technique: Literal["high_quality", "economy"] = "high_quality"
    general_max_chunk_length: int = Field(default=1024, ge=1)
    general_overlap: int = Field(default=100, ge=0)
    parent_max_chunk_length: int = Field(default=4096, ge=1)
    child_max_chunk_length: int = Field(default=512, ge=1)
    child_overlap: int = Field(default=50, ge=0)


_yaml_defaults = _load_yaml_defaults()

#- BaseSettings 负责“从环境变量/.env 里取值”
#- Pydantic 负责“把这些值按字段名塞进 Settings 对象”
class Settings(BaseSettings):
    app_name: str = "Graph RAG"
    secret_key: str = "dev-secret-key"
    debug: bool = False
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    vector_store: VectorStoreSettings = Field(default_factory=VectorStoreSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    object_storage: ObjectStorageSettings = Field(default_factory=ObjectStorageSettings)
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    parser: ParserSettings = Field(default_factory=ParserSettings)
    preview: PreviewSettings = Field(default_factory=PreviewSettings)
    indexing: IndexingSettings = Field(default_factory=IndexingSettings)

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
