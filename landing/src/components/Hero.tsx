import { CopyCommand } from "./CopyCommand";
import { TerminalDemo } from "./TerminalDemo";

const SPECS = [
  { label: "model", value: "qwen3:8b" },
  { label: "server", value: "FastAPI + SSE" },
  { label: "tools", value: "7 built-in" },
  { label: "license", value: "MIT" },
];

export function Hero() {
  return (
    <section className="hero" id="top">
      <div className="hero__glow" aria-hidden="true" />
      <div className="container hero__grid">
        <div className="hero__copy">
          <p className="eyebrow">
            <span className="dot dot--ok" aria-hidden="true" />
            local-first coding agent
          </p>
          <h1 className="hero__title">
            A coding agent that stays
            <span className="hero__title-accent"> on your machine.</span>
          </h1>
          <p className="hero__lede">
            nour-code puts Qwen3 8B, running on Ollama, behind a FastAPI server with seven audited
            tools: read, write, search, patch, shell. Your disk, your model, your rules — and a
            permission file you can actually read.
          </p>

          <div className="hero__cta">
            <CopyCommand value="python -m nour_code --port 8000" compact />
            <a
              className="btn btn--ghost"
              href="https://github.com/Nour-yahyaoui/nour-code"
              target="_blank"
              rel="noreferrer noopener"
            >
              Read the source
            </a>
          </div>

          <p className="hero__fineprint">
            No account. No API key. No telemetry. One localhost hop to your own daemon.
          </p>

          <dl className="hero__specs">
            {SPECS.map((spec) => (
              <div key={spec.label}>
                <dt>{spec.label}</dt>
                <dd>{spec.value}</dd>
              </div>
            ))}
          </dl>
        </div>

        <div className="hero__demo">
          <TerminalDemo />
        </div>
      </div>
    </section>
  );
}
