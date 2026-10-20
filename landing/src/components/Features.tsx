import { FEATURES } from "../data/content";
import { Reveal } from "./Reveal";

export function Features() {
  return (
    <section className="section" id="features" aria-labelledby="features-title">
      <div className="container">
        <div className="section__head">
          <p className="eyebrow">what is inside</p>
          <h2 id="features-title" className="section__title">
            A small surface you can audit
          </h2>
          <p className="section__lede">
            An agent is only as trustworthy as the boundary around it. Everything here is one process,
            one YAML file and one JSONL log.
          </p>
        </div>

        <div className="features">
          {FEATURES.map((feature, index) => (
            <Reveal key={feature.id} delay={index * 60}>
              <article className="feature">
                <div className="feature__top">
                  <span className="feature__index">{String(index + 1).padStart(2, "0")}</span>
                  <code className="feature__meta">{feature.meta}</code>
                </div>
                <h3 className="feature__title">{feature.title}</h3>
                <p className="feature__body">{feature.body}</p>
              </article>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}
