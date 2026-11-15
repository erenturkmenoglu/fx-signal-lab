import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { ConnEvent, Obs, ObsMessage, SystemStatus } from "./types";

const BUFFER = 600; // seconds of observations kept per instrument in the browser

export type Link = "connecting" | "open" | "closed";

export interface Live {
  status: SystemStatus | null;
  link: Link;
  events: ConnEvent[];
  /** Observations (ascending) of one instrument. */
  observations: (symbol: string) => Obs[];
  /** Subscribe to per-second messages (used by the chart for incremental updates). */
  subscribe: (listener: (message: ObsMessage) => void) => () => void;
  /** Incremented whenever the buffers were replaced (connect, resync after a backfill). */
  generation: number;
}

type Incoming =
  | { type: "hello"; status: SystemStatus; recent: Record<string, Obs[]> }
  | { type: "status"; status: SystemStatus }
  | { type: "event"; event: ConnEvent }
  | { type: "error"; error: string }
  | ObsMessage;

export function useLive(): Live {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [link, setLink] = useState<Link>("connecting");
  const [events, setEvents] = useState<ConnEvent[]>([]);
  const [generation, setGeneration] = useState(0);
  const [, setTick] = useState(0);
  const buffers = useRef(new Map<string, Obs[]>());
  const listeners = useRef(new Set<(message: ObsMessage) => void>());

  useEffect(() => {
    let socket: WebSocket | null = null;
    let closed = false;
    let retry = 1000;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let poll: ReturnType<typeof setInterval> | undefined;

    const loadEvents = () => {
      api
        .events()
        .then(setEvents)
        .catch(() => undefined);
    };

    const connect = () => {
      if (closed) return;
      setLink("connecting");
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/ws/live`);
      socket.onopen = () => {
        retry = 1000;
        setLink("open");
      };
      socket.onmessage = (raw) => {
        const message = JSON.parse(raw.data as string) as Incoming;
        if (message.type === "hello") {
          buffers.current = new Map(Object.entries(message.recent).map(([symbol, list]) => [symbol, list.slice(-BUFFER)]));
          setStatus(message.status);
          setGeneration((g) => g + 1);
          loadEvents();
        } else if (message.type === "status") {
          setStatus(message.status);
        } else if (message.type === "obs") {
          const list = buffers.current.get(message.symbol) ?? [];
          list.push(message.obs);
          if (list.length > BUFFER) list.splice(0, list.length - BUFFER);
          buffers.current.set(message.symbol, list);
          for (const listener of listeners.current) listener(message);
          setTick((t) => t + 1);
        } else if (message.type === "event") {
          setEvents((previous) => [message.event, ...previous].slice(0, 100));
        } else if (message.type === "error") {
          setStatus((previous) => previous ?? ({ startup_error: message.error } as SystemStatus));
        }
      };
      socket.onclose = () => {
        if (closed) return;
        setLink("closed");
        timer = setTimeout(connect, retry);
        retry = Math.min(retry * 2, 10_000);
      };
      socket.onerror = () => socket?.close();
    };

    connect();
    // While the stream is down keep asking the REST API, so the page can tell
    // "backend unreachable" from "backend up but refusing to start".
    poll = setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) return;
      api
        .status()
        .then(setStatus)
        .catch(() => undefined);
    }, 3000);

    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      if (poll) clearInterval(poll);
      socket?.close();
    };
  }, []);

  const observations = useCallback((symbol: string) => buffers.current.get(symbol) ?? [], []);
  const subscribe = useCallback((listener: (message: ObsMessage) => void) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  return { status, link, events, observations, subscribe, generation };
}
