import {
  FULL_PAGE_LIMIT,
  fetchAllPages,
  isPaginated,
  unwrapList,
  withLimitOffset,
  type Paginated,
} from './pagination';

function page<T>(count: number, results: T[]): Paginated<T> {
  return { count, next: null, previous: null, results };
}

describe('pagination shape compatibility', () => {
  it('recognizes and unwraps DRF envelopes while preserving legacy arrays', () => {
    const envelope = page(1, [{ id: 1 }]);
    expect(isPaginated(envelope)).toBe(true);
    expect(unwrapList(envelope)).toEqual([{ id: 1 }]);
    expect(unwrapList([{ id: 2 }])).toEqual([{ id: 2 }]);
    expect(unwrapList({ data: [] })).toEqual([]);
  });

  it('appends limit/offset to URLs with and without an existing query', () => {
    expect(withLimitOffset('/api/devices/', 50, 0)).toBe('/api/devices/?limit=50&offset=0');
    expect(withLimitOffset('/api/devices/?site=demo', 50, 10)).toBe(
      '/api/devices/?site=demo&limit=50&offset=10',
    );
  });
});

describe('fetchAllPages', () => {
  it('walks DRF pages using the number of rows already collected as offset', async () => {
    const fetchPage = vi
      .fn<(limit: number, offset: number) => Promise<unknown>>()
      .mockResolvedValueOnce(page(3, [{ id: 1 }, { id: 2 }]))
      .mockResolvedValueOnce(page(3, [{ id: 3 }]));

    await expect(fetchAllPages<{ id: number }>(fetchPage)).resolves.toEqual([
      { id: 1 },
      { id: 2 },
      { id: 3 },
    ]);
    expect(fetchPage.mock.calls).toEqual([
      [FULL_PAGE_LIMIT, 0],
      [FULL_PAGE_LIMIT, 2],
    ]);
  });

  it('returns a legacy bare array without issuing another request', async () => {
    const fetchPage = vi.fn().mockResolvedValue([{ id: 1 }]);
    await expect(fetchAllPages(fetchPage)).resolves.toEqual([{ id: 1 }]);
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });

  it('stops safely when a later response is empty or malformed', async () => {
    const fetchPage = vi
      .fn<(limit: number, offset: number) => Promise<unknown>>()
      .mockResolvedValueOnce(page(3, [{ id: 1 }]))
      .mockResolvedValueOnce(page(3, []));
    await expect(fetchAllPages(fetchPage)).resolves.toEqual([{ id: 1 }]);
    expect(fetchPage).toHaveBeenCalledTimes(2);
  });
});
