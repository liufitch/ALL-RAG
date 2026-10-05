import { useEffect, useState } from "react";
import { Download, RefreshCw, Save, Upload, X } from "lucide-react";
import { request, downloadDocument } from "../api";

const defaults = { mode: "hybrid", top_k: 5, score_threshold: 0.3, semantic_weight: 0.7, keyword_weight: 0.3, rerank_enabled: false };

export default function KnowledgeBaseSettings({ dataset, onClose, onSaved }) {
  const [settings, setSettings] = useState(null);
  const [retrieval, setRetrieval] = useState(defaults);
  const [basic, setBasic] = useState({ name: dataset.name, description: dataset.description || "", permission: dataset.permission || "only_me" });
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => { request(`/api/knowledge_base/${encodeURIComponent(dataset.id)}/settings`).then(data => { setSettings(data); setBasic({ name: data.dataset.name, description: data.dataset.description, permission: data.dataset.permission }); setRetrieval(data.retrieval); }).catch(err => setError(err.message)); }, [dataset.id]);
  function updateRetrieval(key, value) { setRetrieval(current => ({ ...current, [key]: ["top_k"].includes(key) ? Number(value) : ["score_threshold", "semantic_weight", "keyword_weight"].includes(key) ? Number(value) : value })); }
  async function save() {
    if (Math.abs(Number(retrieval.semantic_weight) + Number(retrieval.keyword_weight) - 1) > 0.001) { setError("语义权重与关键词权重之和必须为 1"); return; }
    setBusy("save"); setError("");
    try { const result = await request(`/api/knowledge_base/${encodeURIComponent(dataset.id)}/settings`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...basic, retrieval }) }); setSettings(result); setNotice(result.needs_rebuild ? "设置已保存，索引配置已变化，请执行重建" : "设置已保存"); onSaved?.(); } catch (err) { setError(err.message); } finally { setBusy(""); }
  }
  async function rebuild() {
    if (!window.confirm("确认重建整个知识库？现有索引将在新索引完成后切换。")) return;
    setBusy("rebuild"); setError("");
    try { const result = await request(`/api/knowledge_base/${encodeURIComponent(dataset.id)}/rebuild`, { method: "POST" }); setNotice(`重建任务已${result.status === "queued" ? "进入队列" : "提交，等待补投"}`); } catch (err) { setError(err.message); } finally { setBusy(""); }
  }
  async function exportDataset() { setBusy("export"); setError(""); try { const blob = await downloadDocument(`/api/knowledge_base/${encodeURIComponent(dataset.id)}/export`); const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${dataset.name || "knowledge-base"}.zip`; anchor.click(); URL.revokeObjectURL(url); setNotice("完整知识库已导出"); } catch (err) { setError(err.message); } finally { setBusy(""); } }
  async function importDataset(event) { const file = event.target.files?.[0]; event.target.value = ""; if (!file) return; setBusy("import"); setError(""); const body = new FormData(); body.append("file", file); try { const result = await request("/api/knowledge_base/import?rebuild=true", { method: "POST", body }); setNotice(`已导入新知识库，包含 ${result.document_count} 个文档${result.job_id ? "，重建任务已提交" : ""}`); } catch (err) { setError(err.message); } finally { setBusy(""); } }
  return <section className="settings-panel" aria-label="知识库设置">
    <div className="settings-panel-head"><div><p className="page-eyebrow">知识库 / 设置</p><h2>知识库设置</h2></div><button className="ghost-button icon-button" title="关闭设置" aria-label="关闭设置" onClick={onClose}><X className="icon" /></button></div>
    {error && <div className="notice danger" role="alert">{error}</div>}{notice && <div className="notice success" role="status">{notice}</div>}
    {!settings ? <p className="muted">加载设置中...</p> : <div className="settings-grid">
      <fieldset><legend>基本设置</legend><label>名称<input value={basic.name} onChange={e => setBasic({ ...basic, name: e.target.value })} /></label><label>描述<textarea rows="3" value={basic.description} onChange={e => setBasic({ ...basic, description: e.target.value })} /></label><label>权限<select value={basic.permission} onChange={e => setBasic({ ...basic, permission: e.target.value })}><option value="only_me">仅自己</option><option value="all_team_members">团队成员</option><option value="all_members">所有成员</option></select></label></fieldset>
      <fieldset><legend>检索参数</legend><label>检索模式<select value={retrieval.mode} onChange={e => updateRetrieval("mode", e.target.value)}><option value="hybrid">混合检索</option><option value="vector">向量检索</option><option value="full_text">全文检索</option></select></label><label>Top K<input type="number" min="1" max="100" value={retrieval.top_k} onChange={e => updateRetrieval("top_k", e.target.value)} /></label><label>分数阈值<input type="number" min="0" max="1" step="0.05" value={retrieval.score_threshold} onChange={e => updateRetrieval("score_threshold", e.target.value)} /></label><label>语义权重<input type="number" min="0" max="1" step="0.05" value={retrieval.semantic_weight} onChange={e => updateRetrieval("semantic_weight", e.target.value)} /></label><label>关键词权重<input type="number" min="0" max="1" step="0.05" value={retrieval.keyword_weight} onChange={e => updateRetrieval("keyword_weight", e.target.value)} /></label><label className="checkbox-label"><input type="checkbox" checked={retrieval.rerank_enabled} onChange={e => updateRetrieval("rerank_enabled", e.target.checked)} />启用重排</label></fieldset>
      <fieldset><legend>索引与迁移</legend><p className="muted">索引配置变化后需要重建，旧索引会在新索引成功后切换。</p><div className="settings-actions"><button className="primary-button" disabled={!!busy} onClick={save}><Save className="icon" />{busy === "save" ? "保存中..." : "保存设置"}</button><button className="ghost-button" disabled={!!busy} onClick={rebuild}><RefreshCw className="icon" />{busy === "rebuild" ? "提交中..." : "重建知识库"}</button></div></fieldset>
      <fieldset><legend>导入导出</legend><p className="muted">导出包含设置、检索参数、文档元数据和原始文件；导入会创建新的知识库并自动重建。</p><div className="settings-actions"><button className="ghost-button" disabled={!!busy} onClick={exportDataset}><Download className="icon" />{busy === "export" ? "导出中..." : "导出完整知识库"}</button><label className={`ghost-button ${busy === "import" ? "disabled" : ""}`}><Upload className="icon" />{busy === "import" ? "导入中..." : "导入完整知识库"}<input type="file" accept=".zip,application/zip" hidden disabled={!!busy} onChange={importDataset} /></label></div></fieldset>
    </div>}
  </section>;
}
