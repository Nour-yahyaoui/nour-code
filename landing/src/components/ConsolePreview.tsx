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
      </div>

      <div className="preview__body">
        <div className="preview__chat">
          <p className="preview__who preview__who--user">you</p>
          <p className="preview__bubble">add rate limiting to the chat route</p>

          <p className="preview__who">nour-code</p>
          <details className="preview__think" open>
            <summary>thinking · 214 tokens</summary>
            <p>
              main.py exposes /chat with no limiter. A token bucket per session is the smallest
              change that still behaves. I should read the route, patch it, then run the tests.
            </p>
          </details>

          <div className="preview__card">
            <span className="preview__card-name">apply_patch</span>
            <span className="preview__card-args">nour_code/main.py · +34 −2</span>
            <span className="preview__card-state">ok 9.7 ms</span>
          </div>

          <p className="preview__answer">
            Added a token bucket to <code>/chat</code>: 30 requests per minute per session, returning
            <code>429</code> with <code>Retry-After</code>. Two tool calls, 1.9 s.
          </p>

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
    limit = buckets.hit(session.id)
    if not limit.allowed:
        raise HTTPException(429, headers={
            "Retry-After": str(limit.retry_after)})`}</pre>
          </div>
        </aside>
      </div>

      <figcaption className="preview__foot">
        Recreated in HTML from the real console — not a screenshot. Both sidebars collapse;
        the event stream and the files pane are optional.
      </figcaption>
    </figure>
  );
}
