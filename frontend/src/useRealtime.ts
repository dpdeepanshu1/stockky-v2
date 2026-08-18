import { useCallback, useEffect, useRef, useState } from "react";
import { getApiUrl } from "./api";

export type RealtimeMessage = {
  channel?: string;
  type?: string;
  task_id?: string;
  status?: string;
  processed?: number;
  total?: number;
  elapsed?: number;
  result?: unknown;
  symbol?: string;
  price?: number;
  close?: number;
  as_of?: string;
  source?: string;
  change_pct?: number;
  [key: string]: unknown;
};

export type LiveQuote = {
  symbol: string;
  price: number;
  close?: number;
  as_of?: string;
  source?: string;
  change_pct?: number;
};

function toWsUrl(httpBase: string): string | null {
  if (!httpBase) return null;
  try {
    const u = new URL(httpBase);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    u.pathname = "/ws";
    u.search = "";
    u.hash = "";
    return u.toString();
  } catch {
    return null;
  }
}

const MAX_WATCHED = 12;

/**
 * WebSocket client for Stockky API Gateway /ws.
 * Scan progress + limited live quotes.
 * - Does NOT HTTP-poll /quote when idle
 * - Clears quote subscriptions when tab is hidden
 * - Caps watched symbols to avoid free-tier overload
 */
export function useStockkyRealtime(onMessage?: (msg: RealtimeMessage) => void) {
  const [connected, setConnected] = useState(false);
  const [quotes, setQuotes] = useState<Record<string, LiveQuote>>({});
  const wsRef = useRef<WebSocket | null>(null);
  const onMsgRef = useRef(onMessage);
  onMsgRef.current = onMessage;
  const retries = useRef(0);
  const watchedRef = useRef<Set<string>>(new Set());

  const connect = useCallback(() => {
    const base = getApiUrl();
    const url = toWsUrl(base);
    if (!url) return;
    if (typeof document !== "undefined" && document.visibilityState === "hidden") {
      return;
    }

    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => {
        setConnected(true);
        retries.current = 0;
        ws.send(JSON.stringify({ action: "subscribe", channel: "all" }));
        const syms = Array.from(watchedRef.current).slice(0, MAX_WATCHED);
        if (syms.length) {
          ws.send(JSON.stringify({ action: "subscribe_quotes", symbols: syms }));
        }
      };
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data) as RealtimeMessage;
          if (data.type === "quote" && data.symbol && data.price != null) {
            const q: LiveQuote = {
              symbol: String(data.symbol).toUpperCase(),
              price: Number(data.price),
              close: data.close != null ? Number(data.close) : undefined,
              as_of: data.as_of ? String(data.as_of) : undefined,
              source: data.source ? String(data.source) : undefined,
              change_pct:
                data.change_pct != null ? Number(data.change_pct) : undefined,
            };
            setQuotes((prev) => ({ ...prev, [q.symbol]: q }));
          }
          onMsgRef.current?.(data);
        } catch {
          /* ignore malformed */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        // Retry only while tab visible
        if (typeof document !== "undefined" && document.visibilityState === "visible") {
          const delay = Math.min(15000, 1500 * Math.pow(1.5, retries.current++));
          setTimeout(() => connect(), delay);
        }
      };
      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          /* ignore */
        }
      };
    } catch {
      setConnected(false);
    }
  }, []);

  useEffect(() => {
    connect();
    const ping = setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: "ping" }));
      }
    }, 30000);

    const onVis = () => {
      if (document.visibilityState === "hidden") {
        // Drop quote watches when user leaves — stops backend /quote fan-out
        const ws = wsRef.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ action: "unsubscribe_quotes" }));
        }
      } else if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        connect();
      } else {
        const syms = Array.from(watchedRef.current).slice(0, MAX_WATCHED);
        if (syms.length) {
          wsRef.current.send(
            JSON.stringify({ action: "subscribe_quotes", symbols: syms })
          );
        }
      }
    };
    document.addEventListener("visibilitychange", onVis);

    return () => {
      clearInterval(ping);
      document.removeEventListener("visibilitychange", onVis);
      try {
        const ws = wsRef.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ action: "unsubscribe_quotes" }));
        }
        wsRef.current?.close();
      } catch {
        /* ignore */
      }
    };
  }, [connect]);

  const subscribeScan = useCallback((taskId: string) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: "subscribe", channel: `scan:${taskId}` }));
      ws.send(JSON.stringify({ action: "poll_scan", task_id: taskId }));
    }
  }, []);

  const subscribeQuotes = useCallback((symbols: string[]) => {
    const cleaned = symbols
      .map((s) => s.toUpperCase().replace(/\.NS$/i, "").replace(/\.BO$/i, "").trim())
      .filter(Boolean);
    // Replace watch set (don't accumulate forever across pages)
    watchedRef.current = new Set(cleaned.slice(0, MAX_WATCHED));
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      // Clear previous then subscribe limited set
      ws.send(JSON.stringify({ action: "unsubscribe_quotes" }));
      const syms = Array.from(watchedRef.current);
      if (syms.length && document.visibilityState !== "hidden") {
        ws.send(JSON.stringify({ action: "subscribe_quotes", symbols: syms }));
      }
    }
  }, []);

  const unsubscribeQuotes = useCallback((symbols?: string[]) => {
    if (symbols) {
      symbols.forEach((s) =>
        watchedRef.current.delete(
          s.toUpperCase().replace(/\.NS$/i, "").replace(/\.BO$/i, "")
        )
      );
    } else {
      watchedRef.current.clear();
    }
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(
        JSON.stringify(
          symbols
            ? { action: "unsubscribe_quotes", symbols }
            : { action: "unsubscribe_quotes" }
        )
      );
    }
  }, []);

  const send = useCallback((payload: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  }, []);

  return { connected, subscribeScan, subscribeQuotes, unsubscribeQuotes, quotes, send };
}
