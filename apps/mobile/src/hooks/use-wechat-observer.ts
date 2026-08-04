import { useCallback, useEffect, useRef, useState } from "react";

import type { PocketApi } from "@/lib/api";
import type {
  SourceCoverageGaps,
  SourcePairing,
  WechatObserverStatus,
} from "@/lib/types";

const EMPTY_GAPS: SourceCoverageGaps = { items: [], total: 0 };

export function useWechatObserver(api: PocketApi, sourceId: string) {
  const mountedRef = useRef(true);
  const requestRef = useRef(0);
  const [status, setStatus] = useState<WechatObserverStatus | null>(null);
  const [gaps, setGaps] = useState<SourceCoverageGaps>(EMPTY_GAPS);
  const [pairing, setPairing] = useState<SourcePairing | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busyAction, setBusyAction] = useState<
    "pairing" | "pause" | "resume" | "revoke" | null
  >(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestRef.current += 1;
    };
  }, []);

  const reload = useCallback(
    async (quiet = false) => {
      const request = requestRef.current + 1;
      requestRef.current = request;
      if (!quiet) setRefreshing(true);
      try {
        const [statusResult, gapsResult] = await Promise.allSettled([
          api.observerStatus(sourceId),
          api.sourceCoverageGaps(sourceId, 20),
        ]);
        if (!mountedRef.current || requestRef.current !== request) return;
        if (statusResult.status === "fulfilled") {
          setStatus(statusResult.value);
        }
        if (gapsResult.status === "fulfilled") {
          setGaps(gapsResult.value);
        }
        const failed =
          statusResult.status === "rejected"
            ? statusResult.reason
            : gapsResult.status === "rejected"
              ? gapsResult.reason
              : null;
        setError(
          failed instanceof Error
            ? failed.message
            : failed
              ? "部分网页观察器状态暂时无法读取"
              : null,
        );
      } catch (caught) {
        if (!mountedRef.current || requestRef.current !== request) return;
        setError(
          caught instanceof Error ? caught.message : "暂时无法读取网页观察器状态",
        );
      } finally {
        if (mountedRef.current && requestRef.current === request) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    [api, sourceId],
  );

  useEffect(() => {
    const initialTimer = setTimeout(() => void reload(true), 0);
    const timer = setInterval(() => void reload(true), 20_000);
    return () => {
      clearTimeout(initialTimer);
      clearInterval(timer);
    };
  }, [reload]);

  useEffect(() => {
    if (!pairing?.expiresAt) return;
    const expiresAt = Date.parse(pairing.expiresAt);
    if (Number.isNaN(expiresAt)) return;
    const timer = setTimeout(
      () => setPairing((current) => (current?.id === pairing.id ? null : current)),
      Math.max(0, Math.min(expiresAt - Date.now(), 2_147_483_647)),
    );
    return () => clearTimeout(timer);
  }, [pairing]);

  const createPairing = useCallback(async () => {
    setBusyAction("pairing");
    setError(null);
    try {
      const created = await api.createObserverPairing(sourceId);
      if (!created.id || !created.pairingCode) {
        throw new Error("服务没有返回有效配对码");
      }
      if (mountedRef.current) setPairing(created);
      await reload(true);
      return created;
    } catch (caught) {
      if (mountedRef.current) {
        setError(caught instanceof Error ? caught.message : "创建配对码失败");
      }
      throw caught;
    } finally {
      if (mountedRef.current) setBusyAction(null);
    }
  }, [api, reload, sourceId]);

  const revokePairing = useCallback(async () => {
    if (!pairing) return;
    setBusyAction("revoke");
    setError(null);
    try {
      await api.revokeObserverPairing(sourceId, pairing.id);
      if (mountedRef.current) setPairing(null);
      await reload(true);
    } catch (caught) {
      if (mountedRef.current) {
        setError(caught instanceof Error ? caught.message : "撤销配对码失败");
      }
    } finally {
      if (mountedRef.current) setBusyAction(null);
    }
  }, [api, pairing, reload, sourceId]);

  const setPaused = useCallback(
    async (paused: boolean) => {
      setBusyAction(paused ? "pause" : "resume");
      setError(null);
      try {
        await api.setObserverPaused(sourceId, paused);
        await reload(true);
      } catch (caught) {
        if (mountedRef.current) {
          setError(
            caught instanceof Error
              ? caught.message
              : paused
                ? "暂停采集失败"
                : "恢复采集失败",
          );
        }
      } finally {
        if (mountedRef.current) setBusyAction(null);
      }
    },
    [api, reload, sourceId],
  );

  return {
    status,
    gaps,
    pairing,
    loading,
    refreshing,
    busyAction,
    error,
    reload: () => reload(false),
    createPairing,
    revokePairing,
    setPaused,
  };
}
