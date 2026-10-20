import { useCallback, useState } from "react";
import { explainHealthError, fetchHealth, formatExpiry, warmupModel, type Health } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import { useMutation } from "../lib/useMutation";
import { useSlowIndicator } from "../lib/useSlowIndicator";
import { DelayedSpinner } from "./DelayedSpinner";

type Tone = "ok" | "idle" | "down";

function tone(health: Health): Tone {
  if (!health.ollama_reachable) return "down";
  return health.model_running ? "ok" : "idle";
}

const TONE_LABEL: Record<Tone, string> = {
  ok: "model resident",
  idle: "model idle",
  down: "ollama unreachable",
};

/**
 * The live half of the page. Everything here follows the skill's state
 * checklist: loading → skeleton with the row's real height, error → message +
 * retry, empty/partial → explained, success → no layout shift, plus a slow
 * indicator so a stalled probe is never mistaken for a frozen page.
 */
export function StatusPanel() {
  const { data, loading, error, retry } = useAsync<Health>((signal) => fetchHealth(signal), [], {
    timeoutMs: 6000,
  });
  const slow = useSlowIndicator(loading, 2500);
  const [warmed, setWarmed] = useState<string | null>(null);

  const warm = useMutation(
    useCallback(async () => {
      const result = await warmupModel("30m");
      await retry();
      setWarmed(result.expires_at);
    }, [retry]),
    { timeoutMs: 120_000 },
  );

  const state = data ? tone(data) : null;

  return (
    <section className="status" aria-labelledby="status-title" aria-busy={loading}>
      <div className="status__head">
        <div>
          <p className="eyebrow">
            <span className={`dot ${state ? `dot--${state}` : "dot--wait"}`} aria-hidden="true" />
            live check
          </p>
          <h2 id="status-title" className="status__title">
            Is your agent running?
          </h2>
        </div>
        <button type="button" className="status__refresh" onClick={() => void retry()} disabled={loading}>
          {loading ? <DelayedSpinner label="Checking the API" /> : null}
          re-check
        </button>
      </div>

      {/* loading: skeleton keeps the exact height of the loaded row */}
      {loading && !data ? (
        <div className="status__row" aria-hidden="true">
          <div className="skeleton skeleton--pill" />
          <div className="skeleton skeleton--line" />
          <div className="skeleton skeleton--line skeleton--short" />
        </div>
      ) : null}
      {loading && data ? <p className="status__note">re-checking…</p> : null}

      {/* error: message + retry + the fix */}
      {error ? (
        <div className="status__error" role="alert">
          <p className="status__error-title">Could not reach the local API.</p>
          <p className="status__error-body">{explainHealthError(error)}</p>
          <button type="button" className="btn btn--ghost" onClick={() => void retry()}>
            Retry
          </button>
        </div>
      ) : null}

      {/* success */}
      {data ? (
        <>
          <div className="status__row">
            <span className={`pill pill--${state ?? "idle"}`} aria-live="polite">
              {TONE_LABEL[state ?? "idle"]}
            </span>
            <dl className="status__facts">
              <div>
                <dt>model</dt>
                <dd>{data.model}</dd>
              </div>
              <div>
                <dt>ollama</dt>
                <dd>{data.ollama_version ?? "unknown version"}</dd>
              </div>
              <div>
                <dt>tools</dt>
                <dd>{data.tools}</dd>
              </div>
              <div>
                <dt>pulled</dt>
                <dd>{data.model_loaded ? "yes" : "no"}</dd>
              </div>
            </dl>
          </div>

          <p className="status__note">
            {state === "idle" && !data.model_running
              ? "Pulled but not in memory: Ollama loads qwen3 on the first turn and unloads it 5 minutes after the last one."
              : null}
            {state === "ok"
              ? `In memory now${data.model_expires_at ? ` — ${formatExpiry(data.model_expires_at)}` : ""}.`
              : null}
            {state === "down"
              ? "Start Ollama (`ollama serve`) or fix NOUR_CODE_OLLAMA_BASE_URL. The rest of the page still works."
              : null}
            {data.detail ? ` ${data.detail}` : ""}
          </p>

          {state !== "down" ? (
            <div className="status__actions">
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => warm.mutate().catch(() => undefined)}
                disabled={warm.isPending || data.model_running}
                aria-busy={warm.isPending}
              >
                {warm.isPending ? "loading model…" : data.model_running ? "already warm" : "preload for 30 min"}
              </button>
              {warm.error ? (
                <span className="status__inline-error" role="alert">
                  {warm.error.message === "timeout" ? "Ollama took too long to load the model." : warm.error.message}
                </span>
              ) : null}
              {warmed && !warm.error ? <span className="status__inline-ok">warm until {formatExpiry(warmed)}</span> : null}
            </div>
          ) : null}
        </>
      ) : null}

      {slow ? (
        <p className="status__note status__note--slow" role="status">
          Still checking — localhost is usually instant, so the server may be starting up.
        </p>
      ) : null}
    </section>
  );
}
