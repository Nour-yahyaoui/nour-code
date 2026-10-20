const FILES = [
  { name: "nour_code/", dir: true, open: true },
  { name: "agent.py", depth: 1, fresh: true },
  { name: "ollama_client.py", depth: 1 },
  { name: "tools/", dir: true, depth: 1 },
  { name: "permissions.yaml", fresh: false },
  { name: "README.md" },
];

/**
 * A static, honest picture of the console for visitors who will never run the
 * agent. It is drawn with the same layout vocabulary as the real UI (transcript
 * on the left, workspace on the right) and labelled as a recreation — this page
 * makes no network calls at all.
 */
export function ConsolePreview() {
  return (
    <figure className="preview" aria-label="The nour-code browser console, recreated in HTML">
      <div className="preview__chrome">
        <span className="preview__dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="preview__url">localhost:8000</span>
        <span className="preview__tag">console</span>
        <span className="preview__phase">
          <i aria-hidden="true" />
          waiting for approval
        </span>
      </div>

      <div className="preview__body">
        <div className="preview__chat">
          <p className="preview__who preview__who--user">you</p>
          <p className="preview__bubble">add rate limiting to the chat route</p>

          <p className="preview__who">nour-code</p>
          <details className="preview__think" open>
            <summary>
              <span>thinking</span>
              <span className="preview__pulse" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
              <span className="preview__think-meta">1.4 s · 214 tokens</span>
            </summary>
            <p>
              main.py exposes /chat with no limiter. A token bucket per session is the smallest
              change that still behaves. I should read the route, patch it, then run the tests.
            </p>
          </details>

          <div className="preview__card">
            <span className="preview__card-name">read_file</span>
            <span className="preview__card-args">nour_code/main.py</span>
            <span className="preview__card-state">ok 12 ms</span>
          </div>

          <div className="preview__gate">
            <div className="preview__gate-row">
              <span className="preview__gate-tool">apply_patch</span>
              <span className="preview__gate-risk">low risk</span>
              <span className="preview__gate-clock">4:52</span>
            </div>
            <div className="preview__gate-args">nour_code/main.py · +34 −2</div>
            <div className="preview__gate-actions" aria-hidden="true">
              <span className="is-primary">approve</span>
              <span>reject</span>
              <span>approve all</span>
            </div>
          </div>

          <div className="preview__composer" aria-hidden="true">
            <span>Ask nour-code to build, fix or explain something…</span>
            <b>send</b>
          </div>
        </div>

        <aside className="preview__side">
          <p className="preview__side-head">
            workspace <span>6</span>
          </p>
          <ul className="preview__tree">
            {FILES.map((file) => (
              <li
                key={file.name}
                className={`${file.dir ? "is-dir" : ""} ${file.fresh ? "is-fresh" : ""}`}
                style={file.depth ? { paddingLeft: 18 } : undefined}
              >
                {file.name}
                {file.fresh ? <i aria-hidden="true" /> : null}
              </li>
            ))}
          </ul>
          <div className="preview__viewer">
            <p className="preview__viewer-head">main.py · read-only</p>
            <pre>{`@app.post("/chat")
async def chat(payload: ChatRequest):
    session = await resolve(payload)
    return StreamingResponse(
        stream(session, payload.message),
        media_type="text/event-stream")`}</pre>
          </div>
        </aside>
      </div>

      <figcaption className="preview__foot">
        Recreated in HTML from the real console — not a screenshot. Status and reasoning update
        live, approvals count down, and both sidebars collapse.
      </figcaption>
    </figure>
  );
}
