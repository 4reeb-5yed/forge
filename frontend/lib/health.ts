/**
 * Health polling hook for monitoring Forge API status.
 */

"use client";

import { useState, useEffect, useRef } from "react";
import { HealthResponse, getHealth } from "./api";
import { addError } from "./error-store";

export interface UseHealthPollingResult {
  health: HealthResponse | null;
  isConnected: boolean;
}

export function useHealthPolling(interval = 30000): UseHealthPollingResult {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [isConnected, setIsConnected] = useState(true);
  const consecutiveFailures = useRef(0);

  useEffect(() => {
    let mounted = true;

    const poll = async () => {
      try {
        const result = await getHealth();
        if (!mounted) return;
        setHealth(result);
        setIsConnected(true);
        consecutiveFailures.current = 0;
      } catch {
        if (!mounted) return;
        consecutiveFailures.current++;

        if (consecutiveFailures.current >= 3) {
          setIsConnected(false);
          addError({
            code: "CONNECTION_ERROR",
            message: "Forge API is unreachable",
            category: "connection",
            recoverable: true,
            timestamp: new Date().toISOString(),
            suggestion: "Check that the Forge backend is running and accessible.",
          });
        }
      }
    };

    poll();
    const timer = setInterval(poll, interval);

    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, [interval]);

  return { health, isConnected };
}
