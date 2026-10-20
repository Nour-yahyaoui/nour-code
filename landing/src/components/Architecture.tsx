import { Reveal } from "./Reveal";

export function Architecture() {
  return (
    <section className="section section--alt" id="architecture" aria-labelledby="arch-title">
      <div className="container">
        <div className="section__head">
          <p className="eyebrow">one process, one hop</p>
          <h2 id="arch-title" className="section__title">
            Everything the model can touch is drawn here
          </h2>
          <p className="section__lede">
            The agent has no ambient authority. Each tool call is validated by the permission file,
            executed inside the workspace, and written to the audit log.
          </p>
        </div>

        <Reveal>
          <div className="diagram" role="img" aria-label="Client calls nour-code over SSE, nour-code calls Ollama on localhost, and all tool calls pass through permissions.yaml">
            <div className="diagram__node diagram__node--client">
              <span className="diagram__kind">client</span>
              <strong>curl · browser console · terminal chat</strong>
              <span className="diagram__hint">SSE stream, one JSON body</span>
            </div>

            <div className="diagram__link">
              <span>HTTP · localhost:8000</span>
            </div>

            <div className="diagram__node diagram__node--core">
              <span className="diagram__kind">nour-code</span>
              <strong>FastAPI · agent loop · SSE</strong>
              <span className="diagram__hint">15 iterations max · finish_reason on every turn</span>
            </div>

            <div className="diagram__fan">
              <div className="diagram__leaf">
                <span className="diagram__kind">tools</span>
                <strong>fs · shell · patch</strong>
                <span className="diagram__hint">7 validated tools</span>
              </div>
              <div className="diagram__leaf">
                <span className="diagram__kind">skills</span>
                <strong>Python files</strong>
                <span className="diagram__hint">hot reload, isolated failures</span>
              </div>
              <div className="diagram__leaf">
                <span className="diagram__kind">state</span>
                <strong>sessions · audit · metrics</strong>
                <span className="diagram__hint">JSONL + Prometheus</span>
              </div>
            </div>

            <div className="diagram__guard">
              <span className="diagram__guard-title">permissions.yaml</span>
              <span>every call passes here first — roots, shell allow/deny, per-tool switches</span>
            </div>

            <div className="diagram__link diagram__link--model">
              <span>HTTP · localhost:11434</span>
            </div>

            <div className="diagram__node diagram__node--model">
              <span className="diagram__kind">ollama</span>
              <strong>qwen3:8b</strong>
              <span className="diagram__hint">OpenAI-compatible /v1 for chat · native /api for residency</span>
            </div>
          </div>
        </Reveal>

        <div className="arch__notes">
          <Reveal delay={80}>
            <div className="note">
              <h3>Read the permission file in a minute</h3>
              <pre className="code">
                <code>{`allowed_roots:
  - ./                      # everything else is refused

filesystem:
  denied_globs: ["**/.env", "**/*.pem"]

shell:
  allowed_commands: [git, python, npm, pytest]
  denied_patterns: ["rm -rf", "(^|\\s)format(\\s|$)"]

tools:
  delete_file: false        # destructive: opt-in`}</code>
              </pre>
              <p>
                Windows-style and POSIX roots are both accepted; roots that do not exist on the
                current host are skipped with a warning instead of failing at boot.
              </p>
            </div>
          </Reveal>
          <Reveal delay={140}>
            <div className="note">
              <h3>Watch it work</h3>
              <ul className="ticks">
                <li>Tokens stream as they arrive, with the model's reasoning kept separate.</li>
                <li>Each tool call becomes a card: arguments, status, duration, full output.</li>
                <li>Every event is inspectable in the console pane and exportable as JSONL.</li>
              </ul>
              <p className="note__foot">
                Skills stay in <code>nour_code/skills/</code>; drop your own .py in <code>./skills/</code>.
              </p>
            </div>
          </Reveal>
        </div>
      </div>
    </section>
  );
}
