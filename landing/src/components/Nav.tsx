import { NavMenu } from "./NavMenu";

export function Nav() {
  return (
    <header className="nav">
      <div className="container nav__inner">
        <a className="brand" href="#top" aria-label="nour-code, back to top">
          <img className="brand__mark" src="/icon.svg" alt="" width={28} height={28} />
          <span className="brand__word">
            nour<span className="brand__dim">-code</span>
          </span>
        </a>

        <div className="nav__actions">
          <a
            className="nav__version"
            href="https://github.com/Nour-yahyaoui/nour-code/releases/latest"
            target="_blank"
            rel="noreferrer noopener"
            title="Latest release"
          >
            v1.4
          </a>
          <a
            className="btn btn--ghost btn--sm nav__source"
            href="https://github.com/Nour-yahyaoui/nour-code"
            target="_blank"
            rel="noreferrer noopener"
          >
            Source
            <svg viewBox="0 0 16 16" className="btn__icon" aria-hidden="true">
              <path
                d="M6 3h7v7h-2V6.4l-6.3 6.3-1.4-1.4L9.6 5H6V3Z"
                fill="currentColor"
              />
            </svg>
          </a>

          <NavMenu />
        </div>
      </div>
    </header>
  );
}
