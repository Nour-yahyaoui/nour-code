const LINKS = [
  { href: "#features", label: "Features" },
  { href: "#architecture", label: "Architecture" },
  { href: "#start", label: "Quick start" },
  { href: "#api", label: "API" },
  { href: "#security", label: "Security" },
  { href: "#faq", label: "FAQ" },
];

export function Nav() {
  return (
    <header className="nav">
      <div className="container nav__inner">
        <a className="brand" href="#top" aria-label="nour-code, back to top">
          <svg className="brand__mark" viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="8" fill="currentColor" opacity="0.12" />
            <path d="M9 23V9h3.3l6.4 9V9H22v14h-3.3l-6.4-9v9H9Z" fill="currentColor" />
          </svg>
          <span className="brand__word">
            nour<span className="brand__dim">-code</span>
          </span>
        </a>

        <nav className="nav__links" aria-label="Sections">
          {LINKS.map((link) => (
            <a key={link.href} href={link.href}>
              {link.label}
            </a>
          ))}
        </nav>

        <div className="nav__actions">
          <a className="nav__version" href="#status" title="Live status of the local instance">
            v1.2
          </a>
          <a
            className="btn btn--ghost btn--sm"
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
        </div>
      </div>
    </header>
  );
}
