/**
 * Forge API client — fetch wrappers for all backend endpoints.
 * All calls go through the Next.js rewrite (/api/* → localhost:8000/*).
 */

const BASE = "/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Session {
  id: string;
  repo_url: string;
  goal: string;
  build_mode: string;
  status: string;
  created_at: string;
}

export interface CreateSessionPayload {
  repo_url: string;
  goal: string;
  build_mode: string;
}

export interface InvokePayload {
  message: string;
  session_id: string;
}

export interface InvokeResponse {
  status: string;
  commit_shas?: string[];
  errors?: Array<{ code?: string; message?: string; node?: string }>;
  node_path?: string[];
}

export interface RuntimeStatus {
  session_id: string;
  current_node: string;
  worker_status: {
    worker_id: string;
    status: string;
    current_task_id: string | null;
  };
  task_queue: { id: string; title: string; status: string }[];
  active_task: { id: string; title: string; status: string } | null;
  budget: Record<string, unknown>;
}

export interface DecisionExplanation {
  session_id: string;
  kind: string;
  subject: string;
  inputs: Record<string, unknown>;
  decision: string;
  rationale: string;
  alternatives: string[];
}

export interface SessionEvent {
  schema_version: string;
  seq: number;
  session_id: string;
  type: string;
  timestamp: string;
  source: string;
  payload: Record<string, unknown>;
  event_id: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Session endpoints
// ---------------------------------------------------------------------------

export async function listSessions(): Promise<Session[]> {
  return request<Session[]>("/sessions");
}

export async function createSession(payload: CreateSessionPayload): Promise<Session> {
  return request<Session>("/sessions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function getSession(id: string): Promise<Session> {
  return request<Session>(`/sessions/${id}`);
}

export async function deleteSession(id: string): Promise<void> {
  return request<void>(`/sessions/${id}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Workflow
// ---------------------------------------------------------------------------

export async function invokeWorkflow(payload: InvokePayload): Promise<InvokeResponse> {
  return request<InvokeResponse>("/workflow/invoke", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------------
// Runtime inspection
// ---------------------------------------------------------------------------

export async function getSessionStatus(id: string): Promise<RuntimeStatus> {
  return request<RuntimeStatus>(`/sessions/${id}/status`);
}

export async function getExplanation(id: string): Promise<DecisionExplanation> {
  return request<DecisionExplanation>(`/sessions/${id}/explain`);
}

// ---------------------------------------------------------------------------
// Control
// ---------------------------------------------------------------------------

export async function interruptSession(id: string): Promise<{ status: string }> {
  return request(`/sessions/${id}/interrupt`, { method: "POST" });
}

export async function resumeSession(id: string): Promise<{ status: string }> {
  return request(`/sessions/${id}/resume`, { method: "POST" });
}

export async function stopSession(id: string): Promise<{ status: string }> {
  return request(`/sessions/${id}/stop`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Config & Health Types
// ---------------------------------------------------------------------------

export interface ErrorEnvelope {
  code: string;
  message: string;
  category: string;
  recoverable: boolean;
  timestamp: string;
  suggestion?: string;
}

export interface ComponentHealth {
  status: "healthy" | "degraded" | "unhealthy";
  message?: string;
  latency_ms?: number;
}

export interface HealthResponse {
  status: "healthy" | "degraded" | "unhealthy";
  configured: boolean;
  components: Record<string, ComponentHealth>;
}

export interface ConfigResponse {
  configured: boolean;
  openrouter_api_key: string;
  github_token: string;
  selected_model: string;
  sandbox_mode: string;
}

export interface KeyTestResult {
  success: boolean;
  latency_ms: number;
  error?: string;
  details?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Config & Health Endpoints
// ---------------------------------------------------------------------------

export async function getConfig(): Promise<ConfigResponse> {
  return request<ConfigResponse>("/config");
}

export async function updateConfig(payload: Partial<ConfigResponse>): Promise<ConfigResponse> {
  return request<ConfigResponse>("/config", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function testConfigKey(component: string, key?: string): Promise<KeyTestResult> {
  return request<KeyTestResult>("/config/test", {
    method: "POST",
    body: JSON.stringify({ component, key }),
  });
}

export async function getConfigHealth(): Promise<Record<string, ComponentHealth>> {
  return request<Record<string, ComponentHealth>>("/config/health");
}

export async function getConfigModels(): Promise<{ models: Array<{ id: string; name: string }> }> {
  return request<{ models: Array<{ id: string; name: string }> }>("/config/models");
}

export async function getHealth(): Promise<HealthResponse> {
  // Health endpoint has no auth requirement
  const res = await fetch(`${BASE}/health`);
  if (!res.ok) throw new Error(`Health check failed: ${res.status}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------

export function connectEventStream(
  sessionId: string,
  onEvent: (event: SessionEvent) => void,
  onError?: (error: Event) => void,
  onClose?: () => void
): WebSocket {
  // WebSocket connects directly to the backend — Next.js rewrites only handle HTTP.
  // In production, this should be configured via an env var (NEXT_PUBLIC_WS_URL).
  const backendHost =
    typeof window !== "undefined" && process.env.NEXT_PUBLIC_WS_URL
      ? process.env.NEXT_PUBLIC_WS_URL
      : "ws://localhost:8000";
  const wsUrl = `${backendHost}/sessions/${sessionId}/events`;

  let lastSeq = 0;
  let retryCount = 0;
  const maxBackoff = 30000;

  function connect(): WebSocket {
    const url = lastSeq > 0 ? `${wsUrl}?since_seq=${lastSeq}` : wsUrl;
    const ws = new WebSocket(url);

    ws.onmessage = (msg) => {
      try {
        const event: SessionEvent = JSON.parse(msg.data);
        if (event.seq) lastSeq = event.seq;
        retryCount = 0;
        onEvent(event);
      } catch {
        console.error("Failed to parse event:", msg.data);
      }
    };

    ws.onerror = (err) => {
      console.error("WebSocket error:", err);
      onError?.(err);
    };

    ws.onclose = () => {
      onClose?.();
      // Exponential backoff reconnection
      const delay = Math.min(1000 * Math.pow(2, retryCount), maxBackoff);
      retryCount++;
      setTimeout(() => {
        const newWs = connect();
        // Update the reference for the caller
        Object.assign(ws, newWs);
      }, delay);
    };

    return ws;
  }

  return connect();
}
