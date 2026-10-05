import { useEffect, useState } from "react";
import { Archive, ArrowLeft, Download, Edit3, Eye, FileText, MoreHorizontal, RefreshCw, RotateCcw, Settings2, SlidersHorizontal, ToggleLeft, ToggleRight, Trash2, Upload, X } from "lucide-react";
import { downloadDocument, request } from "../api";
import Pagination from "./Pagination";
import StateMessage from "./StateMessage";
import IndexingPanel from "./IndexingPanel";
import SegmentEditor from "./SegmentEditor";
import KnowledgeBaseSettings from "./KnowledgeBaseSettings";

const documentStatuses = {
  waiting: "待索引", downloading: "下载中", parsing: "解析中", splitting: "分段中",
  embedding: "向量生成中", indexing: "索引中", completed: "已完成",
  error: "失败", failed: "失败", queued: "排队中", retry_wait: "等待重试",
};

// 色彩只作辅助，始终保留具体状态文本，避免仅靠颜色传达处理结果。
const documentTones = {
  completed: "success", error: "danger", failed: "danger", waiting: "neutral",
  downloading: "warning", parsing: "warning", splitting: "warning",
  embedding: "warning", indexing: "warning", queued: "warning", retry_wait: "warning",
};

export default function Documents({ dataset, onBack, onUploaded }) {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [result, setResult] = useState(null);
  const [indexingOpen, setIndexingOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [actionBusy, setActionBusy] = useState(null);
  const [jobNotice, setJobNotice] = useState("");
  const path = `/api/knowledge_base/${encodeURIComponent(dataset.id)}/documents`;

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setLoading(true);
    // 加载错误与上传错误分开，列表恢复不能抹掉部分上传失败的提示。
    setLoadError("");
    request(`${path}?page=${page}&page_size=20`, { signal: controller.signal })
      .then(payload => {
        if (!current) return;
        const lastPage = Math.max(1, Math.ceil(payload.total / 20));
        if (page > lastPage) { setPage(lastPage); return; }
        setItems(payload.items);
        setTotal(payload.total);
      })
      .catch(err => { if (current) setLoadError(err.message); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; controller.abort(); };
  }, [path, page, revision]);

  async function uploadFiles(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length || uploading) return;
    setUploading(true);
    setError("");
    setResult(null);
    const body = new FormData();
    // 同一个 files 字段可重复出现；dataset ID 来自已创建的知识库，不能先传无归属文件。
    files.forEach(file => body.append("files", file));
    try {
      setResult(await request(`${path}/upload`, { method: "POST", body }));
      setPage(1);
    } catch (err) {
      setError(`${err.message}。请核对文档列表后重试，部分文件可能已保存。`);
    } finally {
      // 后端逐文件提交，后续文件遇到 503 时先前文件可能已保存，失败后也要刷新列表。
      setUploading(false);
      setRevision(value => value + 1);
      onUploaded();
    }
  }

  async function mutate(item, action, method = "POST") {
    if (actionBusy) return;
    setActionBusy(`${item.id}:${action}`);
    setError("");
    try {
      await request(`${path}/${encodeURIComponent(item.id)}/${action}`, { method });
      setRevision(value => value + 1);
    } catch (err) { setError(err.message); }
    finally { setActionBusy(null); }
  }

  async function rename(item) {
    const name = window.prompt("新的文件名", item.name);
    if (!name || name.trim() === item.name) return;
    setActionBusy(`${item.id}:rename`);
    try {
      await request(`${path}/${encodeURIComponent(item.id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) });
      setRevision(value => value + 1);
    } catch (err) { setError(err.message); }
    finally { setActionBusy(null); }
  }

  async function remove(item) {
    if (!window.confirm(`删除文档“${item.name}”？`)) return;
    await mutate(item, "", "DELETE");
  }

  async function download(item) {
    setActionBusy(`${item.id}:download`);
    try {
      const blob = await downloadDocument(`${path}/${encodeURIComponent(item.id)}/download`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = item.name; anchor.click(); URL.revokeObjectURL(url);
    } catch (err) { setError(err.message); }
    finally { setActionBusy(null); }
  }

  async function pollJob(item, jobId) {
    setJobNotice(`文档“${item.name}”正在重新索引`);
    for (let attempt = 0; attempt < 60; attempt += 1) {
      try {
        const job = await request(`${path}/${encodeURIComponent(item.id)}/jobs/${encodeURIComponent(jobId)}`);
        if (["completed", "partial_success", "failed", "cancelled"].includes(job.status)) {
          setJobNotice(job.status === "completed" ? `文档“${item.name}”重新索引完成` : `文档“${item.name}”重新索引${job.status === "failed" ? "失败" : "已结束"}`);
          setRevision(value => value + 1);
          return;
        }
      } catch (err) { setError(err.message); return; }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    setJobNotice(`文档“${item.name}”重新索引仍在后台运行`);
  }

  return (
    <>
      <header className="page-header">
        <div className="document-heading">
          <button className="ghost-button icon-button" title="返回知识库" aria-label="返回知识库"
            disabled={uploading} onClick={onBack}><ArrowLeft className="icon" /></button>
          <div><p className="page-eyebrow">知识库 / 文档</p><h1>{dataset.name}</h1></div>
        </div>
        <div className="header-actions">
          <button className="ghost-button icon-button" title="刷新文档" aria-label="刷新文档"
            disabled={uploading} onClick={() => { setError(""); setRevision(value => value + 1); }}>
            <RefreshCw className="icon" />
          </button>
          <label className={`primary-button upload-button ${uploading ? "disabled" : ""}`}>
            <Upload className="icon" />{uploading ? "上传中..." : "上传文件"}
            <input type="file" aria-label="上传文件" multiple disabled={uploading} onChange={uploadFiles} />
          </label>
        </div>
      </header>
      {error && <div className="notice danger" role="alert">{error}</div>}
      {jobNotice && <div className="notice" role="status">{jobNotice}</div>}
      {loadError && <div className="notice danger" role="alert">{loadError}</div>}
      {result && <div className="upload-results" aria-live="polite">
        <p>上传成功 {result.documents.length} 个文件</p>
        {result.documents.map(item => <p key={item.id}>{item.name}{item.duplicate ? "（已存在）" : "（待索引）"}</p>)}
        {result.rejected.map((item, i) => <p className="rejection" key={`${item.filename}-${i}`}>
          {item.filename}: {item.message}
        </p>)}
      </div>}
      <div className="document-toolbar"><button className="ghost-button" disabled={uploading} onClick={() => setSettingsOpen(true)}><SlidersHorizontal className="icon" />知识库设置</button><button className="ghost-button" disabled={!items.length || uploading} onClick={() => setIndexingOpen(true)}><Settings2 className="icon" />配置索引</button></div>
      {settingsOpen && <KnowledgeBaseSettings dataset={dataset} onClose={() => setSettingsOpen(false)} onSaved={onUploaded} />}
      {indexingOpen && <IndexingPanel dataset={dataset} documents={items} onClose={() => setIndexingOpen(false)} />}
      <section className="table-wrap" aria-label="文档列表">
        <div className="table-header"><span>文档列表</span><span>共 {total} 条记录</span></div>
        {loading ? <StateMessage kind="loading" title="加载中..." />
          : loadError ? <StateMessage kind="error" title="文档列表加载失败" />
          : items.length === 0 ? <StateMessage title="暂无文档" />
          : <div className="table-scroll"><table className="documents-table">
            <thead><tr><th>文件名</th><th>索引状态</th><th>启用/归档</th><th>操作</th></tr></thead>
            <tbody>{items.map(item => <tr key={item.id}>
              <td><div className="name-cell"><span className="file-symbol" aria-hidden="true"><FileText className="icon" /></span><span>{item.name}</span></div></td>
              <td><span className={`badge ${documentTones[item.status] || "neutral"}`}>{documentStatuses[item.status] || item.status}</span></td>
              <td><span className={`badge ${item.archived ? "neutral" : item.enabled === false ? "warning" : "success"}`}>{item.archived ? "已归档" : item.enabled === false ? "已禁用" : "已启用"}</span></td>
              <td><div className="document-actions">
                <button className="ghost-button icon-button" title={`下载 ${item.name}`} aria-label={`下载 ${item.name}`} disabled={actionBusy !== null} onClick={() => download(item)}><Download className="icon" /></button>
                <button className="ghost-button icon-button" title={`编辑 ${item.name}`} aria-label={`编辑 ${item.name}`} disabled={actionBusy !== null} onClick={() => setEditing(item)}><Edit3 className="icon" /></button>
                <button className="ghost-button icon-button" title={`重命名 ${item.name}`} aria-label={`重命名 ${item.name}`} disabled={actionBusy !== null} onClick={() => rename(item)}><MoreHorizontal className="icon" /></button>
                <button className="ghost-button icon-button" title={item.enabled === false ? `启用 ${item.name}` : `禁用 ${item.name}`} aria-label={item.enabled === false ? `启用 ${item.name}` : `禁用 ${item.name}`} disabled={actionBusy !== null} onClick={() => mutate(item, item.enabled === false ? "enable" : "disable")}>
                  {item.enabled === false ? <ToggleRight className="icon" /> : <ToggleLeft className="icon" />}
                </button>
                <button className="ghost-button icon-button" title={item.archived ? `恢复 ${item.name}` : `归档 ${item.name}`} aria-label={item.archived ? `恢复 ${item.name}` : `归档 ${item.name}`} disabled={actionBusy !== null} onClick={() => mutate(item, item.archived ? "restore" : "archive")}>
                  {item.archived ? <RotateCcw className="icon" /> : <Archive className="icon" />}
                </button>
                <button className="ghost-button icon-button" title={`重新索引 ${item.name}`} aria-label={`重新索引 ${item.name}`} disabled={actionBusy !== null} onClick={() => mutate(item, "reindex")}><RefreshCw className="icon" /></button>
                <button className="ghost-button icon-button delete-button" title={`删除 ${item.name}`} aria-label={`删除 ${item.name}`} disabled={actionBusy !== null} onClick={() => remove(item)}><Trash2 className="icon" /></button>
              </div></td>
            </tr>)}</tbody>
          </table></div>}
      </section>
      {!loadError && <Pagination page={page} pageSize={20} total={total} loading={loading || uploading} onChange={setPage} />}
      {editing && <SegmentEditor datasetId={dataset.id} document={editing} onClose={() => setEditing(null)} onSaved={result => { setEditing(null); setRevision(value => value + 1); pollJob(editing, result.job_id); }} />}
    </>
  );
}
