import { useEffect } from "react";
import { X } from "lucide-react";

/**
 * 右側からスライドインする共通ドロワー。
 * 左のカードグリッドを視界に保つため、オーバーレイは薄い暗転のみ(クリックで閉じる)。
 */
export default function Drawer({ open, onClose, title, children, wide = false }) {
  // Escキーで閉じる
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <>
      <div
        onClick={onClose}
        className={`fixed inset-0 z-40 bg-black/30 transition-opacity duration-200 ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      <aside
        className={`fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-zinc-800 bg-zinc-950 shadow-2xl transition-transform duration-200 ${
          wide ? "max-w-3xl" : "max-w-xl"
        } ${open ? "translate-x-0" : "translate-x-full"}`}
      >
        <div className="flex items-center gap-3 border-b border-zinc-800 px-4 py-3">
          <div className="min-w-0 flex-1">{title}</div>
          <button
            onClick={onClose}
            className="rounded-md p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
          >
            <X size={16} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </aside>
    </>
  );
}
