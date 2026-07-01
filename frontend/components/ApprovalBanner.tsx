"use client";

import { useState, useEffect } from "react";
import { ApprovalRequest, listPendingApprovals } from "@/lib/approval";
import { ApprovalModal } from "./ApprovalModal";

interface ApprovalBannerProps {
  sessionId: string;
  onApprovalDecision?: (approved: boolean) => void;
}

export function ApprovalBanner({ sessionId, onApprovalDecision }: ApprovalBannerProps) {
  const [pendingRequests, setPendingRequests] = useState<ApprovalRequest[]>([]);
  const [selectedRequest, setSelectedRequest] = useState<ApprovalRequest | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadPendingApprovals();
    // Poll for new approval requests every 5 seconds
    const interval = setInterval(loadPendingApprovals, 5000);
    return () => clearInterval(interval);
  }, [sessionId]);

  async function loadPendingApprovals() {
    try {
      const requests = await listPendingApprovals(sessionId);
      setPendingRequests(requests);
    } catch (error) {
      console.error("Failed to load pending approvals:", error);
    } finally {
      setLoading(false);
    }
  }

  function handleApproved() {
    onApprovalDecision?.(true);
    loadPendingApprovals();
  }

  function handleRejected() {
    onApprovalDecision?.(false);
    loadPendingApprovals();
  }

  if (loading || pendingRequests.length === 0) {
    return null;
  }

  return (
    <>
      <div className="bg-yellow-900/30 border border-yellow-600/50 rounded-lg p-4 mb-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex-shrink-0">
              <svg
                className="w-6 h-6 text-yellow-500"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
                />
              </svg>
            </div>
            <div>
              <h3 className="text-sm font-medium text-yellow-200">
                Approval Required
              </h3>
              <p className="text-xs text-yellow-300/80">
                {pendingRequests.length} pending{" "}
                {pendingRequests.length === 1 ? "request" : "requests"}
              </p>
            </div>
          </div>
          <button
            onClick={() => setSelectedRequest(pendingRequests[0])}
            className="px-4 py-2 bg-yellow-600 hover:bg-yellow-500 text-white rounded-lg font-medium transition-colors"
          >
            Review
          </button>
        </div>
      </div>

      {selectedRequest && (
        <ApprovalModal
          request={selectedRequest}
          onClose={() => setSelectedRequest(null)}
          onApproved={handleApproved}
          onRejected={handleRejected}
        />
      )}
    </>
  );
}
