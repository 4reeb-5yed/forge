"use client";

import { useState, useEffect } from "react";
import { getApprovalDiff, approveRequest, rejectRequest } from "@/lib/approval";

interface ApprovalRequest {
  id: string;
  session_id: string;
  task_id: string | null;
  type: string;
  status: string;
  diff_summary: string;
  changed_files: string[];
  requested_at: string;
  expires_at: string | null;
}

interface DiffResponse {
  request_id: string;
  diff: string;
  changed_files: string[];
}

interface ApprovalModalProps {
  request: ApprovalRequest;
  onClose: () => void;
  onApproved?: () => void;
  onRejected?: () => void;
}

export function ApprovalModal({
  request,
  onClose,
  onApproved,
  onRejected,
}: ApprovalModalProps) {
  const [diff, setDiff] = useState<DiffResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [comment, setComment] = useState("");

  useEffect(() => {
    loadDiff();
  }, [request.id]);

  async function loadDiff() {
    setLoading(true);
    try {
      const diffData = await getApprovalDiff(request.id);
      setDiff(diffData);
    } catch (error) {
      console.error("Failed to load diff:", error);
    } finally {
      setLoading(false);
    }
  }

  async function handleApprove() {
    setSubmitting(true);
    try {
      await approveRequest(request.id, comment || undefined);
      onApproved?.();
      onClose();
    } catch (error) {
      console.error("Failed to approve:", error);
    } finally {
      setSubmitting(false);
    }
  }

  async function handleReject() {
    setSubmitting(true);
    try {
      await rejectRequest(request.id, comment || undefined);
      onRejected?.();
      onClose();
    } catch (error) {
      console.error("Failed to reject:", error);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Modal */}
      <div className="relative w-full max-w-4xl max-h-[90vh] bg-forge-card border border-forge-border rounded-lg shadow-2xl flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-forge-border">
          <div>
            <h2 className="text-lg font-semibold text-forge-text">
              Review Changes - {request.type.replace("_", " ").toUpperCase()}
            </h2>
            <p className="text-sm text-forge-muted">
              Task: {request.task_id || "N/A"} • Requested:{" "}
              {new Date(request.requested_at).toLocaleString()}
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-2 hover:bg-forge-bg rounded-lg transition-colors"
          >
            <svg
              className="w-5 h-5 text-forge-muted"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-auto p-6">
          {/* Summary */}
          <div className="mb-4">
            <h3 className="text-sm font-medium text-forge-text mb-2">
              Change Summary
            </h3>
            <p className="text-sm text-forge-muted bg-forge-bg rounded-lg p-3">
              {request.diff_summary}
            </p>
          </div>

          {/* Changed Files */}
          <div className="mb-4">
            <h3 className="text-sm font-medium text-forge-text mb-2">
              Changed Files ({diff?.changed_files.length || request.changed_files.length})
            </h3>
            <div className="flex flex-wrap gap-2">
              {(diff?.changed_files || request.changed_files).map((file, idx) => (
                <span
                  key={idx}
                  className="px-2 py-1 text-xs bg-forge-bg text-forge-muted rounded border border-forge-border"
                >
                  {file}
                </span>
              ))}
            </div>
          </div>

          {/* Diff */}
          <div className="mb-4">
            <h3 className="text-sm font-medium text-forge-text mb-2">
              Diff
            </h3>
            {loading ? (
              <div className="flex items-center justify-center py-8">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-forge-accent"></div>
              </div>
            ) : diff ? (
              <pre className="bg-forge-bg rounded-lg p-4 overflow-auto max-h-96 text-xs font-mono text-forge-text border border-forge-border">
                {diff.diff || "No changes detected"}
              </pre>
            ) : (
              <p className="text-sm text-forge-muted">Failed to load diff</p>
            )}
          </div>

          {/* Comment */}
          <div className="mb-4">
            <h3 className="text-sm font-medium text-forge-text mb-2">
              Review Comment (optional)
            </h3>
            <textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="Add a comment about your decision..."
              className="w-full px-3 py-2 bg-forge-bg border border-forge-border rounded-lg text-forge-text placeholder-forge-muted resize-none focus:outline-none focus:ring-2 focus:ring-forge-accent"
              rows={3}
            />
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-forge-border">
          <button
            onClick={handleReject}
            disabled={submitting}
            className="px-4 py-2 bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white rounded-lg font-medium transition-colors"
          >
            {submitting ? "Rejecting..." : "Reject"}
          </button>
          <button
            onClick={handleApprove}
            disabled={submitting}
            className="px-4 py-2 bg-green-600 hover:bg-green-700 disabled:opacity-50 text-white rounded-lg font-medium transition-colors"
          >
            {submitting ? "Approving..." : "Approve"}
          </button>
        </div>
      </div>
    </div>
  );
}
