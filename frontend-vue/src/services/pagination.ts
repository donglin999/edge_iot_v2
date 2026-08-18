/** DRF limit/offset pagination compatibility helpers. */
export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export function isPaginated<T>(data: unknown): data is Paginated<T> {
  return (
    typeof data === 'object' &&
    data !== null &&
    !Array.isArray(data) &&
    Array.isArray((data as { results?: unknown }).results)
  );
}

export function unwrapList<T>(data: unknown): T[] {
  if (isPaginated<T>(data)) return data.results;
  if (Array.isArray(data)) return data as T[];
  return [];
}

export function withLimitOffset(url: string, limit: number, offset: number): string {
  const separator = url.includes('?') ? '&' : '?';
  return `${url}${separator}limit=${limit}&offset=${offset}`;
}

export const FULL_PAGE_LIMIT = 1000;
const MAX_REQUESTS = 200;

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
