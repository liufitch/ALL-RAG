import { ChevronLeft, ChevronRight } from "lucide-react";

export default function Pagination({ page, pageSize, total, loading, onChange }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <nav className="pagination" aria-label="分页">
      <span>第 {page} / {pages} 页</span>
      <button className="ghost-button icon-button" aria-label="上一页" title="上一页"
        disabled={loading || page <= 1} onClick={() => onChange(page - 1)}>
        <ChevronLeft className="icon" />
      </button>
      <button className="ghost-button icon-button" aria-label="下一页" title="下一页"
        disabled={loading || page >= pages} onClick={() => onChange(page + 1)}>
        <ChevronRight className="icon" />
      </button>
    </nav>
  );
}
