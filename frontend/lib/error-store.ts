/**
 * Client-side error state management.
 * Stores up to 200 ErrorEnvelope entries with listener-based reactivity.
 */

import { ErrorEnvelope } from "./api";

type Listener = () => void;

const MAX_ERRORS = 200;
let errors: ErrorEnvelope[] = [];
let listeners: Listener[] = [];

function notify() {
  listeners.forEach((l) => l());
}

export function addError(error: ErrorEnvelope): void {
  errors = [error, ...errors].slice(0, MAX_ERRORS);
  notify();
}

export function getErrors(): ErrorEnvelope[] {
  return errors;
}

export function filterByCategory(category: string): ErrorEnvelope[] {
  return errors.filter((e) => e.category === category);
}

export function clearErrors(): void {
  errors = [];
  notify();
}

export function subscribe(listener: Listener): () => void {
  listeners.push(listener);
  return () => {
    listeners = listeners.filter((l) => l !== listener);
  };
}

/**
 * React hook to subscribe to the error store.
 */
import { useSyncExternalStore } from "react";

export function useErrorStore() {
  const snapshot = useSyncExternalStore(
    subscribe,
    () => errors,
    () => errors
  );
  return snapshot;
}
