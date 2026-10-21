import { useEffect, useId, useRef, useState } from "react";

/** The page's sections, in reading order. The hint is the section's eyebrow. */
const LINKS = [
  { href: "#console", label: "Console", hint: "what you actually see" },
  { href: "#features", label: "Features", hint: "what is inside" },
  { href: "#architecture", label: "Architecture", hint: "one process, one hop" },
  { href: "#start", label: "Quick start", hint: "three commands" },
  { href: "#api", label: "API", hint: "the whole API" },
  { href: "#security", label: "Security", hint: "security model" },
  { href: "#faq", label: "FAQ", hint: "questions people actually ask" },
] as const;

/** Tracks which section is on screen, so the menu can mark it. */
function useActiveSection() {
  const [active, setActive] = useState("");

  useEffect(() => {
    const sections = LINKS.map((link) => document.getElementById(link.href.slice(1))).filter(
      (element): element is HTMLElement => element !== null,
    );
    if (sections.length === 0 || typeof IntersectionObserver === "undefined") return;

    // Only sections crossing the band just under the header count; of those,
    // the highest one wins. A Map keeps the state between callbacks, which
    // only report the entries that changed.
    const onScreen = new Map<string, number>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) onScreen.set(entry.target.id, entry.boundingClientRect.top);
          else onScreen.delete(entry.target.id);
        }
        const topmost = [...onScreen.entries()].sort((a, b) => a[1] - b[1])[0];
        if (topmost) setActive(`#${topmost[0]}`);
      },
      { rootMargin: "-84px 0px -55% 0px", threshold: 0 },
    );

    sections.forEach((section) => observer.observe(section));
    return () => observer.disconnect();
  }, []);

  return active;
}

/**
 * The header's section links, behind one button.
 *
 * Seven inline links plus two actions made the header a wall of small text, so
 * this is a disclosure menu: a plain button that shows a panel of anchor
 * links. It is deliberately not `role="menu"` — this is navigation, not an
 * application menu, so Tab, Enter and the browser's own link handling keep
 * working.
 */
export function NavMenu() {
  const [open, setOpen] = useState(false);
  const active = useActiveSection();
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      buttonRef.current?.focus();
    };

    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div
      className="menu"
      ref={rootRef}
      onBlur={(event) => {
        // Tab out of the menu and it closes behind you. A null relatedTarget
        // means focus went nowhere in particular (a click on the panel's own
        // padding drops focus to <body>), and closing there would make the
        // menu feel like it was swatting at your pointer.
        const next = event.relatedTarget as Node | null;
        if (next && !event.currentTarget.contains(next)) setOpen(false);
      }}
    >
      <button
        ref={buttonRef}
        type="button"
        className="menu__button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="menu__bars" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span>Menu</span>
      </button>

      <div id={panelId} className={`menu__panel${open ? " is-open" : ""}`}>
        {/* a real landmark, so screen readers can still jump to the sections */}
        <nav aria-label="Sections">
          <p className="menu__eyebrow">jump to a section</p>

          <ul className="menu__list">
            {LINKS.map((link, index) => {
              const current = active === link.href;
              return (
                <li key={link.href}>
                  <a
                    href={link.href}
                    aria-current={current ? "location" : undefined}
                    onClick={() => setOpen(false)}
                  >
                    <span className="menu__index" aria-hidden="true">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <span className="menu__item">{link.label}</span>
                    <span className="menu__hint">{link.hint}</span>
                    <span className="menu__dot" aria-hidden="true" />
                  </a>
                </li>
              );
            })}
          </ul>
        </nav>

        {/* The header's own links move in here on narrow screens. */}
        <div className="menu__foot">
          <a
            href="https://github.com/Nour-yahyaoui/nour-code"
            target="_blank"
            rel="noreferrer noopener"
            onClick={() => setOpen(false)}
          >
            Source
          </a>
          <a
            href="https://github.com/Nour-yahyaoui/nour-code/releases/latest"
            target="_blank"
            rel="noreferrer noopener"
            onClick={() => setOpen(false)}
          >
            Latest release
          </a>
        </div>
      </div>
    </div>
  );
}
