import { useEffect, useRef, useState } from "react";
import { request } from "../api";

const terminalStatuses = new Set(["completed", "partial_success", "failed", "cancelled"]);
const statusLabels = {
  pending: "待投递", queued: "排队中", running: "处理中", retry_wait: "等待重试",
  completed: "已完成", partial_success: "部分完成", failed: "失败", cancelled: "已取消",
};
const stageLabels = {
  downloading: "读取文件", parsing: "解析文档", splitting: "生成分段",
  embedding: "生成向量", indexing: "写入索引", completed: "已完成",
};

export default function IndexingJobProgress({ datasetId, jobId, documents = [], onFinished }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [pollRevision, setPollRevision] = useState(0);
  const finishedJob = useRef(null);
  const onFinishedRef = useRef(onFinished);
  onFinishedRef.current = onFinished;
  const path = `/api/knowledge_base/${encodeURIComponent(datasetId)}/indexing/jobs/${encodeURIComponent(jobId)}`;

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let timer;
    setJob(null);
    setError("");

    async function poll() {
      try {
        const next = await request(path, { signal: controller.signal });
        if (!active) return;
        setJob(next);
        setError("");
        if (terminalStatuses.has(next.status)) {
          if (finishedJob.current !== jobId) {
            finishedJob.current = jobId;
            onFinishedRef.current?.();
          }
          return;
        }
      } catch (err) {
        if (!active || err.name === "AbortError") return;
        setError(err.message);
      }
      if (active) timer = setTimeout(poll, 2000);
    }

    poll();
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [jobId, path, pollRevision]);

  async function act(action) {
    setBusy(action);
    setError("");
    try {
      const result = await request(`${path}/${action}`, { method: "POST" });
      setJob(result.job);
      if (action === "retry") {
        finishedJob.current = null;
        setPollRevision(value => value + 1);
      }
    } catch (err) { setError(err.message); }
    finally { setBusy(""); }
  }

  const names = new Map(documents.map(item => [item.id, item.name]));
  const progress = Math.max(0, Math.min(100, Number(job?.progress || 0)));
  return <section className="job-progress-panel" role="region" aria-label="索引任务进度">
    <div className="job-progress-head"><div><p className="page-eyebrow">索引任务</p><h2>查看进度</h2></div>
      <span className={`badge ${job?.status === "failed" ? "danger" : job?.status === "completed" ? "success" : "warning"}`}>{statusLabels[job?.status] || job?.status || "加载中"}</span></div>
    <p className="muted">任务 ID：{jobId}</p>
    {error && <div className="notice danger" role="alert">进度读取失败：{error}，正在重试。</div>}
    {job && <>
      <div className="job-progress-summary"><strong>{progress}%</strong><span>{job.completed_documents} / {job.total_documents} 个文档完成 · {stageLabels[job.current_stage] || job.current_stage || statusLabels[job.status] || "等待处理"}</span></div>
      <progress max="100" value={progress} aria-label="索引总进度" />
      {(job.error || job.error_code) && <div className="notice danger" role="alert">{job.error || job.error_code}</div>}
      <div className="job-document-list">{job.documents?.map(item => <div className="job-document" key={item.id}>
        <span>{names.get(item.document_id) || item.document_id}</span>
        <span>{stageLabels[item.current_stage] || statusLabels[item.status] || item.status} · {item.progress}%</span>
        {(item.error || item.error_code) && <small>{item.error || item.error_code}</small>}
      </div>)}</div>
      <div className="job-progress-actions">
        {!terminalStatuses.has(job.status) && !job.cancel_requested && <button className="ghost-button" disabled={!!busy} onClick={() => act("cancel")}>取消索引</button>}
        {terminalStatuses.has(job.status) && job.documents?.some(item => ["failed", "retry_wait"].includes(item.status)) &&
          <button className="primary-button" disabled={!!busy} onClick={() => act("retry")}>重试失败文档</button>}
      </div>
    </>}
  </section>;
}
