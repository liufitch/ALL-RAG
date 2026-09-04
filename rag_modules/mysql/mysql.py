from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from rag_modules.config.settings import settings
from sqlalchemy.orm import declarative_base
from fastapi import Depends
from typing import Annotated
#创建异步引擎
engine = create_async_engine(
    settings.MYSQL_DATABASE_URI,
    echo=settings.debug,#debug=True 时打印完整 SQL 日志；生产关闭。
    hide_parameters=True,
    pool_size=settings.MYSQL_DATABASE_POOL_SIZE,
    max_overflow=settings.MYSQL_DATABASE_MAX_OVERFLOW, #流量高峰，最多额外扩容 20 个临时连接。
    pool_timeout=settings.MYSQL_DATABASE_POOL_TIMEOUT,
    pool_recycle=settings.MYSQL_DATABASE_POOL_RECY
)
#Base 会收集所有继承它的表元信息，可以用来建表、迁移
Base = declarative_base()
# 获取数据库会话
async def get_mysql_engine() ->AsyncSession:
    #从 engine 连接池取出一条连接，创建会话 session。
    #`expire_on_commit=False`：commit 提交之后，ORM 对象属性不会过期，
    # 接口可以直接读取返回 JSON；如果是 True，commit 之后访问对象属性会重新发 SQL 查询，接口容易报错。
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session  #把 session 对象**交出给 FastAPI 接口函数使用**。

#Annotated[真实类型, 附加元数据] 等价db: AsyncSession = Depends(get_db)
DbSession = Annotated[AsyncSession,Depends(get_mysql_engine)]
