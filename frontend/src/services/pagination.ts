/**
 * Helpers for DRF's global LimitOffsetPagination (XIU-9 / H10).
 *
 * Backend XIU-5 enabled `StandardLimitOffsetPagination` (default_limit=50,
 * max_limit=1000 — see backend commit 9709285), so every standard ViewSet
 * `list` endpoint now returns
 *
 *   { count, next, previous, results: [...] }
 *
 * instead of a bare `[...]` array, and is paged with `?limit=N&offset=M`
 * (NOT `?page=N`). Custom `@action` endpoints that build their own Response
 * (latest-values, sessions/active, data-points, overview, point-history,
 * device points, ...) are unaffected.
 *
 * `unwrapList` tolerates both shapes so call sites stay simple, and
 * `fetchAllPages` walks every page so existing list pages keep showing all
 * rows — client-side search / filter / table pagination work unchanged.
 */

/** DRF paginated list envelope. */
export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

/** True when `data` is a DRF paginated envelope (carries an array `results`). */
export function isPaginated<T>(data: unknown): data is Paginated<T> {
  return (
    typeof data === 'object' &&
    data !== null &&
    !Array.isArray(data) &&
    Array.isArray((data as { results?: unknown }).results)
  );
}

/**
 * Unwrap a single list response to a plain array, tolerating both the
 * paginated `{ results }` envelope and the legacy bare-array shape.
 */
export function unwrapList<T>(data: unknown): T[] {
  if (isPaginated<T>(data)) return data.results;
  if (Array.isArray(data)) return data as T[];
  return [];
}

/**
 * Append `limit`/`offset` query params to a URL, picking `?` or `&`
 * depending on whether the URL already has a query string.
 */
export function withLimitOffset(url: string, limit: number, offset: number): string {
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}limit=${limit}&offset=${offset}`;
}

/**
 * Page size used when walking a full list with `fetchAllPages`. Matches the
 * backend `max_limit` so a full list is fetched in the fewest round-trips.
 */
export const FULL_PAGE_LIMIT = 1000;

// Safety cap: stop after this many requests even if `count` keeps growing.
const MAX_REQUESTS = 200;

/**
 * Fetch every page of a paginated list endpoint and concatenate `results`.
 *
 * `fetchPage(limit, offset)` must resolve to the parsed JSON body for that
 * window. If the first response is not paginated (legacy bare array, or a
 * custom action), it is returned as-is with no further requests.
 */
export async function fetchAllPages<T>(
  fetchPage: (limit: number, offset: number) => Promise<unknown>,
): Promise<T[]> {
  const first = await fetchPage(FULL_PAGE_LIMIT, 0);
  if (!isPaginated<T>(first)) {
    return Array.isArray(first) ? (first as T[]) : [];
  }
  const all: T[] = [...first.results];
  let requests = 1;
  while (all.length < first.count && requests < MAX_REQUESTS) {
    const body = await fetchPage(FULL_PAGE_LIMIT, all.length);
    if (!isPaginated<T>(body) || body.results.length === 0) break;
    all.push(...body.results);
    requests += 1;
  }
  return all;
}
