"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type WebSocketMessage = {
  type: string;
  task_id?: string;
  event_type?: string;
  data?: unknown;
  timestamp?: number;
  message?: string;
};

type UseWebSocketOptions = {
  taskId?: string;
  /** Custom WS path (overrides task/global default). Use with buildWsUrl semantics. */
  path?: string;
  enabled?: boolean;
  onMessage?: (message: WebSocketMessage) => void;
  onError?: (error: Event) => void;
  onClose?: (event: CloseEvent) => void;
  onOpen?: (event: Event) => void;
  reconnectInterval?: number;
  maxReconnectAttempts?: number;
};

/** Derive ws(s) URL from NEXT_PUBLIC_API_BASE (Gateway), never hardcode :8090. */
export function buildWsUrl(path: string, apiKey?: string): string {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8080";
  const u = new URL(apiBase);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  const basePath = u.pathname.replace(/\/$/, "");
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  u.pathname = `${basePath}${cleanPath}`;
  u.search = "";
  if (apiKey) {
    u.searchParams.set("api_key", apiKey);
  }
  return u.toString();
}

function detachSocket(ws: WebSocket) {
  ws.onopen = null;
  ws.onmessage = null;
  ws.onerror = null;
  ws.onclose = null;
}

export function useWebSocket(options: UseWebSocketOptions = {}) {
  const {
    taskId,
    path: customPath,
    enabled = true,
    onMessage,
    onError,
    onClose,
    onOpen,
    reconnectInterval = 3000,
    maxReconnectAttempts = 8,
  } = options;

  const [isConnected, setIsConnected] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const urlRef = useRef<string | null>(null);
  const intentionalCloseRef = useRef(false);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onMessageRef = useRef(onMessage);
  const onErrorRef = useRef(onError);
  const onCloseRef = useRef(onClose);
  const onOpenRef = useRef(onOpen);

  useEffect(() => {
    onMessageRef.current = onMessage;
    onErrorRef.current = onError;
    onCloseRef.current = onClose;
    onOpenRef.current = onOpen;
  }, [onMessage, onError, onClose, onOpen]);

  const disconnect = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    const ws = wsRef.current;
    wsRef.current = null;
    urlRef.current = null;
    if (!ws) {
      setIsConnected(false);
      return;
    }
    intentionalCloseRef.current = true;
    detachSocket(ws);
    try {
      if (ws.readyState === WebSocket.OPEN) {
        ws.close(1000, "client disconnect");
      } else if (ws.readyState === WebSocket.CONNECTING) {
        // Avoid "closed before the connection is established" by waiting
        // for the handshake, then closing immediately.
        const abort = () => {
          try {
            ws.close(1000, "client disconnect");
          } catch {
            // ignore
          }
        };
        ws.addEventListener("open", abort);
        ws.addEventListener("error", abort);
      }
    } catch {
      // ignore
    }
    setIsConnected(false);
  }, []);

  const connect = useCallback(() => {
    if (!enabled) return;
    if (typeof window === "undefined") return;

    // Query api_key removed on Gateway; WS relies on AUTH_REQUIRED=false locally
    // or a future cookie/ticket. Do not put session tokens in the URL.
    const path = customPath
      ? customPath
      : taskId
        ? `/v1/tasks/${taskId}/events/ws`
        : `/v1/events/ws`;
    const wsUrl = buildWsUrl(path);

    const existing = wsRef.current;
    if (
      existing &&
      urlRef.current === wsUrl &&
      (existing.readyState === WebSocket.OPEN ||
        existing.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    if (existing) {
      intentionalCloseRef.current = true;
      detachSocket(existing);
      try {
        if (existing.readyState === WebSocket.OPEN) {
          existing.close(1000, "replace");
        } else if (existing.readyState === WebSocket.CONNECTING) {
          const abort = () => {
            try {
              existing.close(1000, "replace");
            } catch {
              // ignore
            }
          };
          existing.addEventListener("open", abort);
          existing.addEventListener("error", abort);
        }
      } catch {
        // ignore
      }
      wsRef.current = null;
    }

    intentionalCloseRef.current = false;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      urlRef.current = wsUrl;

      ws.onopen = (event) => {
        if (wsRef.current !== ws) return;
        setIsConnected(true);
        setConnectionError(null);
        reconnectAttemptsRef.current = 0;
        onOpenRef.current?.(event);
      };

      ws.onmessage = (event) => {
        if (wsRef.current !== ws) return;
        try {
          const message = JSON.parse(event.data) as WebSocketMessage;
          onMessageRef.current?.(message);
        } catch (error) {
          console.error("Failed to parse WebSocket message:", error);
        }
      };

      ws.onerror = (event) => {
        if (wsRef.current !== ws || intentionalCloseRef.current) return;
        setConnectionError("WebSocket connection error");
        onErrorRef.current?.(event);
      };

      ws.onclose = (event) => {
        if (wsRef.current === ws) {
          wsRef.current = null;
          urlRef.current = null;
        }
        setIsConnected(false);
        if (!intentionalCloseRef.current) {
          onCloseRef.current?.(event);
        }

        if (
          intentionalCloseRef.current ||
          event.code === 1000 ||
          reconnectAttemptsRef.current >= maxReconnectAttempts
        ) {
          return;
        }
        reconnectAttemptsRef.current += 1;
        reconnectTimeoutRef.current = setTimeout(() => {
          connect();
        }, reconnectInterval);
      };
    } catch (error) {
      setConnectionError("Failed to create WebSocket connection");
      console.error("WebSocket connection error:", error);
    }
  }, [enabled, taskId, customPath, reconnectInterval, maxReconnectAttempts]);

  const sendMessage = useCallback((message: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(message));
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      disconnect();
      return;
    }
    // Defer so React Strict Mode's mount→unmount→remount does not open+abort
    // a handshake (avoids "closed before the connection is established").
    const timer = setTimeout(() => connect(), 0);
    return () => {
      clearTimeout(timer);
      disconnect();
    };
  }, [connect, disconnect, enabled]);

  return {
    isConnected,
    connectionError,
    sendMessage,
    disconnect,
    reconnect: connect,
  };
}

export function useTaskWebSocket(
  taskId: string | null,
  onTaskEvent?: (event: WebSocketMessage) => void
) {
  const [taskState, setTaskState] = useState<unknown>(null);
  const [taskEvents, setTaskEvents] = useState<WebSocketMessage[]>([]);

  const handleMessage = useCallback(
    (message: WebSocketMessage) => {
      switch (message.type) {
        case "initial_state":
          setTaskState(message.data);
          break;
        case "initial_events":
          setTaskEvents(
            Array.isArray(message.data)
              ? (message.data as WebSocketMessage[])
              : []
          );
          break;
        case "task_event":
          setTaskEvents((prev) => [...prev, message]);
          onTaskEvent?.(message);
          break;
        default:
          onTaskEvent?.(message);
      }
    },
    [onTaskEvent]
  );

  const ws = useWebSocket({
    taskId: taskId || undefined,
    enabled: Boolean(taskId),
    onMessage: handleMessage,
  });

  useEffect(() => {
    setTaskState(null);
    setTaskEvents([]);
  }, [taskId]);

  return {
    ...ws,
    taskState,
    taskEvents,
  };
}

export function useGlobalWebSocket(
  onSystemEvent?: (event: WebSocketMessage) => void
) {
  const [systemState, setSystemState] = useState<unknown>(null);

  const handleMessage = useCallback(
    (message: WebSocketMessage) => {
      switch (message.type) {
        case "initial_state":
          setSystemState(message.data);
          break;
        case "system_event":
          onSystemEvent?.(message);
          break;
        default:
          onSystemEvent?.(message);
      }
    },
    [onSystemEvent]
  );

  const ws = useWebSocket({ onMessage: handleMessage });
  return { ...ws, systemState };
}
