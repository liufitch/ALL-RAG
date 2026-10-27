const API_BASE = import.meta.env.VITE_API_BASE || "";

export async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options);
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    const detail = problem.detail;
    // FastAPI 校验错误的 detail 是数组，不能直接交给 Error，否则只会显示 [object Object]。
    const message = problem.code && OCR_ERROR_MESSAGES[problem.code]
      ? OCR_ERROR_MESSAGES[problem.code]
      : Array.isArray(detail)
      ? detail.map(item => item.msg || item.message).filter(Boolean).join("; ")
      : typeof detail === "string" ? detail : problem.message;
    throw new Error(message || `请求失败 (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

const OCR_ERROR_MESSAGES = {
  OCR_NOT_ENABLED: "请先在服务端启用 OCR 后再解析扫描文档",
  OCR_DEPENDENCY_MISSING: "服务端未安装 OCR 解析依赖",
  OCR_INITIALIZATION_FAILED: "OCR 引擎初始化失败，请检查设备和模型配置",
  OCR_FAILED: "OCR 识别失败，请稍后重试",
  OCR_NO_EXTRACTABLE_TEXT: "图片中没有识别到可用文字",
  OCR_LINE_LIMIT_EXCEEDED: "文档识别内容超过系统限制",
  PDF_RENDER_FAILED: "扫描版 PDF 页面渲染失败",
  IMAGE_MALFORMED: "图片无法读取，请检查文件是否损坏",
  IMAGE_DIMENSION_LIMIT_EXCEEDED: "图片或 PDF 页面尺寸超过系统限制",
  PDF_NO_EXTRACTABLE_TEXT: "PDF 中没有可识别文字",
};

export async function downloadDocument(path) {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(problem.detail || `请求失败 (${response.status})`);
  }
  return response.blob();
}

export async function streamEvidence(path, payload, onEvent, signal) {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload), signal,
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(typeof problem.detail === "string" ? problem.detail : `请求失败 (${response.status})`);
  }
  if (!response.body) throw new Error("浏览器无法读取流式检索结果");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let match;
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const event = frame.split(/\r?\n/).find(line => line.startsWith("event:"))?.slice(6).trim();
        const data = frame.split(/\r?\n/).filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
        if (!event || !data) continue;
        let parsed;
        try { parsed = JSON.parse(data); } catch { throw new Error("检索结果流格式无效"); }
        if (event === "error") throw new Error(parsed.message || "检索失败");
        onEvent(event, parsed);
        if (event === "done") { finished = true; return; }
      }
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
  if (!finished) throw new Error("检索连接提前结束，请重试");
}
