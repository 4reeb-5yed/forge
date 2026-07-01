"use client";

import { HealthResponse } from "@/lib/api";

interface ConnectionIndicatorProps {
  health: HealthResponse | null;
  isConnected: boolean;
}

export default function ConnectionIndicator({ health, isConnected }: ConnectionIndicatorProps) {
  const getStatus = (): "healthy" | "degraded" | "unhealthy" => {
    if (!isConnected) return "unhealthy";
    if (!health) return "unhealthy";
    return health.status;
  };

  const status = getStatus();

  const colorClasses = {
    healthy: "bg-green-500 shadow-[0_0_6px_rgba(34,197,94,0.6)]",
    degraded: "bg-yellow-500 shadow-[0_0_6px_rgba(234,179,8,0.6)]",
    unhealthy: "bg-red-500 shadow-[0_0_6px_rgba(239,68,68,0.6)]",
  };

  const labels = {
    healthy: "Connected",
    degraded: "Degraded",
    unhealthy: "Disconnected",
  };

  return (
    <div className="flex items-center gap-2" title={labels[status]}>
      <span
        className={`inline-block w-2 h-2 rounded-full ${colorClasses[status]}`}
        aria-label={labels[status]}
      />
      <span className="text-xs text-forge-muted hidden sm:inline">{labels[status]}</span>
    </div>
  );
}
