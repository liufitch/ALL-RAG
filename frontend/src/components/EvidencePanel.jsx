import { useEffect, useRef, useState } from "react";
import { downloadDocument, streamEvidence } from "../api";

function locationLabel(metadata = {}) {
  return [metadata.page != null && `第 ${metadata.page} 页`,
    metadata.heading_path && String(metadata.heading_path),
    metadata.sheet && `工作表 ${metadata.sheet}`,
    metadata.row_start != null && `第 ${metadata.row_start}${metadata.row_end && metadata.row_end !== metadata.row_start ? `–${metadata.row_end}` : ""} 行`,
  ].filter(Boolean).join(" · ");
}

export default function EvidencePanel({ dataset }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState([]);
  const [activeReference, setActiveReference] = useState(null);
  const [searching, setSearching] = useState(false);
  const [completed, setCompleted] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const controller = useRef(null);

  useEffect(() => () => controller.current?.abort(), []);

  async function search(event) {
    event.preventDefault();
    if (!query.trim() || searching) return;
    const current = new AbortController();
    controller.current = current;
    setHits([]);
    setActiveReference(null);
    setCompleted(false);
    setError("");
    setNotice("");
    setSearching(true);
    try {
      await streamEvidence(`/api/knowledge_base/${encodeURIComponent(dataset.id)}/retrieve/stream`,
        { query: query.trim() }, (eventName, data) => {
          if (eventName === "hit") setHits(items => [...items, data]);
          if (eventName === "done") setCompleted(true);
        }, current.signal);
    } catch (err) {
      setHits([]);
      setActiveReference(null);
      if (err.name === "AbortError") setNotice("已停止检索，未完成的结果已清除。");
      else setError(err.message);
    } finally {
      if (controller.current === current) controller.current = null;
      setSearching(false);
    }
  }

  async function downloadSource(item) {
    try {
      const blob = await downloadDocument(`/api/knowledge_base/${encodeURIComponent(dataset.id)}/documents/${encodeURIComponent(item.document_id)}/download`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = item.document_name;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (err) { setError(err.message); }
  }

  return <section className="evidence-panel" aria-label="检索证据">
    <div className="evidence-panel-head"><div><p className="page-eyebrow">知识库 / 检索</p><h2>检索原文证据</h2></div>
      <span className="muted">直接展示命中的文档分段</span></div>
    <form onSubmit={search} className="evidence-form">
      <label>检索问题<input value={query} onChange={event => setQuery(event.target.value)} maxLength={8000}
        placeholder="输入要从知识库查找的问题" /></label>
      <button className="primary-button" disabled={searching || !query.trim()}>{searching ? "检索中..." : "检索"}</button>
      {searching && <button type="button" className="ghost-button" onClick={() => controller.current?.abort()}>停止检索</button>}
    </form>
    {error && <div className="notice danger" role="alert">{error}</div>}
    {notice && <div className="notice warning" role="status">{notice}</div>}
    {completed && !hits.length && <div className="notice" role="status">没有找到相关证据。</div>}
    {(searching || hits.length > 0) && <div role="region" aria-label="检索证据结果" aria-live="polite">
      {searching && <p className="muted">正在检索证据...</p>}
      {!!hits.length && <>
        <nav className="evidence-links" aria-label="命中来源">{hits.map(item =>
          <a key={item.reference} href={`#source-${dataset.id}-${item.reference}`}
            aria-label={`来源 ${item.reference}`} onClick={() => setActiveReference(item.reference)}>
            [{item.reference}] {item.document_name}
          </a>)}</nav>
        <div className="evidence-sources" aria-label="来源分段">{hits.map(item =>
          <article key={item.reference} id={`source-${dataset.id}-${item.reference}`}
            className={`evidence-source ${activeReference === item.reference ? "active" : ""}`}>
            <div className="evidence-source-head"><strong>[{item.reference}] {item.document_name}</strong>
              <button className="ghost-button" onClick={() => downloadSource(item)}>下载原文件</button></div>
            <small>{locationLabel(item.source_metadata) || "文档分段"} · 匹配分数 {Number(item.score).toFixed(2)}</small>
            <p>{item.content}</p>
          </article>)}</div>
      </>}
    </div>}
  </section>;
}
