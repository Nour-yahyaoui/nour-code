import { GUARANTEES } from "../data/content";
import { Reveal } from "./Reveal";

export function Security() {
  return (
    <section className="section" id="security" aria-labelledby="security-title">
      <div className="container security">
        <div className="security__copy">
          <p className="eyebrow">security model</p>
          <h2 id="security-title" className="section__title">
            Nothing leaves except the traffic you allow
          </h2>
          <p className="section__lede">
            The threat model is simple: the model is untrusted input. It can propose any tool call it
            likes; it cannot grant itself a path, a command or a permission.
          </p>
          <p className="security__note">
            <strong>Honest limits.</strong> nour-code is not a sandbox. A command your allowlist
            permits runs with your user's rights — narrow the allowlist if that matters, and run it
            in a container when you want a real boundary.
          </p>
        </div>

        <div className="security__grid">
          {GUARANTEES.map((item, index) => (
            <Reveal key={item.title} delay={index * 60}>
              <div className="guarantee">
                <h3>{item.title}</h3>
                <p>{item.body}</p>
              </div>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}
