"use client";

import { useEffect, useRef } from "react";
import { SessionEvent } from "@/lib/api";

interface EventLogProps {
  events: SessionEvent[];
  isConnected: boolean;
}

export default function EventLog({ events, isConnected }: EventLogProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events]);

  const eventColor = (type: string) => {
    if (type.includes("error") || type.includes("fail")) return "text-forge-error";
    if (type.includes("success") || type.includes("complete")) return "text-forge-success";
    if (type.includes("warn") || type.includes("retry")) return "text-forge-warning";
    return "text-forge-muted";
  };

  const eventIcon = (type: string) => {
    if (type.includes("error") || type.includes("fail")) return "✗";
    if (type.includes("success") || type.includes("complete")) return "✓";
    if (type.includes("start") || type.includes("begin")) return "▶";
    if (type.includes("retry")) return "↻";
    return "•";
  };

  return (
    <div className="flex flex-col h-full border-l border-forge-border bg-forge-bg">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-forge-border">
        <h3 className="text-xs font-semibold text-forge-text">Event Log</h3>
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${isConnected ? "bg-forge-success animate-pulse" : "bg-forge-muted"}`} />
          <span className="text-xs text-forge-muted">
            {isConnected ? "Live" : "Disconnected"}
          </span>
        </div>
      </div>

      {/* Events */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto scrollbar-thin p-3 space-y-1">
        {events.length === 0 ? (
          <div className="text-xs text-forge-muted text-center py-8">
            No events yet. Events will appear here when a session is active.
          </div>
        ) : (
          events.map((event) => (
            <div
              key={event.event_id || `${event.seq}-${event.timestamp}`}
              className="flex items-start gap-2 text-xs font-mono py-1 hover:bg-forge-card rounded px-2 transition-colors"
            >
              <span className={`flex-shrink-0 ${eventColor(event.type)}`}>
                {eventIcon(event.type)}
              </span>
              <span className="text-forge-muted flex-shrink-0 w-16">
                {new Date(event.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </span>
              <span className={`flex-shrink-0 ${eventColor(event.type)}`}>
                [{event.type}]
              </span>
              <span className="text-forge-text truncate">
                {event.source}
                {event.payload && Object.keys(event.payload).length > 0 && (
                  <span className="text-forge-muted ml-1">
                    {JSON.stringify(event.payload).slice(0, 80)}
                    {JSON.stringify(event.payload).length > 80 ? "…" : ""}
                  </span>
                )}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
