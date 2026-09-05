import { useEffect, useRef, useState } from "react";
import { BookOpen, CircleCheck, Database, Files, Library, LoaderCircle, Plus, RefreshCw, Search, Trash2, X } from "lucide-react";
import { request } from "./api";
import Documents from "./components/Documents";
import Pagination from "./components/Pagination";
import StateMessage from "./components/StateMessage";

const statusMap = {
  ready: { label: "可用", tone: "success" }, indexing: { label: "索引中", tone: "warning" },
  draft: { label: "草稿", tone: "neutral" }, failed: { label: "失败", tone: "danger" },
};
const visibilityMap = { private: "私有", team: "团队", public: "公开" };
const initialForm = { name: "", description: "", permission: "only_me" };

function formatDate(value) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(new Date(value));
}

function CreateDataset({ onClose, onCreated }) {
  const dialog = useRef(null);
  const [form, setForm] = useState(initialForm);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { dialog.current.showModal(); }, []);

  async function create(event) {
    event.preventDefault();
    if (submitting) return;
    if (!form.name.trim()) { setError("请输入知识库名称"); return; }
    setSubmitting(true);
    setError("");
    try {
      const created = await request("/api/knowledge_base", {
        method: "POST", headers: { "Content-Type": "application/json" },
        // 创建接口禁止额外字段，索引参数不属于空知识库的创建契约。
        body: JSON.stringify({ ...form, name: form.name.trim(), description: form.description.trim() }),
      });
      onCreated(created);
    } catch (err) {
      setError(err.message);
      setSubmitting(false);
    }
  }

  return <dialog className="modal" ref={dialog} aria-labelledby="create-title"
    onCancel={event => { event.preventDefault(); if (!submitting) onClose(); }}>
    <form onSubmit={create}>
      <div className="modal-head">
        <div className="modal-title"><span className="title-icon"><Library className="icon" /></span><h2 id="create-title">新建知识库</h2></div>
        <button type="button" className="ghost-button icon-button" title="关闭" aria-label="关闭"
          disabled={submitting} onClick={onClose}><X className="icon" /></button>
      </div>
      {error && <div role="alert" className="notice danger">{error}</div>}
      <label>知识库名称<input autoFocus required maxLength={255} value={form.name}
        onChange={event => setForm({ ...form, name: event.target.value })} /></label>
      <label>描述<textarea rows="4" maxLength={2000} value={form.description}
        onChange={event => setForm({ ...form, description: event.target.value })} /></label>
      <label>权限<select value={form.permission}
        onChange={event => setForm({ ...form, permission: event.target.value })}>
        <option value="only_me">私有</option><option value="all_team_members">团队</option>
      </select></label>
      <div className="modal-actions">
        <button type="button" className="ghost-button" disabled={submitting} onClick={onClose}>取消</button>
        <button className="primary-button" disabled={submitting}>{submitting ? "创建中..." : "创建"}</button>
      </div>
    </form>
  </dialog>;
}

function App() {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState(null);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [visibility, setVisibility] = useState("all");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [statsError, setStatsError] = useState("");
  const [actionError, setActionError] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [selected, setSelected] = useState(null);
  const [revision, setRevision] = useState(0);
  const [deleting, setDeleting] = useState(null);

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    // 同时取消网络请求并保护状态写入；即使响应已到达，过期请求也不能覆盖新筛选。
    setLoading(true);
    setError("");
    // 搜索防抖 300ms；cleanup 同时取消尚未触发的定时器，减少无效查询。
    const timer = setTimeout(async () => {
      const filters = new URLSearchParams({ page, page_size: 10, status, visibility });
      if (query.trim()) filters.set("q", query.trim());
      try {
        const payload = await request(`/api/knowledge_base/list?${filters}`, { signal: controller.signal });
        if (!current) return;
        const lastPage = Math.max(1, Math.ceil(payload.total / 10));
        if (page > lastPage) { setPage(lastPage); return; }
        setItems(payload.items);
        setTotal(payload.total);
      } catch (err) {
        if (current) setError(err.message);
      } finally {
        if (current) setLoading(false);
      }
    }, query.trim() ? 300 : 0);
    return () => { current = false; clearTimeout(timer); controller.abort(); };
  }, [query, status, visibility, page, revision]);

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setStatsError("");
    // 全局统计不依赖搜索/分页，仅在初始化、手动刷新或数据变更后重新获取。
    request("/api/knowledge_base/stats", { signal: controller.signal })
      .then(payload => { if (current) setStats(payload); })
      .catch(err => { if (current) { setStats(null); setStatsError(err.message); } });
    return () => { current = false; controller.abort(); };
  }, [revision]);

  function refresh() { setRevision(value => value + 1); }

  async function deleteDataset(item) {
    if (deleting || !window.confirm(`删除知识库“${item.name}”？`)) return;
    setDeleting(item.id);
    setActionError("");
    try {
      await request(`/api/knowledge_base/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      refresh();
    } catch (err) { setActionError(err.message); }
    finally { setDeleting(null); }
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Database className="icon" /></span>
        <div><strong>Graph-RAG</strong><span>Knowledge Console</span></div>
      </div>
      <nav className="nav-list" aria-label="主导航">
        <span className="nav-item active"><Database className="icon" />知识库管理</span>
      </nav>
    </aside>
    <main className="main-panel">
      {selected ? <Documents key={selected.id} dataset={selected} onBack={() => setSelected(null)} onUploaded={refresh} /> : <>
        <header className="page-header"><div><p className="page-eyebrow">知识与文档</p><h1>知识库管理</h1></div>
          <button className="primary-button" onClick={() => setModalOpen(true)}><Plus className="icon" />新建知识库</button>
        </header>
        <section className="stats-grid" aria-label="知识库统计">
          <Stat label="知识库总数" value={stats?.total ?? "-"} icon={Library} />
          <Stat label="可用知识库" value={stats?.ready ?? "-"} tone="green" icon={CircleCheck} />
          <Stat label="索引中知识库" value={stats?.indexing ?? "-"} tone="amber" icon={LoaderCircle} />
          <Stat label="文档总数" value={stats?.documents ?? "-"} tone="blue" icon={Files} />
        </section>
        {statsError && <div className="notice danger" role="alert">统计加载失败: {statsError}</div>}
        <section className="toolbar">
          <div className="search-box"><Search className="icon" />
            <input value={query} maxLength={255} aria-label="搜索知识库" placeholder="搜索名称、描述或分类"
              onChange={event => { setQuery(event.target.value); setPage(1); }} />
          </div>
          <select value={status} aria-label="知识库状态" onChange={event => { setStatus(event.target.value); setPage(1); }}>
            <option value="all">全部状态</option>
            {Object.entries(statusMap).map(([value, meta]) => <option key={value} value={value}>{meta.label}</option>)}
          </select>
          <select value={visibility} aria-label="知识库权限" onChange={event => { setVisibility(event.target.value); setPage(1); }}>
            <option value="all">全部权限</option>
            {Object.entries(visibilityMap).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <button className="ghost-button icon-button" title="刷新" aria-label="刷新" onClick={refresh}><RefreshCw className="icon" /></button>
        </section>
        {(error || actionError) && <div className="notice danger" role="alert">{error || actionError}</div>}
        <section className="table-wrap" aria-label="知识库列表">
          <div className="table-header"><span>知识库列表</span><span>共 {total} 条记录</span></div>
          {loading ? <StateMessage kind="loading" title="加载中..." />
            : error ? <StateMessage kind="error" title="列表加载失败" />
            : items.length === 0 ? <StateMessage title="暂无知识库" />
            : <div className="table-scroll"><table>
              <thead><tr><th>名称</th><th>状态</th><th>文档/分段</th><th>向量模型</th><th>权限</th><th>更新时间</th><th>操作</th></tr></thead>
              <tbody>{items.map(item => <tr key={item.id}>
                <td><div className="name-cell"><span className="file-symbol" aria-hidden="true"><BookOpen className="icon" /></span>
                  <div className="name-copy"><button className="kb-name text-button" title={item.name} onClick={() => setSelected(item)}>{item.name}</button>
                  <div className="kb-desc" title={item.description}>{item.description || "未填写描述"}</div></div></div></td>
                <td><span className={`badge ${statusMap[item.status]?.tone || "neutral"}`}>{statusMap[item.status]?.label || item.status}</span></td>
                <td className="numeric-cell">{item.document_count} <span className="muted">/ {item.chunk_count}</span></td>
                <td><span className={item.embedding_model ? "model-name" : "muted"}>{item.embedding_model || "未配置"}</span></td><td>{visibilityMap[item.visibility]}</td>
                <td className="date-cell">{formatDate(item.updated_at)}</td>
                <td className="actions-cell"><button className="ghost-button icon-button delete-button" title={`删除 ${item.name}`}
                  aria-label={`删除 ${item.name}`} disabled={deleting !== null} onClick={() => deleteDataset(item)}><Trash2 className="icon" /></button></td>
              </tr>)}</tbody>
            </table></div>}
        </section>
        {!error && <Pagination page={page} pageSize={10} total={total} loading={loading} onChange={setPage} />}
      </>}
    </main>
    {modalOpen && <CreateDataset onClose={() => setModalOpen(false)} onCreated={created => {
      setModalOpen(false); setSelected(created); refresh();
    }} />}
  </div>;
}

function Stat({ label, value, tone = "gray", icon: Icon }) {
  return <div className={`stat-card ${tone}`}>
    <div className="stat-label"><span>{label}</span><Icon className="icon" aria-hidden="true" /></div>
    <strong>{typeof value === "number" ? value.toLocaleString("zh-CN") : value}</strong>
  </div>;
}

export default App;
