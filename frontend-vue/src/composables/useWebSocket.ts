import {
  getCurrentScope,
  onScopeDispose,
  readonly,
  ref,
  shallowRef,
  toValue,
  watch,
  type MaybeRefOrGetter,
} from 'vue';

export interface WebSocketMessage {
  type: string;
  data: unknown;
}

export interface UseWebSocketOptions {
  url: MaybeRefOrGetter<string>;
  onMessage?: (message: WebSocketMessage) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: (error: Event) => void;
  autoReconnect?: MaybeRefOrGetter<boolean>;
  reconnectInterval?: MaybeRefOrGetter<number>;
  /** Close the socket without unmounting the component when this becomes false. */
  enabled?: MaybeRefOrGetter<boolean>;
}

export enum WebSocketStatus {
  CONNECTING = 'connecting',
  CONNECTED = 'connected',
  DISCONNECTED = 'disconnected',
  ERROR = 'error',
}

const normalizeUrl = (url: string) => url.replace(/^http/, 'ws');

export function useWebSocket(options: UseWebSocketOptions) {
  const status = ref(WebSocketStatus.DISCONNECTED);
  const socket = shallowRef<WebSocket | null>(null);
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let shouldConnect = false;
  let disposed = false;

  const isEnabled = () => toValue(options.enabled ?? true);
  const shouldReconnect = () => toValue(options.autoReconnect ?? true);
  const reconnectDelay = () => toValue(options.reconnectInterval ?? 3_000);

  const clearReconnectTimer = () => {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  const scheduleReconnect = () => {
    clearReconnectTimer();
    if (disposed || !shouldConnect || !isEnabled() || !shouldReconnect()) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, reconnectDelay());
  };

  const connect = () => {
    if (disposed || !isEnabled()) return;
    if (
      socket.value?.readyState === WebSocket.OPEN ||
      socket.value?.readyState === WebSocket.CONNECTING
    ) {
      return;
    }

    shouldConnect = true;
    clearReconnectTimer();
    status.value = WebSocketStatus.CONNECTING;

    try {
      const candidate = new WebSocket(normalizeUrl(toValue(options.url)));
      socket.value = candidate;

      candidate.onopen = () => {
        if (socket.value !== candidate) return;
        status.value = WebSocketStatus.CONNECTED;
        options.onOpen?.();
      };

      candidate.onmessage = (event) => {
        if (socket.value !== candidate) return;
        try {
          options.onMessage?.(JSON.parse(String(event.data)) as WebSocketMessage);
        } catch (error) {
          console.error('Failed to parse WebSocket message:', error);
        }
      };

      candidate.onerror = (error) => {
        if (socket.value !== candidate) return;
        status.value = WebSocketStatus.ERROR;
        options.onError?.(error);
      };

      candidate.onclose = () => {
        options.onClose?.();
        if (socket.value !== candidate) return;
        socket.value = null;
        status.value = WebSocketStatus.DISCONNECTED;
        scheduleReconnect();
      };
    } catch (error) {
      socket.value = null;
      status.value = WebSocketStatus.ERROR;
      console.error('Failed to create WebSocket:', error);
    }
  };

  const disconnect = () => {
    shouldConnect = false;
    clearReconnectTimer();
    const current = socket.value;
    socket.value = null;
    status.value = WebSocketStatus.DISCONNECTED;
    current?.close();
  };

  const send = (data: unknown): boolean => {
    if (socket.value?.readyState !== WebSocket.OPEN) return false;
    socket.value.send(JSON.stringify(data));
    return true;
  };

  const stopWatching = watch(
    [() => toValue(options.url), isEnabled],
    ([, enabled]) => {
      disconnect();
      if (enabled) connect();
    },
    { immediate: true, flush: 'sync' },
  );

  if (getCurrentScope()) {
    onScopeDispose(() => {
      disposed = true;
      stopWatching();
      disconnect();
    });
  }

  return {
    status: readonly(status),
    send,
    connect,
    disconnect,
  };
}
