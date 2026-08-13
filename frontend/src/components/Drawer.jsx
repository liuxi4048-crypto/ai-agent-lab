import { useEffect, useRef } from "react";
import { X } from "lucide-react";

/**
 * 右側からスライドインする共通ドロワー。
 * 左のカードグリッドを視界に保つため、オーバーレイは薄い暗転のみ(クリックで閉じる)。
 */
export default function Drawer({ open, onClose, title, children, wide = false }) {
  const panelRef = useRef(null);

  // Escキーで閉じる(IME変換中のEscは変換キャンセルなので無視する)
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === "Escape" && !e.isComposing && e.keyCode !== 229) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 開いたらパネルへフォーカスを移す(キーボード操作でも内容を読み進められるように)
  useEffect(() => {
    if (open) panelRef.current?.focus({ preventScroll: true });
  }, [open]);

  return (
    <>
      <div
        onClick={onClose}
        className={`fixed inset-0 z-40 bg-black/30 transition-opacity duration-200 ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      {/* 閉じている間は invisible でフォーカス順から外す(ArtifactDrawerは常時マウント
          されるため、translate だけだと画面外のボタンにTabが入ってしまう) */}
      <aside
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        className={`fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-zinc-800 bg-zinc-950 shadow-2xl transition-transform duration-200 focus:outline-none ${
          wide ? "max-w-3xl" : "max-w-xl"
        } ${open ? "translate-x-0" : "invisible translate-x-full"}`}
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
