export function Footer() {
  return (
    <footer className="footer">
      <div className="container footer__inner">
        <div className="footer__brand">
          <span className="brand__word">
            nour<span className="brand__dim">-code</span>
          </span>
          <p>
            A local AI coding agent. Qwen3 on Ollama, FastAPI in front, permissions in between.
          </p>
        </div>

        <div className="footer__cols">
          <div>
            <h3>project</h3>
            <a href="https://github.com/Nour-yahyaoui/nour-code" target="_blank" rel="noreferrer noopener">
              Source
            </a>
            <a href="https://github.com/Nour-yahyaoui/nour-code#readme" target="_blank" rel="noreferrer noopener">
              README
            </a>
            <a href="https://github.com/Nour-yahyaoui/nour-code/issues" target="_blank" rel="noreferrer noopener">
              Issues
            </a>
          </div>
          <div>
            <h3>on this page</h3>
            <a href="#status">Live status</a>
            <a href="#start">Quick start</a>
            <a href="#api">API</a>
          </div>
          <div>
            <h3>elsewhere</h3>
            <a href="https://ollama.com" target="_blank" rel="noreferrer noopener">
              Ollama
            </a>
            <a href="https://qwenlm.github.io" target="_blank" rel="noreferrer noopener">
              Qwen
            </a>
            <a href="https://fastapi.tiangolo.com" target="_blank" rel="noreferrer noopener">
              FastAPI
            </a>
          </div>
        </div>
      </div>

      <div className="container footer__base">
        <span>MIT · runs on localhost</span>
        <span className="footer__mono">python -m nour_code --doctor</span>
      </div>
    </footer>
  );
}
