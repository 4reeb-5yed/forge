"use client";

import { useState, useEffect } from "react";
import { Session, listSessions, createSession, deleteSession } from "@/lib/api";

interface SessionListProps {
  activeSessionId: string | null;
  onSelectSession: (session: Session) => void;
  onClose?: () => void;
}

export default function SessionList({ activeSessionId, onSelectSession, onClose }: SessionListProps) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  // Create form state
  const [repoUrl, setRepoUrl] = useState("");
  const [goal, setGoal] = useState("");
  const [buildMode, setBuildMode] = useState("new");

  const fetchSessions = async () => {
    try {
      const data = await listSessions();
      setSessions(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load sessions");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSessions();
    const interval = setInterval(fetchSessions, 5000);
    return () => clearInterval(interval);
  }, []);

  const handleCreate = async () => {
    if (!repoUrl.trim() || !goal.trim()) return;
    try {
      const session = await createSession({ repo_url: repoUrl, goal, build_mode: buildMode });
      setSessions((prev) => [session, ...prev]);
      onSelectSession(session);
      setShowCreate(false);
      setRepoUrl("");
      setGoal("");
      setBuildMode("new");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create session");
    }
  };

  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await deleteSession(id);
      setSessions((prev) => prev.filter((s) => s.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete session");
    }
  };

  const statusColor = (status: string) => {
    switch (status) {
      case "running": return "bg-forge-success";
      case "paused": return "bg-forge-warning";
      case "error": return "bg-forge-error";
      default: return "bg-forge-muted";
    }
  };

  return (
    <aside className="flex flex-col h-full bg-forge-bg border-r border-forge-border">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-forge-border">
        <h2 className="text-sm font-semibold text-forge-text">Sessions</h2>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowCreate(true)}
            className="text-xs px-2 py-1 rounded bg-forge-accent text-white hover:bg-blue-600 transition-colors"
            aria-label="New session"
          >
            + New
          </button>
          {onClose && (
            <button
              onClick={onClose}
              className="text-forge-muted hover:text-forge-text md:hidden"
              aria-label="Close sidebar"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* Create form */}
      {showCreate && (
        <div className="p-4 border-b border-forge-border space-y-3">
          <input
            type="text"
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="Repository URL"
            className="w-full text-xs rounded border border-forge-border bg-forge-card px-3 py-2 text-forge-text placeholder-forge-muted focus:outline-none focus:ring-1 focus:ring-forge-accent"
          />
          <input
            type="text"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder="Goal (e.g., Add JWT auth)"
            className="w-full text-xs rounded border border-forge-border bg-forge-card px-3 py-2 text-forge-text placeholder-forge-muted focus:outline-none focus:ring-1 focus:ring-forge-accent"
          />
          <select
            value={buildMode}
            onChange={(e) => setBuildMode(e.target.value)}
            className="w-full text-xs rounded border border-forge-border bg-forge-card px-3 py-2 text-forge-text focus:outline-none focus:ring-1 focus:ring-forge-accent"
          >
            <option value="new">New Build</option>
            <option value="extend">Extend</option>
            <option value="analyze">Analyze</option>
            <option value="document">Document</option>
          </select>
          <div className="flex gap-2">
            <button
              onClick={handleCreate}
              disabled={!repoUrl.trim() || !goal.trim()}
              className="flex-1 text-xs px-3 py-2 rounded bg-forge-accent text-white hover:bg-blue-600 disabled:opacity-50 transition-colors"
            >
              Create
            </button>
            <button
              onClick={() => setShowCreate(false)}
              className="text-xs px-3 py-2 rounded border border-forge-border text-forge-muted hover:text-forge-text transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="px-4 py-2 text-xs text-forge-error bg-red-500/10">
          {error}
        </div>
      )}

      {/* Session list */}
      <div className="flex-1 overflow-y-auto scrollbar-thin">
        {loading ? (
          <div className="p-4 text-xs text-forge-muted">Loading sessions...</div>
        ) : sessions.length === 0 ? (
          <div className="p-4 text-xs text-forge-muted">No sessions yet. Create one to start building.</div>
        ) : (
          <ul className="divide-y divide-forge-border">
            {sessions.map((session) => (
              <li key={session.id}>
                <button
                  onClick={() => onSelectSession(session)}
                  className={`w-full text-left px-4 py-3 hover:bg-forge-card transition-colors ${
                    activeSessionId === session.id ? "bg-forge-card border-l-2 border-forge-accent" : ""
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`w-2 h-2 rounded-full flex-shrink-0 ${statusColor(session.status)}`} />
                    <span className="text-xs font-medium text-forge-text truncate">
                      {session.goal}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-forge-muted truncate max-w-[140px]">
                      {session.repo_url.replace("https://github.com/", "")}
                    </span>
                    <button
                      onClick={(e) => handleDelete(session.id, e)}
                      className="text-forge-muted hover:text-forge-error text-xs opacity-0 group-hover:opacity-100 transition-opacity"
                      aria-label={`Delete session ${session.id}`}
                    >
                      ×
                    </button>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
