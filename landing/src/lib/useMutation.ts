import { useCallback, useRef, useState } from "react";

type Status = "idle" | "pending" | "success" | "error";

/**
 * Submit helper with the double-submit guard from the UX skill: a second call
 * while one is in flight is ignored, a timeout rejects, and the error is
 * surfaced *and* re-thrown so callers can decide what to do next.
 */
export function useMutation<A extends unknown[], R>(
  fn: (...args: A) => Promise<R>,
  { timeoutMs = 15_000 }: { timeoutMs?: number } = {},
) {
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<Error | null>(null);
  const inFlight = useRef(false);

  const mutate = useCallback(
    async (...args: A) => {
      if (inFlight.current) return undefined;
      inFlight.current = true;
      setStatus("pending");
      setError(null);
      try {
        const result = (await Promise.race([
          fn(...args),
          new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), timeoutMs)),
        ])) as R;
        setStatus("success");
        return result;
      } catch (e) {
        setError(e as Error);
        setStatus("error");
        throw e;
      } finally {
        inFlight.current = false;
      }
    },
    [fn, timeoutMs],
  );

  return {
    mutate,
    status,
    isPending: status === "pending",
    error,
    reset: useCallback(() => {
      setStatus("idle");
      setError(null);
    }, []),
  };
}
