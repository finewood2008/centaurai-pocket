import { useCallback, useEffect, useRef, useState } from "react";

import { RequestGeneration } from "@/lib/request-generation";

type RemoteResource<T> = {
  data: T;
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  isDemo: boolean;
  reload: () => Promise<void>;
};

type ResourceState<T> = {
  loader: () => Promise<T>;
  data: T;
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  isDemo: boolean;
};

export function useRemoteResource<T>(
  loader: () => Promise<T>,
  fallback: T,
  reloadKey?: unknown,
): RemoteResource<T> {
  const gateRef = useRef(new RequestGeneration());

  const [state, setState] = useState<ResourceState<T>>({
    loader,
    data: fallback,
    loading: true,
    refreshing: false,
    error: null,
    isDemo: false,
  });

  const load = useCallback(
    async (refresh = false) => {
      const requestGeneration = gateRef.current.begin();
      setState((current) => ({
        loader,
        data: current.loader === loader ? current.data : fallback,
        loading: !refresh,
        refreshing: refresh,
        error: current.loader === loader ? current.error : null,
        isDemo: current.loader === loader ? current.isDemo : false,
      }));

      try {
        const data = await loader();
        if (!gateRef.current.isCurrent(requestGeneration)) return;
        setState({
          loader,
          data,
          loading: false,
          refreshing: false,
          error: null,
          isDemo: false,
        });
      } catch (caught) {
        if (!gateRef.current.isCurrent(requestGeneration)) return;
        setState({
          loader,
          data: fallback,
          loading: false,
          refreshing: false,
          error:
            caught instanceof Error ? caught.message : "暂时无法读取数据",
          isDemo: true,
        });
      }
    },
    [fallback, loader],
  );

  useEffect(() => {
    const gate = gateRef.current;
    const timer = setTimeout(() => void load(), 0);
    return () => {
      clearTimeout(timer);
      gate.invalidate();
    };
  }, [load, reloadKey]);

  const reload = useCallback(() => load(true), [load]);
  const belongsToCurrentLoader = state.loader === loader;

  return {
    data: belongsToCurrentLoader ? state.data : fallback,
    loading: belongsToCurrentLoader ? state.loading : true,
    refreshing:
      belongsToCurrentLoader ? state.refreshing : false,
    error: belongsToCurrentLoader ? state.error : null,
    isDemo: belongsToCurrentLoader ? state.isDemo : false,
    reload,
  };
}
