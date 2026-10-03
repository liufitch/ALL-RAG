import { useEffect, useState } from "react";
import { Save, X } from "lucide-react";
import { request } from "../api";

export default function SegmentEditor({ datasetId, document, onClose, onSaved }) {
  const [segments, setSegments] = useState([]);
  const [version, setVersion] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [job, setJob] = useState(null);
  const path = `/api/knowledge_base/${encodeURIComponent(datasetId)}/documents/${encodeURIComponent(document.id)}`;

  useEffect(() => {
    let current = true;
    request(`${path}/segments`).then(payload => {
      if (!current) return;
      setSegments(payload.items || []);
      setVersion(payload.revision ?? null);
    }).catch(err => current && setError(err.message)).finally(() => current && setLoading(false));
    return () => { current = false; };
  }, [path]);

  function updateSegment(index, key, value) {
    setSegments(items => items.map((item, itemIndex) => itemIndex === index ? { ...item, [key]: value } : item));
  }

  async function save() {
    if (saving) return;
    if (segments.some(item => !item.content.trim())) {
      setError("分段内容不能为空");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const result = await request(`${path}/segments`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_version: version, segments }),
      });
      setJob(result);
      onSaved(result);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  return <div className="segment-editor-backdrop" role="presentation">
    <section className="segment-editor" role="dialog" aria-label={`编辑 ${document.name} 分段`}>
      <div className="modal-head"><div><p className="page-eyebrow">文档分段</p><h2>{document.name}</h2></div>
        <button className="ghost-button icon-button" aria-label="关闭" onClick={onClose} disabled={saving}><X className="icon" /></button>
      </div>
      {error && <div className="notice danger" role="alert">{error}</div>}
      {loading ? <p>加载分段中...</p> : <div className="segment-list">
        {segments.length === 0 ? <p className="muted">暂无可编辑分段</p> : segments.map((segment, index) => <article className="segment-card" key={segment.id}>
          <div className="segment-card-head"><span>分段 {index + 1}</span><span className="muted">{segment.index_type}</span></div>
          <label>内容<textarea rows="5" value={segment.content} onChange={event => updateSegment(index, "content", event.target.value)} /></label>
          <div className="segment-fields"><label>问题<input value={segment.question || ""} onChange={event => updateSegment(index, "question", event.target.value)} /></label>
            <label>答案<input value={segment.answer || ""} onChange={event => updateSegment(index, "answer", event.target.value)} /></label></div>
          <label>关键词<input value={(segment.keywords || []).join(", ")} onChange={event => updateSegment(index, "keywords", event.target.value.split(",").map(value => value.trim()).filter(Boolean))} /></label>
        </article>)}
      </div>}
      {job && <div className="notice" aria-live="polite">已保存，正在重新索引（任务 {job.job_id}）</div>}
      <div className="modal-actions"><button className="ghost-button" onClick={onClose} disabled={saving}>取消</button>
        <button className="primary-button" onClick={save} disabled={saving || loading || !segments.length}><Save className="icon" />{saving ? "保存中..." : "保存并重新索引"}</button></div>
    </section>
  </div>;
}
