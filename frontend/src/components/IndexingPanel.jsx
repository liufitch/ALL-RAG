import { useEffect, useState } from "react";
import { Eye, X } from "lucide-react";
import { request } from "../api";
import StateMessage from "./StateMessage";

const fallbackOptions = {
  indexing_techniques: [], embedding_models: [], segmentation_modes: [],
  defaults: {
    indexing_technique: "high_quality", embedding_model: "", 
    general: { max_chunk_length: 1024, overlap: 100 },
    parent_child: { parent_mode: "paragraph", parent_max_chunk_length: 4096, child_max_chunk_length: 512, child_overlap: 50 },
  },
};

function makeConfig(options) {
  const defaults = options.defaults || fallbackOptions.defaults;
  return {
    indexing_technique: defaults.indexing_technique,
    embedding_model: defaults.embedding_model,
    segmentation: { mode: "general", max_chunk_length: defaults.general.max_chunk_length, overlap: defaults.general.overlap, separator: "\n" },
  };
}

function sourceLabel(metadata = {}) {
  return [metadata.page && `第 ${metadata.page} 页`, metadata.sheet && `Sheet ${metadata.sheet}`, metadata.row_start && `第 ${metadata.row_start} 行`].filter(Boolean).join(" · ") || "来源定位未提供";
}

function SegmentedButton({ active, disabled, onClick, children }) {
  return <button type="button" aria-pressed={active} disabled={disabled} onClick={onClick}>{children}</button>;
}

export default function IndexingPanel({ dataset, documents, onClose }) {
  const [options, setOptions] = useState(fallbackOptions);
  const [config, setConfig] = useState(makeConfig(fallbackOptions));
  const [loading, setLoading] = useState(true);
  const [previewing, setPreviewing] = useState(false);
  const [preview, setPreview] = useState(null);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let current = true;
    request("/api/indexing/options")
      .then(payload => { if (current) { setOptions(payload); setConfig(makeConfig(payload)); } })
      .catch(err => { if (current) setError(err.message); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, []);

  function update(next) {
    setConfig(next);
    setStale(true);
  }

  function selectTechnique(value) {
    let next = { ...config, indexing_technique: value };
    if (value === "economy" && config.segmentation.mode === "parent_child") {
      next.segmentation = { mode: "general", max_chunk_length: options.defaults.general.max_chunk_length, overlap: options.defaults.general.overlap, separator: "\n" };
    }
    update(next);
  }

  function selectSegmentation(mode) {
    const defaults = mode === "parent_child" ? options.defaults.parent_child : options.defaults.general;
    const segmentation = mode === "parent_child"
      ? { mode, parent_mode: defaults.parent_mode, parent_max_chunk_length: defaults.parent_max_chunk_length, child_max_chunk_length: defaults.child_max_chunk_length, child_overlap: defaults.child_overlap, separator: "\n" }
      : { mode, max_chunk_length: defaults.max_chunk_length, overlap: defaults.overlap, separator: "\n" };
    update({ ...config, segmentation });
  }

  async function previewChunks() {
    if (!documents.length) return;
    setPreviewing(true);
    setError("");
    try {
      const response = await request(`/api/knowledge_base/${encodeURIComponent(dataset.id)}/indexing/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_ids: documents.map(item => item.id), ...config }),
      });
      setPreview(response);
      setStale(false);
    } catch (err) {
      setError(err.message);
    } finally {
      setPreviewing(false);
    }
  }

  const parentChild = config.segmentation.mode === "parent_child";
  return <section className="indexing-panel" aria-label="索引配置">
    <div className="indexing-panel-head">
      <div><p className="page-eyebrow">文档 / 索引配置</p><h2>配置并预览索引</h2></div>
      <button className="ghost-button icon-button" title="关闭索引配置" aria-label="关闭索引配置" onClick={onClose}><X className="icon" /></button>
    </div>
    {error && <div className="notice danger" role="alert">{error}</div>}
    {loading ? <StateMessage kind="loading" title="加载索引选项..." /> : <div className="indexing-layout">
      <div className="indexing-form">
        <fieldset><legend>索引方式</legend><div className="segmented-control">
          {options.indexing_techniques.map(item => <SegmentedButton key={item.id} active={config.indexing_technique === item.id} disabled={item.id === "economy" && parentChild} onClick={() => selectTechnique(item.id)}>{item.id === "high_quality" ? "高质量" : "经济"}</SegmentedButton>)}
        </div></fieldset>
        {config.indexing_technique === "high_quality" && <label>Embedding 模型<select value={config.embedding_model} onChange={event => update({ ...config, embedding_model: event.target.value })}>{options.embedding_models.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}</select></label>}
        <fieldset><legend>分段方式</legend><select value={config.segmentation.mode} onChange={event => selectSegmentation(event.target.value)}>{options.segmentation_modes.map(mode => <option key={mode.id} value={mode.id} disabled={!mode.supported_indexing_techniques.includes(config.indexing_technique)}>{mode.id === "general" ? "普通分段" : "父子分段"}</option>)}</select></fieldset>
        {!parentChild ? <><label>最大块长度（字符）<input type="number" min="1" value={config.segmentation.max_chunk_length} onChange={event => update({ ...config, segmentation: { ...config.segmentation, max_chunk_length: Number(event.target.value) } })} /></label><label>重叠长度（字符）<input type="number" min="0" value={config.segmentation.overlap} onChange={event => update({ ...config, segmentation: { ...config.segmentation, overlap: Number(event.target.value) } })} /></label></> : <><label>父块长度（字符）<input type="number" min="1" value={config.segmentation.parent_max_chunk_length} onChange={event => update({ ...config, segmentation: { ...config.segmentation, parent_max_chunk_length: Number(event.target.value) } })} /></label><label>子块长度（字符）<input type="number" min="1" value={config.segmentation.child_max_chunk_length} onChange={event => update({ ...config, segmentation: { ...config.segmentation, child_max_chunk_length: Number(event.target.value) } })} /></label><label>子块重叠（字符）<input type="number" min="0" value={config.segmentation.child_overlap} onChange={event => update({ ...config, segmentation: { ...config.segmentation, child_overlap: Number(event.target.value) } })} /></label></>}
        <div className="indexing-actions"><button className="primary-button" disabled={previewing || !documents.length} onClick={previewChunks}><Eye className="icon" />{previewing ? "预览中..." : "预览块"}</button><button className="ghost-button" onClick={onClose}>返回文档</button></div>
      </div>
      <div className="preview-pane"><div className="preview-pane-head"><strong>分段预览</strong><span>{preview ? `${preview.total_chunks} 个块` : `${documents.length} 个文档`}</span></div>{stale && preview && <div className="notice warning">配置已变化，请重新预览</div>}{!preview ? <StateMessage title="点击“预览块”查看真实分段结果" /> : <><div className="preview-chunks">{preview.chunks.map(chunk => <article className="preview-chunk" key={chunk.id}><div><span className="muted">#{chunk.position + 1}</span><strong>{chunk.index_type === "parent" ? "父块" : chunk.index_type === "child" ? "子块" : "普通块"}</strong></div><p>{chunk.content}</p><small>{sourceLabel(chunk.source_metadata)}</small></article>)}</div>{preview.warnings?.map((warning, index) => <div className="notice warning" key={`${warning.code}-${index}`}>{warning.filename}: {warning.message}</div>)}</>}</div>
    </div>}
  </section>;
}
