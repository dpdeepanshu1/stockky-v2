// frontend/src/components/BottomSheet.tsx
//
// Groww-style bottom sheet (2026-09-03 UI upgrade). On mobile it slides up
// from the bottom with a drag handle and swipe-down-to-dismiss; on desktop
// (sm: breakpoint and up) it renders as a centered rounded dialog instead —
// bottom sheets are a mobile pattern, a centered modal is the right desktop
// equivalent, not a full-width sheet pinned to the bottom of a wide screen.
//
// This is a primitive — it owns the overlay, positioning, drag/dismiss
// gesture, and header row (title + close button). Callers own everything
// below the header.

import { useEffect, useRef, useState, type ReactNode } from "react";

export interface BottomSheetProps {
  isOpen: boolean;
  onClose: () => void;
  title?: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
  /** Tailwind max-width class for the desktop centered dialog. Default matches prior modal sizing. */
  desktopMaxWidth?: string;
  /** Optional footer, pinned below the scrollable body (e.g. a primary action bar). */
  footer?: ReactNode;
}

const DRAG_DISMISS_THRESHOLD_PX = 110;

export default function BottomSheet({
  isOpen,
  onClose,
  title,
  subtitle,
  children,
  desktopMaxWidth = "sm:max-w-4xl",
  footer,
}: BottomSheetProps) {
  const [dragY, setDragY] = useState(0);
  const [dragging, setDragging] = useState(false);
  const startYRef = useRef<number | null>(null);
  const sheetRef = useRef<HTMLDivElement | null>(null);

  // Reset drag state whenever the sheet opens fresh.
  useEffect(() => {
    if (isOpen) {
      setDragY(0);
      setDragging(false);
    }
  }, [isOpen]);

  // Lock body scroll while open (mobile sheets shouldn't scroll the page behind them).
  useEffect(() => {
    if (!isOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [isOpen]);

  if (!isOpen) return null;

  const onTouchStart = (e: React.TouchEvent) => {
    startYRef.current = e.touches[0].clientY;
    setDragging(true);
  };
  const onTouchMove = (e: React.TouchEvent) => {
    if (startYRef.current == null) return;
    const delta = e.touches[0].clientY - startYRef.current;
    if (delta > 0) setDragY(delta); // only allow dragging downward
  };
  const onTouchEnd = () => {
    setDragging(false);
    if (dragY > DRAG_DISMISS_THRESHOLD_PX) {
      onClose();
    } else {
      setDragY(0);
    }
    startYRef.current = null;
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/50 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label={typeof title === "string" ? title : undefined}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={sheetRef}
        className={[
          "bg-graphite w-full",
          desktopMaxWidth,
          "sm:w-full max-h-[92vh] sm:max-h-[85vh] overflow-hidden flex flex-col",
          "rounded-t-2xl sm:rounded-2xl shadow-panel",
          "border border-slate sm:border",
          dragging ? "" : "transition-transform duration-200 ease-out",
        ].join(" ")}
        style={{ transform: dragY ? `translateY(${dragY}px)` : undefined }}
      >
        {/* Drag handle — mobile only, this is the Groww "grab bar" affordance */}
        <div
          className="sm:hidden flex justify-center pt-2.5 pb-1 shrink-0 touch-none"
          onTouchStart={onTouchStart}
          onTouchMove={onTouchMove}
          onTouchEnd={onTouchEnd}
        >
          <div className="w-10 h-1.5 rounded-full bg-slate" />
        </div>

        {(title || subtitle) && (
          <div
            className="sticky top-0 z-10 flex items-start justify-between gap-4 px-5 py-3.5 sm:py-4 border-b border-slate bg-graphite/95 backdrop-blur shrink-0 touch-none"
            onTouchStart={onTouchStart}
            onTouchMove={onTouchMove}
            onTouchEnd={onTouchEnd}
          >
            <div className="min-w-0">
              {title && (
                <h2 className="text-base sm:text-lg font-display font-bold text-paper tracking-tight truncate">
                  {title}
                </h2>
              )}
              {subtitle && <p className="text-xs text-mist mt-0.5">{subtitle}</p>}
            </div>
            <button
              type="button"
              onClick={onClose}
              className="text-mist hover:text-paper text-lg leading-none px-2 py-1 rounded-lg hover:bg-ink transition shrink-0"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
        )}

        <div className="overflow-y-auto flex-1 overscroll-contain">{children}</div>

        {footer && (
          <div className="shrink-0 border-t border-slate px-5 py-3 bg-graphite">{footer}</div>
        )}
      </div>
    </div>
  );
}
