const API_BASE = import.meta.env.VITE_API_BASE || "";

export async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options);
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    const detail = problem.detail;
    // FastAPI 校验错误的 detail 是数组，不能直接交给 Error，否则只会显示 [object Object]。
    const message = Array.isArray(detail)
      ? detail.map(item => item.msg || item.message).filter(Boolean).join("; ")
      : typeof detail === "string" ? detail : problem.message;
    throw new Error(message || `请求失败 (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}
