import { useOnline } from "../lib/useOnline";

/** Says what is actually true, and what happens next. */
export function OfflineBanner() {
  const online = useOnline();
  if (online) return null;
  return (
    <div className="offline" role="status">
      You are offline. This page is static, so it keeps working — only the links out (GitHub, releases) need a connection.
    </div>
  );
}
