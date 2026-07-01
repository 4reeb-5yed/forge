"use client";

import { useState, useEffect, useCallback } from "react";
import { ErrorEnvelope } from "@/lib/api";
import { useErrorStore } from "@/lib/error-store";

interface ToastItem {
  id: string;
  error: ErrorEnvelope;
  dismissedAt?: number;
}

const MAX_VISIBLE = 5;
const AUTO_DISMISS_MS = 8000;

export default function ErrorToast() {
  const errors = useErrorStore();
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  // Track new errors appearing
  useEffect(() => {
    if (errors.length === 0) return;

    const latest = errors[0];
    // Only show toast for recoverable errors
    if (!latest.recoverable) return;

    const id = `${latest.timestamp}-${latest.code}-${Math.random().toString(36).slice(2, 8)}`;
    setToasts((prev) => {
      const next = [{ id, error: latest }, ...prev];
      return next.slice(0, MAX_VISIBLE + 5); // keep a small buffer
    });
  }, [errors.length]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-dismiss timer
  useEffect(() => {
    const timer = setInterval(() => {
      setToasts((prev) =>
        prev.filter((t) => {
          if (t.id === hoveredId) return true;
          if (t.dismissedAt) return false;
          return true;
        })
      );
    }, 1000);

    return () => clearInterval(timer);
  }, [hoveredId]);

  // Set auto-dismiss timers
  useEffect(() => {
    const timers: NodeJS.Timeout[] = [];
    toasts.forEach((t) => {
      if (!t.dismissedAt && t.id !== hoveredId) {
        const timer = setTimeout(() => {
          setToasts((prev) => prev.filter((toast) => toast.id !== t.id));
        }, AUTO_DISMISS_MS);
        timers.push(timer);
      }
    });
    return () => timers.forEach(clearTimeout);
  }, [toasts, hoveredId]);

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const visibleToasts = toasts.slice(0, MAX_VISIBLE);
  const overflow = toasts.length > MAX_VISIBLE ? toasts.length - MAX_VISIBLE : 0;

  if (visibleToasts.length === 0) return null;

  return (
    <div className="fixed top-4 right-4 z-[100] flex flex-col gap-2 max-w-sm w-full pointer-events-none">
      {visibleToasts.map((toast) => (
        <div
          key={toast.id}
          className="pointer-events-auto bg-forge-card border border-forge-border rounded-lg p-3 shadow-lg animate-in slide-in-from-right"
          onMouseEnter={() => setHoveredId(toast.id)}
          onMouseLeave={() => setHoveredId(null)}
        >
          <div className="flex items-start justify-between gap-2">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs font-mono px-1.5 py-0.5 rounded bg-forge-error/20 text-forge-error">
                  {toast.error.code}
                </span>
                <span className="text-xs text-forge-muted">{toast.error.category}</span>
              </div>
              <p className="text-sm text-forge-text truncate">{toast.error.message}</p>
              {toast.error.suggestion && (
                <p className="text-xs text-forge-muted mt-1">{toast.error.suggestion}</p>
              )}
            </div>
            <button
              onClick={() => dismiss(toast.id)}
              className="text-forge-muted hover:text-forge-text flex-shrink-0"
              aria-label="Dismiss"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      ))}
      {overflow > 0 && (
        <div className="pointer-events-auto text-xs text-forge-muted text-center py-1">
          +{overflow} more errors — view Error Panel
        </div>
      )}
    </div>
  );
}
