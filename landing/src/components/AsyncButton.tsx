import type { ButtonHTMLAttributes } from "react";
import { useMutation } from "../lib/useMutation";

export function Spinner({ className = "spinner" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

type Props = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onClick"> & {
  onClick: () => Promise<unknown>;
  pendingLabel?: string;
  variant?: "primary" | "ghost" | "quiet";
};

/**
 * One button component, correct by default: double-submit guarded, busy state
 * announced, never spams the console when the handler rejects.
 */
export function AsyncButton({ onClick, children, pendingLabel = "Working…", variant = "primary", ...rest }: Props) {
  const { mutate, isPending } = useMutation(onClick);
  return (
    <button
      {...rest}
      type={rest.type ?? "button"}
      onClick={() => {
        mutate().catch(() => {
          /* surfaced by the caller's error state */
        });
      }}
      disabled={isPending || rest.disabled}
      aria-busy={isPending}
      className={`btn btn--${variant}`}
    >
      {isPending && <Spinner className="spinner spinner--inline" />}
      {isPending ? pendingLabel : children}
    </button>
  );
}
