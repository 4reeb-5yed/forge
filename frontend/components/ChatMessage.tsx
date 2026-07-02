"use client";

import { InvokeResponse } from "@/lib/api";

export interface Message {
  id: string;
  role: "user" | "system";
  content: string;
  timestamp: Date;
  response?: InvokeResponse;
  isLoading?: boolean;
}

interface ChatMessageProps {
  message: Message;
}

export default function ChatMessage({ message }: ChatMessageProps) {
  const isUser = message.role === "user";

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} mb-4`}>
      <div
        className={`max-w-[80%] rounded-lg px-4 py-3 ${
          isUser
            ? "bg-forge-accent text-white"
            : "bg-forge-card border border-forge-border"
        }`}
      >
        {/* Role label */}
        <div className={`text-xs mb-1 ${isUser ? "text-blue-200" : "text-forge-muted"}`}>
          {isUser ? "You" : "Forge"}
        </div>

        {/* Content */}
        <div className="text-sm whitespace-pre-wrap">{message.content}</div>

        {/* Loading indicator */}
        {message.isLoading && (
          <div className="mt-2 flex items-center gap-1">
            <div className="w-2 h-2 rounded-full bg-forge-accent animate-bounce" style={{ animationDelay: "0ms" }} />
            <div className="w-2 h-2 rounded-full bg-forge-accent animate-bounce" style={{ animationDelay: "150ms" }} />
            <div className="w-2 h-2 rounded-full bg-forge-accent animate-bounce" style={{ animationDelay: "300ms" }} />
          </div>
        )}

        {/* Response details */}
        {message.response && (
          <div className="mt-3 pt-3 border-t border-forge-border">
            {/* Status */}
            <div className="flex items-center gap-2 mb-2">
              <span className={`inline-block w-2 h-2 rounded-full ${
                message.response.status === "success" ? "bg-forge-success" :
                message.response.status === "error" ? "bg-forge-error" :
                "bg-forge-warning"
              }`} />
              <span className="text-xs text-forge-muted">
                {message.response.status}
              </span>
            </div>

            {/* Node path */}
            {message.response.node_path && message.response.node_path.length > 0 && (
              <div className="mb-2">
                <span className="text-xs text-forge-muted">Path: </span>
                <span className="text-xs font-mono text-forge-muted">
                  {message.response.node_path.join(" → ")}
                </span>
              </div>
            )}

            {/* Commits */}
            {message.response.commit_shas && message.response.commit_shas.length > 0 && (
              <div className="mb-2">
                <span className="text-xs text-forge-muted">Commits: </span>
                {message.response.commit_shas.map((sha) => (
                  <span key={sha} className="text-xs font-mono text-forge-success mr-1">
                    {sha.slice(0, 7)}
                  </span>
                ))}
              </div>
            )}

            {/* Errors */}
            {message.response.errors && message.response.errors.length > 0 && (
              <div className="mt-2">
                {message.response.errors.map((err, i) => (
                  <div key={i} className="text-xs text-forge-error font-mono">
                    {err.code && <span className="font-bold">[{err.code}] </span>}
                    {err.message || JSON.stringify(err)}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Timestamp */}
        <div className={`text-xs mt-2 ${isUser ? "text-blue-200" : "text-forge-muted"} opacity-60`}>
          {message.timestamp.toLocaleTimeString()}
        </div>
      </div>
    </div>
  );
}
