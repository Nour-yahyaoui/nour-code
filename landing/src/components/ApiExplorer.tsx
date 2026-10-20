import { useMemo, useState } from "react";
import { ENDPOINTS } from "../data/content";
import { useDebounced } from "../lib/useDebounced";
import { DelayedSpinner } from "./DelayedSpinner";
import { Reveal } from "./Reveal";

const GROUP_LABEL: Record<string, string> = {
  chat: "chat",
  model: "model memory",
  skills: "skills",
  sessions: "sessions & approvals",
  files: "files",
  ops: "observability",
};

export function ApiExplorer() {
  const [query, setQuery] = useState("");
  const term = useDebounced(query, 250).trim().toLowerCase();

  const results = useMemo(() => {
    if (!term) return ENDPOINTS;
    return ENDPOINTS.filter((endpoint) =>
      `${endpoint.method} ${endpoint.path} ${endpoint.summary} ${endpoint.group}`.toLowerCase().includes(term),
    );
  }, [term]);

  return (
    <section className="section section--alt" id="api" aria-labelledby="api-title">
      <div className="container">
        <div className="section__head section__head--split">
          <div>
            <p className="eyebrow">the whole API</p>
            <h2 id="api-title" className="section__title">
              Fifteen endpoints, no hidden plane
            </h2>
            <p className="section__lede">
              Everything the browser console does, curl can do. Interactive docs are served by your
              own instance at <code>/docs</code>.
            </p>
          </div>
          <div className="search">
            <label className="visually-hidden" htmlFor="api-search">
              Filter endpoints
            </label>
            <input
              id="api-search"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter endpoints…"
              autoComplete="off"
              spellCheck={false}
            />
            {query !== term ? <DelayedSpinner delayMs={120} label="Filtering endpoints" /> : null}
          </div>
        </div>

        <Reveal>
          <div className="api" role="list" aria-live="polite">
            {results.map((endpoint) => (
              <div className="api__row" role="listitem" key={`${endpoint.method}-${endpoint.path}`}>
                <span className={`method method--${endpoint.method.toLowerCase()}`}>{endpoint.method}</span>
                <code className="api__path">{endpoint.path}</code>
                <span className="api__summary">{endpoint.summary}</span>
                <span className="api__group">{GROUP_LABEL[endpoint.group] ?? endpoint.group}</span>
              </div>
            ))}
            {results.length === 0 ? (
              <div className="api__empty" role="status">
                <p>No endpoint matches “{query.trim()}”.</p>
                <button type="button" className="btn btn--ghost btn--sm" onClick={() => setQuery("")}>
                  Clear filter
                </button>
              </div>
            ) : null}
          </div>
        </Reveal>

        <p className="api__foot">
          {results.length} of {ENDPOINTS.length} endpoints · no endpoint can read outside the allowed
          roots, including the read-only ones.
        </p>
      </div>
    </section>
  );
}
