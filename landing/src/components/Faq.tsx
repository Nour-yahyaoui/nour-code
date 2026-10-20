import { FAQ } from "../data/content";
import { Reveal } from "./Reveal";

export function Faq() {
  return (
    <section className="section section--alt" id="faq" aria-labelledby="faq-title">
      <div className="container faq">
        <div className="faq__head">
          <p className="eyebrow">questions people actually ask</p>
          <h2 id="faq-title" className="section__title">
            The five-minute version
          </h2>
          <p className="section__lede">
            Most “it is not working” reports are one of these. The first answer is the one that
            started this page.
          </p>
        </div>

        <div className="faq__list">
          {FAQ.map((item, index) => (
            <Reveal key={item.q} delay={index * 40}>
              <details className="faq__item" open={index === 0}>
                <summary>
                  <span>{item.q}</span>
                  <span className="faq__chevron" aria-hidden="true" />
                </summary>
                <p>{item.a}</p>
              </details>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}
