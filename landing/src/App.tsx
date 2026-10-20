import { ApiExplorer } from "./components/ApiExplorer";
import { Architecture } from "./components/Architecture";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Faq } from "./components/Faq";
import { Features } from "./components/Features";
import { Footer } from "./components/Footer";
import { Hero } from "./components/Hero";
import { Nav } from "./components/Nav";
import { OfflineBanner } from "./components/OfflineBanner";
import { Security } from "./components/Security";
import { StatusPanel } from "./components/StatusPanel";
import { Steps } from "./components/Steps";

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

        <div className="container status-wrap" id="status">
          <ErrorBoundary label="Live status">
            <StatusPanel />
          </ErrorBoundary>
        </div>

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
                href="https://github.com/Nour-yahyaoui/nour-code"
                target="_blank"
                rel="noreferrer noopener"
              >
                Get nour-code
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
