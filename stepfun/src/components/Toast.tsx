import { useEffect } from "react";
import { Alert, Check, Info, X } from "./Icons";

export type ToastKind = "success" | "error" | "info";
export type ToastState = { id: number; kind: ToastKind; message: string; detail?: string } | null;

export function Toast({ toast, onClose }: { toast: ToastState; onClose: () => void }) {
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(onClose, toast.kind === "error" ? 8000 : 4200);
    return () => window.clearTimeout(timer);
  }, [toast, onClose]);
  if (!toast) return null;
  const Icon = toast.kind === "success" ? Check : toast.kind === "error" ? Alert : Info;
  return (
    <div className={`toast toast-${toast.kind}`} role="status">
      <span className="toast-icon">
        <Icon size={18} />
      </span>
      <div className="toast-copy">
        <strong>{toast.message}</strong>
        {toast.detail ? <span>{toast.detail}</span> : null}
      </div>
      <button className="icon-btn" onClick={onClose} aria-label="关闭提示">
        <X size={15} />
      </button>
    </div>
  );
}
