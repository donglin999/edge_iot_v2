import { fetchWithAbort, isAbortError } from './http';

describe('fetchWithAbort', () => {
  it('forwards the signal without dropping other request options', async () => {
    const controller = new AbortController();
    const response = new Response('{}', { status: 200 });
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response);

    await expect(
      fetchWithAbort('/api/example/', controller.signal, {
        headers: { Accept: 'application/json' },
        method: 'GET',
      }),
    ).resolves.toBe(response);

    expect(fetchMock).toHaveBeenCalledWith('/api/example/', {
      headers: { Accept: 'application/json' },
      method: 'GET',
      signal: controller.signal,
    });
  });
});

describe('isAbortError', () => {
  it('recognizes DOMException and Error aborts', () => {
    expect(isAbortError(new DOMException('cancelled', 'AbortError'))).toBe(true);
    const error = new Error('cancelled');
    error.name = 'AbortError';
    expect(isAbortError(error)).toBe(true);
  });

  it('does not swallow unrelated failures', () => {
    expect(isAbortError(new Error('network failed'))).toBe(false);
    expect(isAbortError('AbortError')).toBe(false);
  });
});
