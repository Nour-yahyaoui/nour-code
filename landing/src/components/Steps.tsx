import { STEPS } from "../data/content";
import { CopyCommand } from "./CopyCommand";
import { Reveal } from "./Reveal";

export function Steps() {
  return (
    <section className="section" id="start" aria-labelledby="start-title">
      <div className="container">
        <div className="section__head">
          <p className="eyebrow">three commands</p>
          <h2 id="start-title" className="section__title">
            From zero to a streamed turn
          </h2>
          <p className="section__lede">
            Works on Linux, macOS and Windows. If something is off, <code>--doctor</code> tells you
            which of the four pieces is missing before you start guessing.
          </p>
        </div>

        <ol className="steps">
          {STEPS.map((step, index) => (
            <li className="steps__cell" key={step.n}>
              <Reveal delay={index * 70}>
                <div className="step">
                  <div className="step__num">{step.n}</div>
                  <h3 className="step__title">{step.title}</h3>
                  <p className="step__body">{step.body}</p>
                  <CopyCommand value={step.command} />
                </div>
              </Reveal>
            </li>
          ))}
        </ol>

        <div className="start__extra">
          <Reveal>
            <div className="start__card">
              <h3>Prefer the terminal?</h3>
              <p>
                The agent is also a REPL: streaming tokens, tool results inline, one session for the
                whole conversation.
              </p>
              <CopyCommand value="python -m nour_code --chat" compact />
            </div>
          </Reveal>
          <Reveal delay={80}>
            <div className="start__card">
              <h3>Not sure why nothing answers?</h3>
              <p>One command checks Python, dependencies, the port, Ollama and the model itself.</p>
              <CopyCommand value="python -m nour_code --doctor" compact />
            </div>
          </Reveal>
        </div>
      </div>
    </section>
  );
}
