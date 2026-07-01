/**
 * Forge Approval Gates API client
 */

const BASE = "/api";

export interface ApprovalRequest {
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

export interface ApprovalDecisionRequest {
  comment?: string;
}

export interface ApprovalDecisionResponse {
  request_id: string;
  status: string;
  comment?: string;
  reviewed_at: string;
}

export interface DiffResponse {
  request_id: string;
  diff: string;
  changed_files: string[];
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`API ${res.status}: ${body}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export async function listPendingApprovals(
  sessionId: string
): Promise<ApprovalRequest[]> {
  return request<ApprovalRequest[]>(`/approval/pending/${sessionId}`);
}

export async function getApprovalRequest(
  requestId: string
): Promise<ApprovalRequest> {
  return request<ApprovalRequest>(`/approval/${requestId}`);
}

export async function getApprovalDiff(requestId: string): Promise<DiffResponse> {
  return request<DiffResponse>(`/approval/${requestId}/diff`);
}

export async function approveRequest(
  requestId: string,
  comment?: string
): Promise<ApprovalDecisionResponse> {
  return request<ApprovalDecisionResponse>(`/approval/${requestId}/approve`, {
    method: "POST",
    body: JSON.stringify({ comment }),
  });
}

export async function rejectRequest(
  requestId: string,
  comment?: string
): Promise<ApprovalDecisionResponse> {
  return request<ApprovalDecisionResponse>(`/approval/${requestId}/reject`, {
    method: "POST",
    body: JSON.stringify({ comment }),
  });
}
