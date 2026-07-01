"use client";

import Link from "next/link";
import { HealthResponse } from "@/lib/api";

interface SetupBannerProps {
  health: HealthResponse | null;
}

export default function SetupBanner({ health }: SetupBannerProps) {
  // Don't show if no health data yet or if configured and healthy
  if (!health) return null;
  if (health.configured && health.status === "healthy") return null;

  const unhealthyComponents = Object.entries(health.components || {})
    .filter(([, comp]) => comp.status === "unhealthy" || comp.status === "degraded")
    .map(([name]) => name);

  return (
    <div className="w-full bg-yellow-500/10 border-b border-yellow-500/30 px-4 py-2">
      <div className="flex items-center justify-between gap-3 max-w-screen-xl mx-auto">
        <div className="flex items-center gap-2 min-w-0">
          <svg className="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z" />
          </svg>
          <span className="text-sm text-yellow-200">
            Forge is not fully configured.
            {unhealthyComponents.length > 0 && (
              <span className="text-xs text-yellow-300/80 ml-2">
                Issues: {unhealthyComponents.join(", ")}
              </span>
            )}
          </span>
        </div>
        <Link
          href="/setup"
          className="text-xs px-3 py-1 rounded bg-yellow-500/20 text-yellow-300 hover:bg-yellow-500/30 transition-colors whitespace-nowrap"
        >
          Go to Setup
        </Link>
      </div>
    </div>
  );
}
