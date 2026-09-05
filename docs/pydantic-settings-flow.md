# Pydantic Settings 加载链路

这份文档解释 `rag_modules/config/settings.py` 里，`.env`、`BaseSettings`、`SettingsConfigDict` 和 `settings.database.password` 是怎么连起来的。

## 代码入口

```python
class DatabaseSettings(BaseModel):
    username: str = ""
    password: str = ""

class Settings(BaseSettings):
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", PROJECT_DIR / ".env") if APP_ENV == "dev" else None,
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

settings = Settings()
```

## 一句话结论

`settings = Settings()` 这一句会触发 Pydantic / BaseSettings 读取环境变量，并把值自动填进 `settings.database.password` 这类字段里。

## 执行顺序

```text
1. class Settings(BaseSettings) 只是在定义结构
2. settings = Settings() 触发实例化
3. BaseSettings 读取 .env / 系统环境变量 / 其他配置源
4. env_nested_delimiter="__" 把键拆成层级
5. Pydantic 按字段名组装成嵌套对象
6. 得到 settings.database.password
```

## 变量如何映射

假设 `.env` 里写的是：

```env
DATABASE__USERNAME=admin
DATABASE__PASSWORD=secret
```

那么对应到 Python 对象就是：

```python
settings.database.username == "admin"
settings.database.password == "secret"
```

## 为什么是 `DATABASE__PASSWORD`

不是 `DatabaseSettings` 这个类名决定前缀，而是 `Settings` 里的字段名：

```python
database: DatabaseSettings
```

所以：

- `database` -> `DATABASE`
- `username` -> `USERNAME`
- `password` -> `PASSWORD`
- `__` -> 层级分隔符

## `SettingsConfigDict` 的作用

它不负责绑定 `database` 字段本身，只负责告诉 Pydantic：

- 开发环境读取哪个 `.env`
- 嵌套环境变量用什么分隔符
- 多余字段是否忽略

## 最终记忆法

```text
Settings 定结构
SettingsConfigDict 定读取规则
Settings() 触发实际读取和填充
```

## `__all__` 是什么

`__all__` 是模块对外公开的名字列表。

当别的代码写：

```python
from module import *
```

Python 只会导出 `__all__` 里列出来的对象。

例如：

```python
__all__ = [
    "Base",
    "DbSession",
    "DatasetRecord",
    "DocumentRecord",
    "DocumentSegmentRecord",
    "get_database_type",
    "SessionLocal",
    "engine",
    "get_db_session",
]
```

意思是这个模块希望外部主要使用这些名字，其他内部变量默认不作为公开接口。

补充：
- `from module import X` 不受 `__all__` 影响
- `from module import *` 才主要看 `__all__`
