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
    enabled = true,
  } = options;

  const [status, setStatus] = useState<WebSocketStatus>(WebSocketStatus.DISCONNECTED);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shouldConnectRef = useRef(true);

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

  useEffect(() => {
    onMessageRef.current = onMessage;
    onOpenRef.current = onOpen;
    onCloseRef.current = onClose;
    onErrorRef.current = onError;
    autoReconnectRef.current = autoReconnect;
    reconnectIntervalRef.current = reconnectInterval;
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

        // Auto-reconnect if enabled and component is still mounted
        if (autoReconnectRef.current && shouldConnectRef.current) {
          reconnectTimeoutRef.current = setTimeout(() => {
            connect();
          }, reconnectIntervalRef.current);
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
