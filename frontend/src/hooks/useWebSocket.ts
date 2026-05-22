import { useEffect, useRef, useState, useCallback } from 'react';

export interface WebSocketMessage {
  type: string;
  data: unknown;
}

export interface UseWebSocketOptions {
  url: string;
  onMessage?: (message: WebSocketMessage) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: (error: Event) => void;
  autoReconnect?: boolean;
  reconnectInterval?: number;
  /**
   * Upper bound for reconnect backoff. When set greater than
   * `reconnectInterval`, the reconnect delay grows exponentially
   * (interval, ×2, ×4, …) capped at this value, and resets to the base
   * interval once a connection succeeds. When unset the delay stays fixed
   * at `reconnectInterval` (backward-compatible default).
   */
  maxReconnectInterval?: number;
  /**
   * When false the hook keeps the socket closed (and closes an open one).
   * Lets a parent toggle realtime updates on/off without unmounting.
   */
  enabled?: boolean;
}

export enum WebSocketStatus {
  CONNECTING = 'connecting',
  CONNECTED = 'connected',
  DISCONNECTED = 'disconnected',
  ERROR = 'error',
}

export function useWebSocket(options: UseWebSocketOptions) {
  const {
    url,
    onMessage,
    onOpen,
    onClose,
    onError,
    autoReconnect = true,
    reconnectInterval = 3000,
    maxReconnectInterval,
    enabled = true,
  } = options;

  const [status, setStatus] = useState<WebSocketStatus>(WebSocketStatus.DISCONNECTED);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shouldConnectRef = useRef(true);
  // Number of consecutive failed connects — drives exponential backoff.
  // Reset to 0 on a successful open or an explicit (re)connect.
  const reconnectAttemptsRef = useRef(0);

  // Keep the latest callbacks/config in refs. The WebSocket lifecycle must NOT
  // restart just because a parent re-rendered with new callback identities —
  // otherwise the socket churns (disconnect/reconnect) on every parent render.
  // `connect` therefore only depends on `url`.
  const onMessageRef = useRef(onMessage);
  const onOpenRef = useRef(onOpen);
  const onCloseRef = useRef(onClose);
  const onErrorRef = useRef(onError);
  const autoReconnectRef = useRef(autoReconnect);
  const reconnectIntervalRef = useRef(reconnectInterval);
  const maxReconnectIntervalRef = useRef(maxReconnectInterval);

  useEffect(() => {
    onMessageRef.current = onMessage;
    onOpenRef.current = onOpen;
    onCloseRef.current = onClose;
    onErrorRef.current = onError;
    autoReconnectRef.current = autoReconnect;
    reconnectIntervalRef.current = reconnectInterval;
    maxReconnectIntervalRef.current = maxReconnectInterval;
  });

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      return;
    }

    setStatus(WebSocketStatus.CONNECTING);

    try {
      // Convert http/https to ws/wss
      const wsUrl = url.replace(/^http/, 'ws');
      const ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        // A clean connection resets the backoff ladder.
        reconnectAttemptsRef.current = 0;
        setStatus(WebSocketStatus.CONNECTED);
        onOpenRef.current?.();
      };

      ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data) as WebSocketMessage;
          onMessageRef.current?.(message);
        } catch (err) {
          console.error('Failed to parse WebSocket message:', err);
        }
      };

      ws.onclose = () => {
        setStatus(WebSocketStatus.DISCONNECTED);
        onCloseRef.current?.();

        // Auto-reconnect if enabled and component is still mounted.
        // With `maxReconnectInterval` set the delay grows exponentially
        // (base, ×2, ×4, …) capped at the max; otherwise it stays fixed.
        if (autoReconnectRef.current && shouldConnectRef.current) {
          const base = reconnectIntervalRef.current;
          const max = maxReconnectIntervalRef.current;
          const attempt = reconnectAttemptsRef.current;
          const delay =
            max && max > base
              ? Math.min(base * 2 ** attempt, max)
              : base;
          reconnectAttemptsRef.current = attempt + 1;
          reconnectTimeoutRef.current = setTimeout(() => {
            connect();
          }, delay);
        }
      };

      ws.onerror = (error) => {
        setStatus(WebSocketStatus.ERROR);
        onErrorRef.current?.(error);
      };

      wsRef.current = ws;
    } catch (err) {
      console.error('Failed to create WebSocket:', err);
      setStatus(WebSocketStatus.ERROR);
    }
  }, [url]);

  const disconnect = useCallback(() => {
    shouldConnectRef.current = false;
    reconnectAttemptsRef.current = 0;

    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    setStatus(WebSocketStatus.DISCONNECTED);
  }, []);

  const send = useCallback((data: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    } else {
      console.warn('WebSocket is not connected. Cannot send message.');
    }
  }, []);

  // Single lifecycle effect: connect when enabled, tear down otherwise / on
  // unmount. Re-runs only when `enabled` or `url` (via `connect`) changes.
  useEffect(() => {
    if (!enabled) {
      disconnect();
      return;
    }

    shouldConnectRef.current = true;
    connect();

    return () => {
      disconnect();
    };
  }, [enabled, connect, disconnect]);

  return {
    status,
    send,
    connect,
    disconnect,
  };
}
