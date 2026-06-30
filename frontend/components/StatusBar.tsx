"use client";

import { RuntimeStatus } from "@/lib/api";

interface StatusBarProps {
  status: RuntimeStatus | null;
  sessionId: string | null;
  onInterrupt: () => void;
  onResume: () => void;
  onStop: () => void;
  isLoading?: boolean;
}

export default function StatusBar({
  status,
  sessionId,
  onInterrupt,
  onResume,
  onStop,
  isLoading,
}: StatusBarProps) {
  if (!sessionId) {
    return (
      <header className="flex items-center justify-between px-4 py-2 border-b border-forge-border bg-forge-card">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <svg className="w-5 h-5 text-forge-accent" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
            <span className="text-sm font-semibold text-forge-text">Forge</span>
          </div>
          <span className="text-xs text-forge-muted">Select a session to begin</span>
        </div>
      </header>
    );
  }

  const workerStatus = status?.worker_status?.status || "idle";
  const currentNode = status?.current_node || "—";

  const statusColor = () => {
    switch (workerStatus) {
      case "running": return "bg-forge-success";
      case "paused": return "bg-forge-warning";
      case "error": return "bg-forge-error";
      default: return "bg-forge-muted";
    }
  };

  return (
    <header className="flex items-center justify-between px-4 py-2 border-b border-forge-border bg-forge-card">
      {/* Left: Logo + Node info */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2">
          <svg className="w-5 h-5 text-forge-accent" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
          </svg>
          <span className="text-sm font-semibold text-forge-text">Forge</span>
        </div>

        <div className="hidden sm:flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${statusColor()}`} />
          <span className="text-xs text-forge-muted">Node:</span>
          <span className="text-xs font-mono text-forge-text">{currentNode}</span>
        </div>

        {status?.active_task && (
          <div className="hidden md:flex items-center gap-2">
            <span className="text-xs text-forge-muted">Task:</span>
            <span className="text-xs text-forge-text truncate max-w-[200px]">
              {status.active_task.title}
            </span>
          </div>
        )}

        {isLoading && (
          <div className="flex items-center gap-1">
            <div className="w-1.5 h-1.5 rounded-full bg-forge-accent animate-pulse" />
            <span className="text-xs text-forge-muted">Processing</span>
          </div>
        )}
      </div>

      {/* Right: Control buttons */}
      <div className="flex items-center gap-2">
        <button
          onClick={onInterrupt}
          className="text-xs px-3 py-1.5 rounded border border-forge-border text-forge-warning hover:bg-forge-warning/10 transition-colors"
          title="Pause build"
          aria-label="Pause build"
        >
          <span className="hidden sm:inline">Pause</span>
          <svg className="w-4 h-4 sm:hidden" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 9v6m4-6v6" />
          </svg>
        </button>
        <button
          onClick={onResume}
          className="text-xs px-3 py-1.5 rounded border border-forge-border text-forge-success hover:bg-forge-success/10 transition-colors"
          title="Resume build"
          aria-label="Resume build"
        >
          <span className="hidden sm:inline">Resume</span>
          <svg className="w-4 h-4 sm:hidden" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
          </svg>
        </button>
        <button
          onClick={onStop}
          className="text-xs px-3 py-1.5 rounded border border-forge-border text-forge-error hover:bg-forge-error/10 transition-colors"
          title="Stop build"
          aria-label="Stop build"
        >
          <span className="hidden sm:inline">Stop</span>
          <svg className="w-4 h-4 sm:hidden" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
          </svg>
        </button>
      </div>
    </header>
  );
}
