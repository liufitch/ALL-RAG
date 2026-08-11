import { useEffect, useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "";

const statusMap = {
  ready: { label: "可用", tone: "success" },
  indexing: { label: "索引中", tone: "warning" },
  draft: { label: "草稿", tone: "neutral" },
  failed: { label: "失败", tone: "danger" },
};

const visibilityMap = {
  private: "私有",
  team: "团队",
  public: "公开",
};

const initialForm = {
  name: "",
  description: "",
  category: "通用知识",
  owner: "当前用户",
  visibility: "private",
  embedding_model: "bge-large-zh",
  tags: "",
};

function Icon({ name }) {
  const paths = {
    search: "M10.5 18a7.5 7.5 0 1 1 5.3-12.8 7.5 7.5 0 0 1-5.3 12.8Zm5.6-1.9 4.4 4.4",
    plus: "M12 5v14M5 12h14",
    database:
      "M4 7c0-2.2 3.6-4 8-4s8 1.8 8 4-3.6 4-8 4-8-1.8-8-4Zm0 0v5c0 2.2 3.6 4 8 4s8-1.8 8-4V7M4 12v5c0 2.2 3.6 4 8 4s8-1.8 8-4v-5",
    file: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Zm0 0v5h5",
    trash: "M3 6h18M8 6V4h8v2m-9 0 1 15h8l1-15M10 10v7M14 10v7",
    refresh: "M20 12a8 8 0 0 1-14.9 4M4 12A8 8 0 0 1 18.9 8M19 4v4h-4M5 20v-4h4",
    close: "M6 6l12 12M18 6 6 18",
    shield: "M12 3 5 6v5c0 4.6 2.9 8.8 7 10 4.1-1.2 7-5.4 7-10V6l-7-3Z",
  };

  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="icon">
      <path d={paths[name]} />
    </svg>
  );
}

function formatDate(value) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function App() {
  const [items, setItems] = useState([]);
  const [stats, setStats] = useState(null);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [visibility, setVisibility] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [form, setForm] = useState(initialForm);

  const filters = useMemo(() => {
    const params = new URLSearchParams();
    if (query.trim()) params.set("q", query.trim());
    params.set("status", status);
    params.set("visibility", visibility);
    return params;
  }, [query, status, visibility]);

  async function fetchKnowledgeBases() {
    setLoading(true);
    setError("");
    try {
      const [listRes, statsRes] = await Promise.all([
        fetch(`${API_BASE}/api/knowledge-bases?${filters}`),
        fetch(`${API_BASE}/api/knowledge-bases/stats`),
      ]);
      if (!listRes.ok || !statsRes.ok) throw new Error("数据加载失败");
      const listPayload = await listRes.json();
      const statsPayload = await statsRes.json();
      setItems(listPayload.items);
      setStats(statsPayload);
    } catch (err) {
      setError(err.message || "数据加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchKnowledgeBases();
  }, [filters]);

  function updateForm(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function createKnowledgeBase(event) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const payload = {
        ...form,
        tags: form.tags
          .split(/[,，\s]+/)
          .map((tag) => tag.trim())
          .filter(Boolean),
      };
      const response = await fetch(`${API_BASE}/api/knowledge-bases`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const problem = await response.json().catch(() => ({}));
        throw new Error(problem.detail || "创建失败");
      }
      setForm(initialForm);
      setModalOpen(false);
      await fetchKnowledgeBases();
    } catch (err) {
      setError(err.message || "创建失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function deleteKnowledgeBase(id) {
    const response = await fetch(`${API_BASE}/api/knowledge-bases/${id}`, {
      method: "DELETE",
    });
    if (response.ok) {
      await fetchKnowledgeBases();
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <Icon name="database" />
          </span>
          <div>
            <strong>Graph-RAG</strong>
            <span>Knowledge Console</span>
          </div>
        </div>
        <nav className="nav-list" aria-label="主导航">
          <a className="nav-item active" href="#">
            <Icon name="database" /> 知识库管理
          </a>
          <a className="nav-item" href="#">
            <Icon name="file" /> 文档管理
          </a>
          <a className="nav-item" href="#">
            <Icon name="shield" /> 权限配置
          </a>
        </nav>
      </aside>

      <main className="main-panel">
        <header className="page-header">
          <div>
            <p className="eyebrow">Datasets Management</p>
            <h1>知识库管理</h1>
          </div>
          <button className="primary-button" onClick={() => setModalOpen(true)}>
            <Icon name="plus" />
            新建知识库
          </button>
        </header>

        <section className="stats-grid" aria-label="知识库统计">
          <Stat label="知识库总数" value={stats?.total ?? "-"} />
          <Stat label="可用知识库" value={stats?.ready ?? "-"} tone="green" />
          <Stat label="索引任务" value={stats?.indexing ?? "-"} tone="amber" />
          <Stat label="文档总数" value={stats?.documents ?? "-"} tone="blue" />
        </section>

        <section className="toolbar">
          <div className="search-box">
            <Icon name="search" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索名称、描述、分类或标签"
            />
          </div>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="all">全部状态</option>
            <option value="ready">可用</option>
            <option value="indexing">索引中</option>
            <option value="draft">草稿</option>
            <option value="failed">失败</option>
          </select>
          <select
            value={visibility}
            onChange={(event) => setVisibility(event.target.value)}
          >
            <option value="all">全部权限</option>
            <option value="private">私有</option>
            <option value="team">团队</option>
            <option value="public">公开</option>
          </select>
          <button className="ghost-button icon-button" onClick={fetchKnowledgeBases}>
            <Icon name="refresh" />
          </button>
        </section>

        {error && <div className="notice danger">{error}</div>}

        <section className="table-wrap">
          <div className="table-header">
            <span>知识库列表</span>
            <span>{items.length} 条记录</span>
          </div>
          {loading ? (
            <div className="empty-state">加载中...</div>
          ) : items.length === 0 ? (
            <div className="empty-state">暂无知识库</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>状态</th>
                  <th>文档/分片</th>
                  <th>向量模型</th>
                  <th>权限</th>
                  <th>更新时间</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <div className="kb-name">{item.name}</div>
                      <div className="kb-desc">{item.description || "未填写描述"}</div>
                      <div className="tag-row">
                        <span>{item.category}</span>
                        {item.tags.map((tag) => (
                          <span key={tag}>{tag}</span>
                        ))}
                      </div>
                    </td>
                    <td>
                      <Badge meta={statusMap[item.status]} />
                    </td>
                    <td>
                      <strong>{item.document_count}</strong>
                      <span className="muted"> / {item.chunk_count}</span>
                    </td>
                    <td>{item.embedding_model}</td>
                    <td>{visibilityMap[item.visibility]}</td>
                    <td>{formatDate(item.updated_at)}</td>
                    <td className="actions-cell">
                      <button
                        className="ghost-button icon-button"
                        onClick={() => deleteKnowledgeBase(item.id)}
                        aria-label={`删除 ${item.name}`}
                      >
                        <Icon name="trash" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </main>

      {modalOpen && (
        <div className="modal-backdrop" role="presentation">
          <form className="modal" onSubmit={createKnowledgeBase}>
            <div className="modal-head">
              <div>
                <p className="eyebrow">Create Dataset</p>
                <h2>新建知识库</h2>
              </div>
              <button
                type="button"
                className="ghost-button icon-button"
                onClick={() => setModalOpen(false)}
              >
                <Icon name="close" />
              </button>
            </div>
            <label>
              知识库名称
              <input
                required
                maxLength={80}
                value={form.name}
                onChange={(event) => updateForm("name", event.target.value)}
                placeholder="例如：合同审查知识库"
              />
            </label>
            <label>
              描述
              <textarea
                rows="4"
                maxLength={500}
                value={form.description}
                onChange={(event) => updateForm("description", event.target.value)}
                placeholder="填写适用场景、数据范围或维护说明"
              />
            </label>
            <div className="form-grid">
              <label>
                分类
                <input
                  value={form.category}
                  onChange={(event) => updateForm("category", event.target.value)}
                />
              </label>
              <label>
                负责人
                <input
                  value={form.owner}
                  onChange={(event) => updateForm("owner", event.target.value)}
                />
              </label>
              <label>
                权限
                <select
                  value={form.visibility}
                  onChange={(event) => updateForm("visibility", event.target.value)}
                >
                  <option value="private">私有</option>
                  <option value="team">团队</option>
                  <option value="public">公开</option>
                </select>
              </label>
              <label>
                向量模型
                <select
                  value={form.embedding_model}
                  onChange={(event) =>
                    updateForm("embedding_model", event.target.value)
                  }
                >
                  <option value="bge-large-zh">bge-large-zh</option>
                  <option value="bge-m3">bge-m3</option>
                  <option value="text-embedding-3-large">
                    text-embedding-3-large
                  </option>
                </select>
              </label>
            </div>
            <label>
              标签
              <input
                value={form.tags}
                onChange={(event) => updateForm("tags", event.target.value)}
                placeholder="多个标签用逗号或空格分隔"
              />
            </label>
            <div className="modal-actions">
              <button
                type="button"
                className="ghost-button"
                onClick={() => setModalOpen(false)}
              >
                取消
              </button>
              <button className="primary-button" disabled={submitting}>
                {submitting ? "创建中..." : "创建"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, tone = "gray" }) {
  return (
    <div className={`stat-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Badge({ meta }) {
  return <span className={`badge ${meta.tone}`}>{meta.label}</span>;
}

export default App;
