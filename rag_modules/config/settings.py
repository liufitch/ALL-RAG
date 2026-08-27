from pydantic_settings import BaseSettings, SettingsConfigDict
from omegaconf import OmegaConf
from pathlib import Path
from pydantic import Field
import os
from urllib.parse import quote_plus
from typing import Any,cast
BASE_DIR = Path(__file__).parent
# --------------------------
# 1.读取运行环境 APP_ENV，控制多环境yaml
# --------------------------
APP_ENV = os.getenv("APP_ENV", "dev").lower()
VALID_ENVS = {"dev", "qa", "prod"}
if APP_ENV not in VALID_ENVS:
    raise ValueError(f"APP_ENV 必须为 {VALID_ENVS}, 当前:{APP_ENV}")

# 找到对应环境
omegaConfig = OmegaConf.load(BASE_DIR/f"config_{APP_ENV}.yaml")
yaml_dict: dict[str,Any] = OmegaConf.to_container(omegaConfig, resolve=True)

def flatten_yaml(d: dict, prefix: str = "") -> dict[str, Any]:
    """嵌套字典扁平化: db_milvus:{host:xx} → db_milvus_host"""
    out = {}
    for k, v in d.items():
        key = f"{prefix}_{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten_yaml(v, key))
        else:
            out[key] = v
    return out

flattened_yaml = flatten_yaml(yaml_dict)


class Settings(BaseSettings):
    # ========== db_milvus 配置（来自yaml扁平化 db_milvus.xxx → db_milvus_xxx） ==========
    db_milvus_host: str
    db_milvus_port: int
    db_milvus_database: str
    db_milvus_collection_prex: str
    db_milvus_connect_timeout: int
    db_milvus_enable: bool
    db_milvus_user: str = Field(
        default="",
        validation_alias="MILVUS_USER",
    )
    db_milvus_password: str = Field(
        default="",
        validation_alias="MILVUS_PASSWORD",
    )
    db_milvus_token: str = Field(
        default="",
        validation_alias="MILVUS_TOKEN",
    )

    # mysql 配置
    db_mysql_host: str
    db_mysql_port: int
    db_mysql_db_name: str
    db_mysql_user: str
    db_mysql_password: str

    # ========== app配置 ==========
    app_name: str
    secret_key: str
    debug: bool

    # dotenv 配置，指定.env路径
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env" if APP_ENV == "dev" else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )
   # ** pydantic‑settings的钩子函数 **
    @classmethod
    def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
    ):
        """
        优先级从高到低：
        init > 系统环境变量 > dotenv(.env仅dev) > yaml配置 > file_secret
        """
        return (
            init_settings,# 1 最高：代码手动传入
            env_settings,# 2 系统环境变量
            dotenv_settings,# 3 .env文件
            cls._yaml_source(flattened_yaml), #yaml兜底默认
            file_secret_settings,
        )
    @staticmethod
    def _yaml_source(yaml_data: dict):
        print("=== >>> _yaml_source 被执行了")
        def inner() -> dict[str, Any]:
            print("=== >>> inner() 框架正在读取yaml配置源")
            return yaml_data

        return inner

    @property
    def milvus_config(self) -> dict[str,Any]:
        return {
            "host": self.db_milvus_host,
            "port": self.db_milvus_port,
            "db_name": self.db_milvus_database,
            "timeout": self.db_milvus_connect_timeout,
            "user": self.db_milvus_user,
            "password": self.db_milvus_password,
            "token": self.db_milvus_token,
            "url":self.database_url
        }

    @property
    def mysql_config(self) -> dict[str, Any]:
        user = quote_plus(self.db_mysql_user)
        password = quote_plus(self.db_mysql_password)
        host = self.db_mysql_host
        port = self.db_mysql_port
        db_name = self.db_mysql_db_name
        charset ='utf8mb4'

        url = (
            f"mysql+asyncmy://{user}:{password}"
            f"@{host}:{port}/{db_name}"
            f"?charset={charset}"
        )
        return {
            "host": host,
            "port": port,
            "db_name": db_name,
            "user": self.db_mysql_user,
            "password": self.db_mysql_password,
            "charset": charset,
            "url": url,
        }


settings = cast(Settings, Settings())