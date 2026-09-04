from sqlalchemy.ext.asyncio import AsyncSession,create_async_engine
from rag_modules.config.settings import settings
from sqlalchemy.orm import DeclarativeBase
from typing import Annotated
from fastapi import Depends
pg_engine =create_async_engine(
    settings.postgres_url,
    echo=settings.echo,
    hide_parameters=True,
    pool_size=settings.POSTGRES_DATABASE_POOL_SIZE,
    max_overflow=settings.POSTGRES_DATABASE_MAX_OVERFLOW,  # 流量高峰，最多额外扩容 20 个临时连接。
    pool_timeout=settings.POSTGRES_DATABASE_POOL_TIMEOUT,
    pool_recycle=settings.POSTGRESDATABASE_POOL_RECY
)
#Base 会收集所有继承它的表元信息，可以用来建表、迁移
Base = DeclarativeBase()
# 获取数据库会话
async def get_pg_engine() -> AsyncSession:
    # 从 engine 连接池取出一条连接，创建会话 session。
    # `expire_on_commit=False`：commit 提交之后，ORM 对象属性不会过期，
    # 接口可以直接读取返回 JSON；如果是 True，commit 之后访问对象属性会重新发 SQL 查询，接口容易报错。
    async with AsyncSession(pg_engine,expire_on_commit=False) as session:
        yield session #把 session 对象**交出给 FastAPI 接口函数使用**。
#Annotated[真实类型, 附加元数据] 等价db: AsyncSession = Depends(get_db)
pgDbSession = Annotated[AsyncSession,Depends(get_pg_engine)]
