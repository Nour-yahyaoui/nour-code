# nour-code landing page

The public page for [nour-code](../README.md) — <https://nour-code-agent.vercel.app/>.
React 19 + TypeScript + Vite, one hand-written stylesheet, no external requests
(no web fonts, no CDN, no telemetry — the page practises what the product
promises).

It is **fully static**. There is no proxy, no API target, and no call to
`localhost`: most visitors will never run the agent, so a "live status of your
instance" panel would only ever tell them *not running*. What they get instead
is a written tour plus a labelled recreation of the console
(`src/components/ConsolePreview.tsx`) — real markup, no fabricated metrics.

## Run it

```bash
cd landing
npm install
npm run dev            # http://localhost:5173
npm run build          # type-check + bundle into landing/dist
npm run preview        # serve the build on :4173
```

`dist/` is ignored by git. Deploy it anywhere static: Vercel, Netlify,
GitHub Pages, nginx. The only outbound links are to GitHub (and one to
<https://ollama.com>).

## Icons

`public/` holds the mark as `icon.svg` (source of truth), `icon-512.png`,
`icon-64.png`, `favicon-32.png`, `favicon.ico` (16/32/48) and
`apple-touch-icon.png` (180) — plus `og.png`, the 1200×630 social card. They are
rendered from the same vectors by a Pillow script, because this sandbox has no
working SVG rasteriser:

```bash
python landing/scripts/make-icon.py    # needs Pillow; writes every size + og.png
```

`index.html` wires them up (favicon, apple-touch, `og:image`). `vite.config.ts`
only sets host/port for the preview sandbox.

## How the UX skill was applied

The skill's components were ported from its Tailwind snippets to this page's
design system, keeping the behaviour intact.

| Skill pattern | Where it lives | What it guarantees here |
|---|---|---|
| `useMutation` (double-submit guard) | `src/lib/useMutation.ts` → `AsyncButton`, `CopyCommand` | "copy" cannot fire twice |
| `<AsyncButton>` | `src/components/AsyncButton.tsx` | busy state, `aria-busy`, no console spam on failure |
| delayed spinner | `src/components/DelayedSpinner.tsx` | no flicker for fast answers; used by the API filter |
| debounced input | `src/lib/useDebounced.ts` → `ApiExplorer` | filtering 15 endpoints does not run per keystroke |
| offline banner | `src/components/OfflineBanner.tsx` + `useOnline.ts` | explains that the page itself is static, so almost nothing changes offline |
| error boundary per section | `src/components/ErrorBoundary.tsx` | one broken widget cannot blank the page |
| reduced motion + honest states | `TerminalDemo`, `Reveal`, `global.css` | the animation is a still image and content is visible without motion |

Patterns from the skill that are intentionally **not** used: `useAsync` timeouts,
retry and stale guards (nothing is fetched any more), `useDraft`, optimistic
list updates and table skeletons — this page has no forms, no async data and no
editable lists to protect.

## Structure

```
landing/
├── index.html
├── vite.config.ts          # host/port only — no proxy
├── public/                 # icon.svg + generated PNG/ICO icons
├── scripts/make-icon.py    # renders the icon set with Pillow
├── scripts/mock-ollama.py  # dev-only fake daemon (for the agent, not this page)
└── src/
    ├── App.tsx             # section order + error boundaries
    ├── components/         # Nav, Hero, ConsolePreview, Features, …
    ├── data/content.ts     # copy, endpoints, FAQ (single source)
    ├── lib/                # useMutation, useDebounced, useOnline
    └── styles/global.css   # tokens + layout + all component styles
```
