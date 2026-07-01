"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { useRouter } from "next/navigation";
import ChatInput from "@/components/ChatInput";
import ChatMessage, { Message } from "@/components/ChatMessage";
import SessionList from "@/components/SessionList";
import EventLog from "@/components/EventLog";
import StatusBar from "@/components/StatusBar";
import ErrorToast from "@/components/ErrorToast";
import ErrorPanel from "@/components/ErrorPanel";
import SetupBanner from "@/components/SetupBanner";
import { ApprovalBanner } from "@/components/ApprovalBanner";
import { useHealthPolling } from "@/lib/health";
import { addError } from "@/lib/error-store";
import {
  Session,
  SessionEvent,
  RuntimeStatus,
  invokeWorkflow,
  getSessionStatus,
  interruptSession,
  resumeSession,
  stopSession,
  connectEventStream,
  getConfig,
} from "@/lib/api";

export default function Home() {
  const router = useRouter();

  // Health polling
  const { health, isConnected } = useHealthPolling(30000);

  // Session state
  const [activeSession, setActiveSession] = useState<Session | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [eventLogOpen, setEventLogOpen] = useState(true);
  const [errorPanelOpen, setErrorPanelOpen] = useState(false);

  // Chat state
  const [messages, setMessages] = useState<Message[]>([]);
  const [isInvoking, setIsInvoking] = useState(false);
  const [streamingContent, setStreamingContent] = useState<Record<string, string>>({});

  // Event + status state
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [wsConnected, setWsConnected] = useState(false);
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus | null>(null);

  // Refs
  const wsRef = useRef<WebSocket | null>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const statusPollRef = useRef<NodeJS.Timeout | null>(null);

  // Redirect to /setup if not configured
  useEffect(() => {
    async function checkConfig() {
      try {
        const config = await getConfig();
        if (!config.configured) {
          router.push("/setup");
        }
      } catch {
        // API not available yet, don't redirect
      }
    }
    checkConfig();
  }, [router]);

  // Auto-scroll chat
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // WebSocket connection management
  useEffect(() => {
    if (!activeSession) {
      wsRef.current?.close();
      wsRef.current = null;
      setWsConnected(false);
      setEvents([]);
      return;
    }

    // Connect WebSocket
    const ws = connectEventStream(
      activeSession.id,
      (event) => {
        setEvents((prev) => [...prev, event]);
        
        // Route error events to the error store
        if (event.type?.startsWith("error.") && event.payload) {
          addError({
            code: (event.payload.code as string) || "UNKNOWN",
            message: (event.payload.message as string) || "An error occurred",
            category: (event.payload.category as string) || "runtime",
            recoverable: (event.payload.recoverable as boolean) ?? true,
            timestamp: event.timestamp,
            suggestion: event.payload.suggestion as string | undefined,
          });
        }
        
        // Handle token events for streaming display
        if (event.type === "token" && event.payload) {
          const token = event.payload.token as string;
          const timestamp = event.payload.timestamp as string;
          if (token) {
            setStreamingContent((prev) => ({
              ...prev,
              [timestamp || "default"]: (prev[timestamp || "default"] || "") + token,
            }));
          }
        }
        
        // Handle approval events
        if (event.type?.startsWith("approval.") && event.payload) {
          // These are handled by the ApprovalBanner component
        }
      },
      () => setWsConnected(false),
      () => setWsConnected(false)
    );

    ws.onopen = () => setWsConnected(true);
    wsRef.current = ws;

    return () => {
      ws.close();
      setWsConnected(false);
    };
  }, [activeSession?.id]);

  // Poll runtime status
  useEffect(() => {
    if (!activeSession) {
      setRuntimeStatus(null);
      return;
    }

    const poll = async () => {
      try {
        const status = await getSessionStatus(activeSession.id);
        setRuntimeStatus(status);
      } catch {
        // Status endpoint may not be available yet
      }
    };

    poll();
    statusPollRef.current = setInterval(poll, 3000);
    return () => {
      if (statusPollRef.current) clearInterval(statusPollRef.current);
    };
  }, [activeSession?.id]);

  // Send message
  const handleSend = useCallback(async (content: string) => {
    if (!activeSession) return;

    const userMsg: Message = {
      id: crypto.randomUUID(),
      role: "user",
      content,
      timestamp: new Date(),
    };

    const loadingMsg: Message = {
      id: crypto.randomUUID(),
      role: "system",
      content: "Processing your request...",
      timestamp: new Date(),
      isLoading: true,
    };

    setMessages((prev) => [...prev, userMsg, loadingMsg]);
    setIsInvoking(true);

    try {
      const response = await invokeWorkflow({
        message: content,
        session_id: activeSession.id,
      });

      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === loadingMsg.id
            ? {
                ...msg,
                content: response.status === "success"
                  ? "Build completed successfully."
                  : response.errors?.join("\n") || `Status: ${response.status}`,
                isLoading: false,
                response,
              }
            : msg
        )
      );
    } catch (err) {
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === loadingMsg.id
            ? {
                ...msg,
                content: err instanceof Error ? err.message : "An error occurred",
                isLoading: false,
              }
            : msg
        )
      );
    } finally {
      setIsInvoking(false);
    }
  }, [activeSession]);

  // Control handlers
  const handleInterrupt = async () => {
    if (!activeSession) return;
    try {
      await interruptSession(activeSession.id);
    } catch (err) {
      console.error("Interrupt failed:", err);
    }
  };

  const handleResume = async () => {
    if (!activeSession) return;
    try {
      await resumeSession(activeSession.id);
    } catch (err) {
      console.error("Resume failed:", err);
    }
  };

  const handleStop = async () => {
    if (!activeSession) return;
    try {
      await stopSession(activeSession.id);
    } catch (err) {
      console.error("Stop failed:", err);
    }
  };

  // Select session
  const handleSelectSession = (session: Session) => {
    setActiveSession(session);
    setMessages([]);
    setEvents([]);
    setSidebarOpen(false);
  };

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      {/* Setup Banner */}
      <SetupBanner health={health} />

      {/* Approval Banner */}
      {activeSession && (
        <ApprovalBanner
          sessionId={activeSession.id}
          onApprovalDecision={(approved) => {
            // Refresh events after approval decision
            setEvents([]);
          }}
        />
      )}

      {/* Error Toast notifications */}
      <ErrorToast />

      {/* Error Panel */}
      <ErrorPanel isOpen={errorPanelOpen} onClose={() => setErrorPanelOpen(false)} />

      <div className="flex flex-1 overflow-hidden">
      {/* Mobile sidebar overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-40 md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar */}
      <div
        className={`fixed inset-y-0 left-0 z-50 w-72 transform transition-transform duration-200 md:relative md:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <SessionList
          activeSessionId={activeSession?.id || null}
          onSelectSession={handleSelectSession}
          onClose={() => setSidebarOpen(false)}
        />
      </div>

      {/* Main content */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Status bar */}
        <StatusBar
          status={runtimeStatus}
          sessionId={activeSession?.id || null}
          onInterrupt={handleInterrupt}
          onResume={handleResume}
          onStop={handleStop}
          isLoading={isInvoking}
          health={health}
          isConnected={isConnected}
          onOpenErrorPanel={() => setErrorPanelOpen(true)}
        />

        {/* Mobile toolbar */}
        <div className="flex items-center gap-2 px-4 py-2 border-b border-forge-border md:hidden">
          <button
            onClick={() => setSidebarOpen(true)}
            className="text-forge-muted hover:text-forge-text"
            aria-label="Open sessions"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
          {activeSession && (
            <span className="text-xs text-forge-muted truncate">
              {activeSession.goal}
            </span>
          )}
          <button
            onClick={() => setEventLogOpen(!eventLogOpen)}
            className="ml-auto text-forge-muted hover:text-forge-text text-xs"
          >
            {eventLogOpen ? "Hide Events" : "Show Events"}
          </button>
        </div>

        {/* Content area */}
        <div className="flex-1 flex overflow-hidden">
          {/* Chat panel */}
          <div className="flex-1 flex flex-col min-w-0">
            {/* Messages */}
            <div className="flex-1 overflow-y-auto scrollbar-thin p-4">
              {!activeSession ? (
                <div className="flex flex-col items-center justify-center h-full text-center">
                  <svg className="w-16 h-16 text-forge-muted/30 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M13 10V3L4 14h7v7l9-11h-7z" />
                  </svg>
                  <h2 className="text-lg font-semibold text-forge-text mb-2">Welcome to Forge</h2>
                  <p className="text-sm text-forge-muted max-w-md">
                    Create or select a session from the sidebar to start an autonomous build.
                    Describe your goal in plain English and Forge will plan, build, verify, and commit.
                  </p>
                </div>
              ) : messages.length === 0 ? (
                <div className="flex flex-col items-center justify-center h-full text-center">
                  <div className="text-sm text-forge-muted mb-4">
                    Session: <span className="font-mono text-forge-text">{activeSession.id}</span>
                  </div>
                  <p className="text-sm text-forge-muted max-w-md">
                    Tell Forge what to build. It will clarify requirements, plan the architecture,
                    execute tasks, verify correctness, and commit the result.
                  </p>
                </div>
              ) : (
                <>
                  {messages.map((msg) => (
                    <ChatMessage key={msg.id} message={msg} />
                  ))}
                  <div ref={chatEndRef} />
                </>
              )}
            </div>

            {/* Input */}
            <ChatInput
              onSend={handleSend}
              disabled={!activeSession || isInvoking}
              placeholder={
                !activeSession
                  ? "Select a session first..."
                  : isInvoking
                  ? "Forge is working..."
                  : "Describe what you want to build..."
              }
            />
          </div>

          {/* Event log panel (collapsible on desktop, toggle on mobile) */}
          {eventLogOpen && activeSession && (
            <div className="hidden md:block w-80 lg:w-96">
              <EventLog events={events} isConnected={wsConnected} />
            </div>
          )}
        </div>

        {/* Mobile event log */}
        {eventLogOpen && activeSession && (
          <div className="md:hidden h-48 border-t border-forge-border">
            <EventLog events={events} isConnected={wsConnected} />
          </div>
        )}
      </div>

      {/* Desktop event log toggle */}
      {activeSession && (
        <button
          onClick={() => setEventLogOpen(!eventLogOpen)}
          className="hidden md:flex fixed bottom-4 right-4 z-30 items-center gap-1 px-3 py-2 rounded-lg bg-forge-card border border-forge-border text-xs text-forge-muted hover:text-forge-text transition-colors shadow-lg"
          aria-label={eventLogOpen ? "Hide event log" : "Show event log"}
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
          {eventLogOpen ? "Hide Log" : "Show Log"}
        </button>
      )}
      </div>
    </div>
  );
}