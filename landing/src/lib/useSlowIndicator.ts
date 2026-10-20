import { useEffect, useState } from "react";

/** True once `active` has been true for longer than `thresholdMs`. */
export function useSlowIndicator(active: boolean, thresholdMs = 3000) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!active) {
      setSlow(false);
      return;
    }
    const timer = setTimeout(() => setSlow(true), thresholdMs);
    return () => clearTimeout(timer);
  }, [active, thresholdMs]);
  return slow;
}
