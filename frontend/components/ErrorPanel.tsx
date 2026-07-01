"use client";

import { useState } from "react";
import { ErrorEnvelope } from "@/lib/api";
import { useErrorStore } from "@/lib/error-store";

const CATEGORIES = ["all", "configuration", "runtime", "workflow", "connection"] as const;

interface ErrorPanelProps {
  isOpen: boolean;
  onClose: () => void;
}

export default function ErrorPanel({ isOpen, onClose }: ErrorPanelProps) {
  const errors = useErrorStore();
  const [activeCategory, setActiveCategory] = useState<string>("all");

  if (!isOpen) return null;

  const filtered =
    activeCategory === "all"
      ? errors
      : errors.filter((e) => e.category === activeCategory);

  return (
    <div className="fixed inset-0 z-[90] flex">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />

      {/* Panel */}
      <div className="relative ml-auto w-full max-w-lg h-full bg-forge-bg border-l border-forge-border flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-forge-border">
          <h2 className="text-sm font-semibold text-forge-text">Error Panel</h2>
          <button
            onClick={onClose}
            className="text-forge-muted hover:text-forge-text"
            aria-label="Close error panel"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Category filter */}
        <div className="flex gap-1 px-4 py-2 border-b border-forge-border overflow-x-auto">
          {CATEGORIES.map((cat) => (
            <button
              key={cat}
              onClick={() => setActiveCategory(cat)}
              className={`text-xs px-2.5 py-1 rounded-full whitespace-nowrap transition-colors ${
                activeCategory === cat
                  ? "bg-forge-accent text-white"
                  : "bg-forge-card text-forge-muted hover:text-forge-text"
              }`}
            >
              {cat.charAt(0).toUpperCase() + cat.slice(1)}
            </button>
          ))}
        </div>

        {/* Error list */}
        <div className="flex-1 overflow-y-auto scrollbar-thin p-4 space-y-2">
          {filtered.length === 0 ? (
            <div className="text-center text-forge-muted text-sm py-8">
              No errors to display
            </div>
          ) : (
            filtered.map((error, idx) => (
              <ErrorEntry key={`${error.timestamp}-${idx}`} error={error} />
            ))
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-forge-border text-xs text-forge-muted">
          {errors.length} total error{errors.length !== 1 ? "s" : ""} (max 200)
        </div>
      </div>
    </div>
  );
}

function ErrorEntry({ error }: { error: ErrorEnvelope }) {
  const categoryColor = {
    configuration: "bg-yellow-500/20 text-yellow-400",
    runtime: "bg-red-500/20 text-red-400",
    workflow: "bg-blue-500/20 text-blue-400",
    connection: "bg-orange-500/20 text-orange-400",
  }[error.category] || "bg-forge-card text-forge-muted";

  return (
    <div className="p-3 rounded-lg border border-forge-border bg-forge-card">
      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-xs font-mono px-1.5 py-0.5 rounded bg-forge-error/20 text-forge-error">
          {error.code}
        </span>
        <span className={`text-xs px-1.5 py-0.5 rounded ${categoryColor}`}>
          {error.category}
        </span>
        <span className="text-xs text-forge-muted ml-auto">
          {formatTimestamp(error.timestamp)}
        </span>
      </div>
      <p className="text-sm text-forge-text mb-1">{error.message}</p>
      {error.suggestion && (
        <p className="text-xs text-forge-muted">
          💡 {error.suggestion}
        </p>
      )}
    </div>
  );
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return ts;
  }
}
