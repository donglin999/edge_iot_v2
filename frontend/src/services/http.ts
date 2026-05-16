/**
 * Low-level fetch helpers shared by the raw-`fetch` API modules
 * (`dataApi.ts`, `acquisitionApi.ts`).
 *
 * `fetchWithAbort` threads an optional `AbortSignal` into every request so
 * callers can cancel in-flight requests from a `useEffect` cleanup. This
 * prevents "setState on unmounted component" warnings and stale-response
 * races when a component re-renders faster than the network responds.
 */

/** A `fetch` call that forwards an optional `AbortSignal`. */
export function fetchWithAbort(
  input: string,
  signal?: AbortSignal,
  init?: RequestInit,
): Promise<Response> {
  return fetch(input, { ...init, signal });
}

/**
 * True when an error originates from an `AbortController.abort()` call.
 * Callers should swallow these instead of surfacing them as load errors.
 */
export function isAbortError(err: unknown): boolean {
  if (err instanceof DOMException) return err.name === 'AbortError';
  return err instanceof Error && err.name === 'AbortError';
}
