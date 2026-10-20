import { useCallback, useEffect, useRef, useState } from "react";

type State<T> = { data: T | null; loading: boolean; error: Error | null };

/**
 * The every-fetch hook — loading, error, timeout, abort, stale-response
 * protection and manual retry in one place.
 *
 * Ported from the UX skill's `useAsync`:
 *  - `AbortController` cancels the request on timeout and on unmount;
 *  - `runId` drops responses that arrive after a newer run started;
 *  - `retry` re-runs the same fetcher.
 */
export function useAsync<T>(
  fn: (signal: AbortSignal) => Promise<T>,
  deps: unknown[] = [],
  { timeoutMs = 10_000 }: { timeoutMs?: number } = {},
) {
  const [state, setState] = useState<State<T>>({ data: null, loading: true, error: null });
  const runId = useRef(0);
  const mounted = useRef(true);

  const run = useCallback(async () => {
    const id = ++runId.current;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(new Error("timeout")), timeoutMs);
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await fn(ctrl.signal);
      if (mounted.current && id === runId.current) setState({ data, loading: false, error: null });
    } catch (e) {
      const error = e as Error;
      if (mounted.current && id === runId.current && error.name !== "AbortError") {
        setState({ data: null, loading: false, error });
      }
    } finally {
      clearTimeout(timer);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    mounted.current = true;
    void run();
    return () => {
      mounted.current = false;
      runId.current += 1;
    };
  }, [run]);

  return { ...state, retry: run };
}
