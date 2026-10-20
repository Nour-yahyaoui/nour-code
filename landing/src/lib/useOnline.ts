import { useEffect, useState } from "react";

/** `navigator.onLine`, kept in sync with the browser's online/offline events. */
export function useOnline() {
  const [online, setOnline] = useState(() => (typeof navigator === "undefined" ? true : navigator.onLine));

  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    addEventListener("online", up);
    addEventListener("offline", down);
    return () => {
      removeEventListener("online", up);
      removeEventListener("offline", down);
    };
  }, []);

  return online;
}
