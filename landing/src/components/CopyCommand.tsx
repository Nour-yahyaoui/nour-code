import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation } from "../lib/useMutation";
import { Spinner } from "./AsyncButton";

async function copyText(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  // Fallback for non-secure origins (plain http on a LAN address).
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  const ok = document.execCommand("copy");
  document.body.removeChild(area);
  if (!ok) throw new Error("clipboard unavailable");
}

type Props = {
  value: string;
  label?: string;
  compact?: boolean;
};

/** A command you can copy, with an honest success/error state. */
export function CopyCommand({ value, label, compact = false }: Props) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const { mutate, isPending, error } = useMutation(() => copyText(value));

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const onClick = useCallback(() => {
    setCopied(false);
    mutate()
      .then(() => {
        setCopied(true);
        timer.current = window.setTimeout(() => setCopied(false), 1800);
      })
      .catch(() => setCopied(false));
  }, [mutate]);

  return (
    <div className={`copy ${compact ? "copy--compact" : ""}`}>
      {label ? <span className="copy__label">{label}</span> : null}
      <code className="copy__code">
        <span className="copy__prompt" aria-hidden="true">
          $
        </span>
        {value}
      </code>
      <button
        type="button"
        className="copy__button"
        onClick={onClick}
        disabled={isPending}
        aria-label={copied ? "Copied" : `Copy: ${value}`}
      >
        {isPending ? <Spinner className="spinner spinner--inline" /> : null}
        <span aria-live="polite">{copied ? "copied" : "copy"}</span>
      </button>
      {error ? (
        <span className="copy__error" role="alert">
          copy blocked — select the text instead
        </span>
      ) : null}
    </div>
  );
}
