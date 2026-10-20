import { useEffect, useState } from "react";
import { Spinner } from "./AsyncButton";

/** No flicker on fast responses: only appears after `delayMs`. */
export function DelayedSpinner({ delayMs = 200, label }: { delayMs?: number; label?: string }) {
  const [show, setShow] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setShow(true), delayMs);
    return () => clearTimeout(timer);
  }, [delayMs]);
  if (!show) return null;
  return (
    <span className="inline-spinner" role="status">
      <Spinner className="spinner spinner--inline" />
      {label ? <span className="visually-hidden">{label}</span> : null}
    </span>
  );
}
