import { effectScope, nextTick, ref } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useWebSocket, WebSocketStatus } from './useWebSocket';

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  readyState = FakeWebSocket.CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  sent: string[] = [];
  close = vi.fn(() => {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.(new Event('close') as CloseEvent);
  });

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.(new Event('open'));
  }

  message(data: string) {
    this.onmessage?.(new MessageEvent('message', { data }));
  }

  serverClose() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.(new Event('close') as CloseEvent);
  }

  send(data: string) {
    this.sent.push(data);
  }
}

describe('useWebSocket', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('connects, parses messages and sends JSON', () => {
    const onMessage = vi.fn();
    const scope = effectScope();
    const api = scope.run(() =>
      useWebSocket({ url: 'http://localhost/ws/data/', onMessage }),
    )!;
    const ws = FakeWebSocket.instances[0]!;

    expect(ws.url).toBe('ws://localhost/ws/data/');
    expect(api.status.value).toBe(WebSocketStatus.CONNECTING);
    ws.open();
    expect(api.status.value).toBe(WebSocketStatus.CONNECTED);

    ws.message(JSON.stringify({ type: 'data_point_update', data: { value: 42 } }));
    expect(onMessage).toHaveBeenCalledWith({
      type: 'data_point_update',
      data: { value: 42 },
    });
    expect(api.send({ cursor: 9 })).toBe(true);
    expect(ws.sent).toEqual(['{"cursor":9}']);

    scope.stop();
  });

  it('reconnects after the configured delay, but not after scope disposal', () => {
    vi.useFakeTimers();
    const onClose = vi.fn();
    const scope = effectScope();
    const api = scope.run(() =>
      useWebSocket({
        url: 'ws://localhost/ws/global/',
        reconnectInterval: 250,
        onClose,
      }),
    )!;

    FakeWebSocket.instances[0]!.serverClose();
    expect(api.status.value).toBe(WebSocketStatus.DISCONNECTED);
    expect(onClose).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(249);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    FakeWebSocket.instances[1]!.serverClose();
    scope.stop();
    vi.advanceTimersByTime(250);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('reacts to enabled and url changes without leaving stale reconnect timers', async () => {
    vi.useFakeTimers();
    const enabled = ref(false);
    const url = ref('https://example.test/ws/one/');
    const onClose = vi.fn();
    const scope = effectScope();
    const api = scope.run(() =>
      useWebSocket({ url, enabled, reconnectInterval: 10, onClose }),
    )!;

    expect(FakeWebSocket.instances).toHaveLength(0);
    enabled.value = true;
    await nextTick();
    expect(FakeWebSocket.instances[0]!.url).toBe('wss://example.test/ws/one/');

    const first = FakeWebSocket.instances[0]!;
    first.open();
    url.value = 'https://example.test/ws/two/';
    await nextTick();
    expect(first.close).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances[1]!.url).toBe('wss://example.test/ws/two/');
    vi.advanceTimersByTime(20);
    expect(FakeWebSocket.instances).toHaveLength(2);

    enabled.value = false;
    await nextTick();
    expect(api.status.value).toBe(WebSocketStatus.DISCONNECTED);
    expect(FakeWebSocket.instances[1]!.close).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
    scope.stop();
  });

  it('does not notify a disposed consumer when its socket closes', () => {
    const onClose = vi.fn();
    const scope = effectScope();
    scope.run(() => useWebSocket({ url: 'ws://localhost/ws/global/', onClose }));
    const ws = FakeWebSocket.instances[0]!;

    scope.stop();
    expect(ws.close).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('ignores malformed frames without disconnecting the socket', () => {
    const onMessage = vi.fn();
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const scope = effectScope();
    const api = scope.run(() => useWebSocket({ url: '/ws/global/', onMessage }))!;
    const ws = FakeWebSocket.instances[0]!;
    ws.open();

    ws.message('{not-json');
    expect(onMessage).not.toHaveBeenCalled();
    expect(api.status.value).toBe(WebSocketStatus.CONNECTED);
    expect(consoleSpy).toHaveBeenCalledOnce();
    scope.stop();
  });
});
