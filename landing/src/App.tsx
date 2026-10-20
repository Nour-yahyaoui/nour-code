import { ApiExplorer } from "./components/ApiExplorer";
import { Architecture } from "./components/Architecture";
import { ConsolePreview } from "./components/ConsolePreview";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Faq } from "./components/Faq";
import { Features } from "./components/Features";
import { Footer } from "./components/Footer";
import { Hero } from "./components/Hero";
import { Nav } from "./components/Nav";
import { OfflineBanner } from "./components/OfflineBanner";
import { Security } from "./components/Security";
import { Steps } from "./components/Steps";
import { Reveal } from "./components/Reveal";

export default function App() {
  return (
    <>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <OfflineBanner />
      <Nav />

      <main id="main">
        {/* each section is isolated: one broken widget cannot blank the page */}
        <ErrorBoundary label="Hero">
          <Hero />
        </ErrorBoundary>

        <section className="section section--console" id="console" aria-labelledby="console-title">
          <div className="container">
            <div className="section__head">
              <p className="eyebrow">what you actually see</p>
              <h2 id="console-title" className="section__title">
                A transcript, tool cards, and your files
              </h2>
              <p className="section__lede">
                Tokens stream in as the model writes them. Every tool call becomes a card you can
                open — arguments, status, duration, full output. The workspace on the right is a
                read-only view of the files it just touched. Both sidebars collapse.
              </p>
            </div>

            <ErrorBoundary label="Console preview">
              <Reveal>
                <ConsolePreview />
              </Reveal>
            </ErrorBoundary>
          </div>
        </section>

        <ErrorBoundary label="Features">
          <Features />
        </ErrorBoundary>

        <ErrorBoundary label="Architecture">
          <Architecture />
        </ErrorBoundary>

        <ErrorBoundary label="Quick start">
          <Steps />
        </ErrorBoundary>

        <ErrorBoundary label="API reference">
          <ApiExplorer />
        </ErrorBoundary>

        <ErrorBoundary label="Security">
          <Security />
        </ErrorBoundary>

        <ErrorBoundary label="FAQ">
          <Faq />
        </ErrorBoundary>

        <section className="cta" aria-labelledby="cta-title">
          <div className="container cta__inner">
            <div>
              <h2 id="cta-title">Bring your own machine.</h2>
              <p>
                Clone it, pull the model, run the doctor. If a check fails it tells you what to fix —
                that is the whole onboarding.
              </p>
            </div>
            <div className="cta__actions">
              <a
                className="btn btn--primary"
                href="https://github.com/Nour-yahyaoui/nour-code/releases/latest"
                target="_blank"
                rel="noreferrer noopener"
              >
                Download the release
              </a>
              <a className="btn btn--quiet" href="#start">
                See the three commands
              </a>
            </div>
          </div>
        </section>
      </main>

      <Footer />
    </>
  );
}
