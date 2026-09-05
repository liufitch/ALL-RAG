import { CircleAlert, FolderOpen, LoaderCircle } from "lucide-react";

// 只统一空态、加载和失败的视觉反馈，不改变各页面的数据与重试逻辑。
export default function StateMessage({ title, kind = "empty" }) {
  const Icon = kind === "loading" ? LoaderCircle : kind === "error" ? CircleAlert : FolderOpen;
  return <div className={`empty-state ${kind}`} role={kind === "loading" ? "status" : undefined}>
    <span className="state-icon" aria-hidden="true"><Icon /></span>
    <span>{title}</span>
  </div>;
}
