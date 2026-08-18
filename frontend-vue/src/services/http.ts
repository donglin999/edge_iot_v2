/** Low-level fetch helpers shared by the framework-neutral API modules. */
export function fetchWithAbort(
  input: string,
  signal?: AbortSignal,
  init?: RequestInit,
): Promise<Response> {
  return fetch(input, { ...init, signal });
}

export function isAbortError(error: unknown): boolean {
  if (error instanceof DOMException) return error.name === 'AbortError';
  return error instanceof Error && error.name === 'AbortError';
}
