# FastAPI 多数据库设计问答

本文整理了当前项目中关于主业务数据库切换、类型判断、依赖注入，以及 SQLAlchemy 基础结构的核心问题。

## 1. 如何决定使用 MySQL 还是 PostgreSQL

当前项目不是在业务代码里写死 MySQL 或 PostgreSQL，而是通过配置决定。

配置入口在：

- `/Users/fitch/Documents/workspace/graph-rag/Graph-RAG/rag_modules/config/settings.py`
- `/Users/fitch/Documents/workspace/graph-rag/Graph-RAG/rag_modules/config/config_dev.yaml`
- `/Users/fitch/Documents/workspace/graph-rag/Graph-RAG/rag_modules/config/config_qa.yaml`

核心配置项是：

```yaml
database:
  type: mysql
```

或者：

```yaml
database:
  type: postgresql
```

也就是说：

- `database.type = mysql` 时，主业务数据库走 MySQL
- `database.type = postgresql` 时，主业务数据库走 PostgreSQL

当前 `config_dev.yaml` 里实际配置的是：

```yaml
database:
  type: sqlite
```

所以当前开发环境默认使用的是 SQLite，而不是 MySQL 或 PostgreSQL。

## 2. 如何判断当前使用的是 MySQL 还是 PostgreSQL

判断逻辑在 `rag_modules/config/settings.py` 的 `DatabaseSettings.scheme` 属性中：

```python
return {
    "sqlite": "sqlite+aiosqlite",
    "postgresql": "postgresql+asyncpg",
    "mysql": "mysql+asyncmy",
    "oceanbase": "mysql+asyncmy",
    "seekdb": "postgresql+asyncpg",
}[self.type]
```

这里的判断依据不是自动探测数据库实例类型，而是读取配置里的 `self.type`。

映射关系如下：

- `mysql -> mysql+asyncmy`
- `postgresql -> postgresql+asyncpg`
- `oceanbase -> mysql+asyncmy`
- `seekdb -> postgresql+asyncpg`
- `sqlite -> sqlite+aiosqlite`

后续 `rag_modules/db/session.py` 会使用：

```python
settings.sqlalchemy_database_uri
```

来创建 SQLAlchemy 异步引擎，所以真正决定底层数据库类型的是配置值，而不是业务层判断。

一句话总结：

- 看 `database.type`
- `settings.py` 按类型映射驱动协议
- `session.py` 用生成后的 URI 建立连接

## 3. 为什么引入 `from __future__ import annotations`

这里引入：

```python
from __future__ import annotations
```

主要是为了让类型注解延迟求值。

它的实际好处有这些：

### 3.1 避免前向引用问题

如果函数返回值、字段类型、类之间互相引用，Python 默认会在定义时立刻解析类型。延迟求值后，注解会先按字符串处理，减少因为导入顺序或类尚未定义导致的报错。

### 3.2 让类型注解更自然

像这些写法会更顺手：

```python
list[KnowledgeBase]
tuple[list[KnowledgeBase], int]
dict[str, Any]
```

### 3.3 降低模块循环依赖的摩擦

当前项目结构已经拆成：

- `api`
- `services`
- `repositories`
- `db`
- `dto`

这种结构后续很容易出现类型上的交叉引用。延迟注解可以降低这类问题的概率。

一句话总结：

- 它让类型注解先不要在运行时立即求值
- 更适合中大型 FastAPI / SQLAlchemy 项目

## 4. 为什么要单独写 `class Base(DeclarativeBase)`

当前项目里新增了统一 ORM 基类：

```python
class Base(DeclarativeBase):
    pass
```

它不是多余的包装，而是 SQLAlchemy 2.x 的标准声明式入口。

### 4.1 让所有 ORM 模型有统一父类

后续所有 ORM 表模型都会继承这个 `Base`，例如：

```python
class KnowledgeBaseRecord(Base):
    __tablename__ = "knowledge_bases"
```

这样 SQLAlchemy 才知道这些模型属于同一套映射体系。

### 4.2 统一管理 metadata

所有继承 `Base` 的模型，都会注册到同一份 `metadata` 中。

后面如果要建表或做初始化，通常就靠：

```python
Base.metadata.create_all(...)
```

如果没有统一 `Base`，就缺少整个 ORM 模型体系的共同入口。

### 4.3 方便后续扩展公共约定

现在这个类是空的，但后续很适合挂这些统一能力：

- 命名规范
- 公共字段 mixin
- 类型映射
- 通用表配置
- 自定义公共方法

所以它是一个基座，不只是为了“看起来规范”。

### 4.4 这是 SQLAlchemy 2.x 推荐写法

旧写法常见是：

```python
Base = declarative_base()
```

新写法更推荐：

```python
class Base(DeclarativeBase):
    pass
```

因为它和 `Mapped[...]`、`mapped_column(...)`、类型检查工具的协作更好。

一句话总结：

- `Base` 是整个 ORM 声明体系的统一根节点
- 所有表模型都应该继承它
- 这样才能统一建模、建表、迁移和扩展

## 5. 当前项目的整体思路

当前这版 FastAPI 改造采用的是：

- 配置选择
- 抽象层隔离
- 工厂分发

这不是三个并列口号，而是一条完整链路：先由配置决定选谁，再由抽象层屏蔽差异，最后由工厂把请求分发到具体实现。

### 5.1 配置选择

这一层解决的是“系统到底要用哪个后端”的问题。

在当前项目里，配置入口在：

- `rag_modules/config/settings.py`
- `rag_modules/config/config_dev.yaml`
- `rag_modules/config/config_qa.yaml`

主业务数据库通过 `database.type` 选择：

- `mysql`
- `postgresql`
- `sqlite`
- `oceanbase`
- `seekdb`

向量库通过 `vector_store.provider` 选择：

- `milvus`
- `pgvector`
- `qdrant`
- `weaviate`
- `opensearch`
- `elasticsearch`

`settings.py` 的职责不是直接执行业务逻辑，而是把配置翻译成统一可消费的数据：

- 主库把 `type` 翻译成 `scheme`
- 再拼出 `uri`
- 再给出统一的 `engine_options`

例如：

- `mysql -> mysql+asyncmy`
- `postgresql -> postgresql+asyncpg`

这一层的价值是：

- 环境切换只改配置
- 不同部署环境可以选不同数据库
- 业务代码不需要自己判断“当前是不是 MySQL”

### 5.2 抽象层隔离

这一层解决的是“上层业务不要直接依赖具体数据库实现”的问题。

如果没有抽象层，代码里很容易到处出现这种逻辑：

```python
if database_type == "mysql":
    ...
elif database_type == "postgresql":
    ...
```

这样会带来几个问题：

- 路由层直接耦合底层数据库差异
- service 层难以复用
- 切换数据库时改动面很大
- 测试时很难替换实现

现在项目里的隔离方式是：

- `rag_modules/db/session.py` 统一提供 `engine`、`SessionLocal`、`get_db_session`
- `rag_modules/db/models.py` 统一提供 ORM 模型
- `rag_modules/repositories/knowledge_base_repository.py` 封装数据读写
- `rag_modules/services/knowledge_base_service.py` 只面向 repository 和向量库接口编排业务
- `rag_modules/api/knowledge_base_api.py` 只关心 FastAPI 路由和依赖注入

也就是说，每层只看自己该看的抽象：

- 路由层不关心底层是 MySQL 还是 PG
- service 层不关心连接串怎么拼
- repository 层不关心 HTTP 参数怎么传进来

主业务数据库这边，真正的隔离载体是 SQLAlchemy：

- ORM 模型统一
- `AsyncSession` 统一
- 查询接口统一

所以 repository 基本不用写数据库分支，只要底层方言兼容，同一套 ORM 代码就能复用。

向量库这边，隔离载体是统一 provider 接口：

- `provision_collection(...)`
- `drop_collection(...)`

`KnowledgeBaseService` 只知道“我要准备一个向量集合”，并不关心这个集合最终是在 Milvus、Qdrant 还是 PGVector 里创建。

### 5.3 工厂分发

这一层解决的是“配置已经选好了后端，那么运行时到底实例化哪个实现类”的问题。

主库这边的工厂分发相对隐式，主要体现在：

- `settings.database.type` 决定 `scheme`
- `settings.sqlalchemy_database_uri` 决定 `create_async_engine(...)` 最终连到哪个数据库

也就是说，主库没有单独写一个 `DatabaseFactory` 类，但实际上已经具备工厂分发效果：

- 选 MySQL，就构造 MySQL 的连接 URI
- 选 PostgreSQL，就构造 PostgreSQL 的连接 URI
- 选 SQLite，就构造 SQLite 的连接 URI

向量库这边的工厂分发更明确，入口在：

- `rag_modules/vector_stores/factory.py`

核心逻辑是：

```python
def get_vector_store(provider=None):
    selected = provider or settings.vector_store.provider
    if selected == "milvus":
        return MilvusVectorStore()
    return StubVectorStore(selected)
```

也就是说：

- 配置或调用方给出 provider
- 工厂统一判断 provider 类型
- 工厂返回对应的具体实现类实例

于是上层业务代码可以统一写成：

```python
provider = get_vector_store(vector_store.provider)
provider.provision_collection(...)
```

而不用写成：

```python
if provider == "milvus":
    ...
elif provider == "qdrant":
    ...
```

这就是工厂模式的核心价值：

- 选择逻辑集中在一个地方
- 具体实现可插拔
- 新增后端时，只需要注册新实现，不需要改上层业务流程

### 5.4 三层如何串起来

把这三层连起来看，完整链路是这样的：

1. 配置层声明当前要用哪种主库、哪种向量库
2. 抽象层把上层业务和底层实现隔开
3. 工厂层根据配置把请求分发到具体实现
4. 上层路由和 service 永远面向统一接口编程

以创建知识库为例：

1. `config_dev.yaml` 里声明 `database.type` 和 `vector_store.provider`
2. `settings.py` 把配置转成数据库 URI 和向量库配置对象
3. `db/session.py` 用统一方式创建数据库 session
4. `vector_stores/factory.py` 选择具体向量库 provider
5. `knowledge_base_service.py` 统一调用 repository 和 vector provider
6. 路由层只负责接收请求和返回结果

所以这套设计的目标不是“支持很多数据库”这么简单，而是：

- 把后端选择放到配置层
- 把差异控制在基础设施层
- 让业务逻辑尽量不感知底层变化

一句话总结：

- 配置选择：决定用谁
- 抽象层隔离：隐藏差异
- 工厂分发：实例化具体实现

分成两块理解：

### 5.5 主业务数据库

通过 `database.type` 选择：

- `mysql`
- `postgresql`
- `sqlite`
- `oceanbase`
- `seekdb`

然后由 `settings.py` 负责：

- 选择 SQLAlchemy 方言
- 拼接数据库 URI
- 统一输出引擎配置

再由 `rag_modules/db/session.py` 创建统一的异步引擎和 `AsyncSession`。

所以业务层不会到处写 `if mysql`、`if postgresql` 这类分支，而是统一走 SQLAlchemy。

### 5.6 向量库

通过 `vector_store.provider` 选择：

- `milvus`
- `pgvector`
- `qdrant`
- `weaviate`
- `opensearch`
- `elasticsearch`

当前已经有：

- `rag_modules/vector_stores/factory.py`
- `rag_modules/vector_stores/milvus.py`
- `rag_modules/vector_stores/stub.py`

其中：

- `milvus` 已有真实实现
- 其他 provider 先保留工厂扩展位，后续可以逐个补上

一句话总结：

- 主业务库靠 `database.type + SQLAlchemy`
- 向量库靠 `vector_store.provider + factory`

## 6. 推荐配置示例

### 6.1 MySQL

```yaml
database:
  type: mysql
  host: 127.0.0.1
  port: 3306
  database: graph_rag
  username: root
  password: your_password
  charset: utf8mb4
```

### 6.2 PostgreSQL

```yaml
database:
  type: postgresql
  host: 127.0.0.1
  port: 5432
  database: graph_rag
  username: postgres
  password: your_password
```

### 6.3 SQLite

```yaml
database:
  type: sqlite
  sqlite_path: ./data/graph_rag.db
```

## 7. `async_sessionmaker` 和 `create_async_engine` 的区别

两者不是同一层东西，一个是“造引擎”，一个是“造会话工厂”。

在当前项目里，位置在：

- `rag_modules/db/session.py`

代码结构是：

```python
engine = create_async_engine(...)
SessionLocal = async_sessionmaker(bind=engine, ...)
```

### 7.1 `create_async_engine`

作用：创建数据库引擎。

它更底层，主要负责这些事情：

- 使用哪个数据库驱动
- 连接哪个数据库地址
- 连接池大小是多少
- 超时、回收、日志等基础设施参数怎么配置

它的产物是：

```python
AsyncEngine
```

可以把它理解成整个数据库连接体系的底座。如果没有 `engine`，程序根本不知道该连哪一个数据库。

一句话说：

- `create_async_engine` 解决的是“怎么连接数据库”

### 7.2 `async_sessionmaker`

作用：基于已有的 `engine` 创建会话工厂。

它比 `engine` 更上层，主要负责：

- 统一创建 `AsyncSession`
- 给每个请求或每次数据库操作分配会话对象
- 控制 session 的默认行为，例如 `expire_on_commit=False`

它的产物不是一个具体 session，而是一个可以反复创建 session 的工厂。

一句话说：

- `async_sessionmaker` 解决的是“怎么批量创建数据库会话”

### 7.3 它们之间的关系

典型顺序是：

1. 先用 `create_async_engine()` 创建数据库引擎
2. 再把 `engine` 传给 `async_sessionmaker()`
3. 最后在每次请求里，从 session factory 拿一个 `AsyncSession`

也就是说：

- `engine` 管连接能力
- `sessionmaker` 管会话生产
- `session` 才是具体执行查询和事务的对象

### 7.4 在当前项目中的含义

当前项目里对应的是：

```python
engine = create_async_engine(
    settings.sqlalchemy_database_uri,
    **settings.sqlalchemy_engine_options,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)
```

这段代码的含义是：

- `engine` 负责根据配置连接 MySQL、PostgreSQL 或 SQLite
- `SessionLocal` 负责基于这个 `engine` 创建异步会话
- `get_db_session()` 再把具体 session 提供给 FastAPI 路由或 service 使用

### 7.5 一个直观类比

可以类比成：

- `create_async_engine` = 建数据库连接中心
- `async_sessionmaker` = 建会话生产工厂
- `AsyncSession` = 某一次请求实际拿到的一条工作通道

### 7.6 为什么 `SessionLocal` 后面要加 `()`

在这段代码里：

```python
async def get_db_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
```

`SessionLocal` 不是一个已经创建好的 `AsyncSession` 对象，而是一个“会话工厂”。

它来自：

```python
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
```

这里的 `async_sessionmaker(...)` 返回的是一个可调用对象，可以理解成“专门用来生产 session 的工厂”。

所以：

- `SessionLocal` 表示会话工厂
- `SessionLocal()` 表示调用工厂，创建一个具体的 `AsyncSession`

如果不加 `()`，拿到的只是工厂本身，不是实际 session，自然也不能直接执行数据库操作。

可以类比成：

- `list` 是类型或构造器
- `list()` 才是一个实际列表对象

这里也是同样的逻辑：

- `SessionLocal` 是 session 构造器
- `SessionLocal()` 才是本次请求真正要使用的 session 实例

再结合 `async with` 来看：

```python
async with SessionLocal() as session:
    yield session
```

它表达的是：

1. 调用 `SessionLocal()` 创建一个新的异步 session
2. 把这个 session 交给当前请求使用
3. 请求结束后自动关闭 session

这样每个请求都会拿到自己独立的数据库会话，不会和别的请求混用。

### 7.7 为什么 `SessionLocal()` 会创建一个全新的 `AsyncSession`

因为 `async_sessionmaker` 返回的本来就是一个“可调用的会话工厂”。

它内部保存了这次创建 session 的配置信息，比如：

- 绑定哪个 `engine`
- `expire_on_commit=False`
- `class_=AsyncSession`

所以当你写：

```python
SessionLocal()
```

其实是在执行这个工厂的 `__call__`，让它按保存好的配置，现场造出一个新的 `AsyncSession` 实例。

为什么必须是“新实例”：

- 每个请求都应该有自己独立的 session
- session 不是线程/协程安全的共享对象
- 一次请求里可能有事务、提交、回滚、关闭等生命周期管理

所以这里的模式就是：

- `SessionLocal` 保存“怎么造”
- `SessionLocal()` 执行“开始造”
- 返回一个全新的 `AsyncSession`

简单类比：

- `SessionLocal` 像“咖啡机”
- `SessionLocal()` 像“按下出杯按钮”
- `AsyncSession` 是真的那杯咖啡

### 7.8 一句话总结

- `create_async_engine` 负责“连库”
- `async_sessionmaker` 负责“产出 session”
- `SessionLocal` 是工厂
- `SessionLocal()` 才是具体 session 实例
- 两者通常总是配套使用

## 8. `Depends` 作用是啥

`Depends` 是 FastAPI 的“依赖注入”工具。

在当前项目里，它最常见的用途是：

- 自动拿到数据库 session
- 把 session 注入到路由函数里
- 不用你手动在每个接口里创建和关闭 session

例如：

```python
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
```

这表示：

- 这里需要一个 `AsyncSession`
- 这个 `AsyncSession` 不是手动传进来的
- 而是 FastAPI 在请求进来时调用 `get_db_session()` 后自动提供的

`Depends(get_db_session)` 的含义就是：

1. 请求进入接口时，先执行 `get_db_session()`
2. 拿到返回的 session
3. 把 session 交给当前接口使用
4. 请求结束后按依赖生命周期自动收尾

所以 `Depends` 的作用可以直接理解成：

- “这个参数由 FastAPI 帮我准备”
- “怎么准备，由 `get_db_session` 负责”

在数据库场景里，它的价值非常直接：

- 路由层不用管连接怎么建
- 不用管 session 怎么关
- 也不用每个接口重复写一遍样板代码

### 8.1 一句话总结

- `Depends` = 依赖注入
- `get_db_session` = 依赖提供者
- FastAPI 会自动把依赖结果塞给接口参数

## 9. `knowledge_base_api` 为啥还要单独写 `get_knowledge_base_service`

在当前代码里，`knowledge_base_api.py` 是这样组织的：

```python
def get_knowledge_base_service(db=Depends(get_db_session)):
    return KnowledgeBaseService(KnowledgeBaseRepository(db))
```

而路由里再使用：

```python
service: KnowledgeBaseService = Depends(get_knowledge_base_service)
```

这两层解决的是不同的问题。

### 9.1 路由里那层是“使用 service”

`Depends(get_knowledge_base_service)` 的意思是：

- 这个接口需要一个 `KnowledgeBaseService`
- 但我不手动 new
- 由 FastAPI 帮我去调用 `get_knowledge_base_service()` 生成

也就是说，接口函数只关心“拿来用”，不关心“怎么造”。

### 9.2 `get_knowledge_base_service` 是“组装 service 的地方”

`get_knowledge_base_service` 里面还要先拿数据库 session：

- `get_db_session()` 先提供 `db`
- `KnowledgeBaseRepository(db)` 再把 db 包成 repository
- `KnowledgeBaseService(...)` 最后把 repository 包成 service

所以它本质上是一个“对象组装工厂”。

### 9.3 为什么不直接写在路由里

你当然可以把这段写进路由函数里：

```python
async def list_knowledge_base(db=Depends(get_db_session)):
    service = KnowledgeBaseService(KnowledgeBaseRepository(db))
```

但这么做的问题是：

- 路由层会直接知道 repository 怎么创建
- service 构造方式一变，每个接口都可能要改
- 构造逻辑分散在多个接口里，不好维护

单独抽成 `get_knowledge_base_service` 的好处是：

- 构造逻辑集中
- 路由更干净
- 后面可以统一加日志、缓存、mock 或替换实现

### 9.4 一句话总结

- `Depends(get_knowledge_base_service)` 是“我要用 service”
- `get_knowledge_base_service` 是“怎么创建 service”
- 这一层是把构造逻辑从路由里移出去

## 10. 一句话总览

当前项目中：

- 用什么主业务数据库，看 `database.type`
- MySQL 还是 PostgreSQL，看 `settings.py` 里的驱动映射
- `from __future__ import annotations` 是为了让类型注解延迟求值
- `Base(DeclarativeBase)` 是为了给所有 ORM 模型提供统一基类和 metadata 入口
- `create_async_engine` 负责创建数据库引擎
- `async_sessionmaker` 负责批量创建 session
- `Depends` 负责把依赖自动注入到 FastAPI 接口参数里
- `get_knowledge_base_service` 负责把 session、repository 和 service 组装起来
