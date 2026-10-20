import { useEffect, useRef, useState } from "react";

type Line = {
  kind: "req" | "event" | "tool" | "result" | "token" | "done";
  label: string;
  text: string;
};

const SCRIPT: Line[] = [
  { kind: "req", label: "POST", text: "/chat  { message: “add rate limiting to the API” }" },
  { kind: "event", label: "status", text: "phase=model_call  tools=15  model=qwen3:8b" },
  { kind: "event", label: "thinking", text: "“the chat route has no limiter; a token bucket fits”" },
  { kind: "tool", label: "tool_call", text: 'read_file  {"path": "nour_code/main.py"}' },
  { kind: "result", label: "tool_result", text: "ok  4.1 ms  318 lines" },
  { kind: "tool", label: "tool_call", text: 'apply_patch  {"path": "nour_code/main.py"}' },
  { kind: "result", label: "tool_result", text: "ok  9.7 ms  patched 2 hunks" },
  { kind: "token", label: "token", text: "“Added a token bucket to the chat route — 429 with Retry-After.”" },
  { kind: "result", label: "file_change", text: "modified  nour_code/main.py  +34 −2" },
  { kind: "event", label: "usage", text: "prompt=812  completion=96  iteration=2" },
  { kind: "done", label: "done", text: "finish_reason=stop  1.9 s  2 tool calls" },
];

const KIND_CLASS: Record<Line["kind"], string> = {
  req: "line--req",
  event: "line--event",
  tool: "line--tool",
  result: "line--result",
  token: "line--token",
  done: "line--done",
};

const reducedMotion = () =>
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * A looping SSE transcript. It is a still image for anyone who asked for
 * reduced motion, and it pauses when the tab is hidden.
 */
export function TerminalDemo() {
  const [visible, setVisible] = useState(() => (reducedMotion() ? SCRIPT.length : 0));
  const [cycle, setCycle] = useState(0);
  const [hidden, setHidden] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    const onVisibility = () => setHidden(document.hidden);
    onVisibility();
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  useEffect(() => {
    if (reducedMotion() || hidden) return;
    if (visible >= SCRIPT.length) {
      timer.current = window.setTimeout(() => {
        setVisible(0);
        setCycle((c) => c + 1);
      }, 3400);
      return () => window.clearTimeout(timer.current);
    }
    timer.current = window.setTimeout(() => setVisible((v) => v + 1), visible === 0 ? 500 : 520);
    return () => window.clearTimeout(timer.current);
  }, [visible, hidden]);

  return (
    <figure className="terminal" aria-label="Example of a streamed agent turn">
      <div className="terminal__bar">
        <span className="terminal__lights" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="terminal__title">POST /chat · text/event-stream</span>
        <span className="terminal__badge">SSE</span>
      </div>
      <div className="terminal__body" key={cycle}>
        {SCRIPT.slice(0, visible).map((line, index) => (
          <p key={`${cycle}-${index}`} className={`line ${KIND_CLASS[line.kind]}`}>
            <span className="line__label">{line.label}</span>
            <span className="line__text">{line.text}</span>
          </p>
        ))}
        {visible < SCRIPT.length ? <span className="caret" aria-hidden="true" /> : null}
      </div>
      <figcaption className="terminal__foot">
        Real event names from <code>POST /chat</code>: token, tool_call, tool_result, usage, done.
      </figcaption>
    </figure>
  );
}
