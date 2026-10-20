# nour-code landing page

The marketing page for [nour-code](../README.md): React 19 + TypeScript + Vite,
one hand-written stylesheet, no external requests (no web fonts, no CDN, no
telemetry — the page practises what the product promises).

## Run it

```bash
cd landing
npm install
npm run dev            # http://localhost:5173
```

The page reads live state from the agent, using **same-origin** URLs:

| Page request | Proxied to | Why |
|---|---|---|
| `GET /health` | `http://127.0.0.1:8000/health` | status panel: pulled vs resident, model, tools |
| `POST /ollama/warmup?keep_alive=30m` | same server | the “preload for 30 min” button |

Start the agent next to it (any terminal):

```bash
python -m nour_code --port 8000
```

If your API lives elsewhere: `NOUR_CODE_API=http://127.0.0.1:9000 npm run dev`.
Without a server the page still works — the panel shows the error state, says
what to run, and retries.

No Ollama handy? A tiny fake daemon is included for development:

```bash
python landing/scripts/mock-ollama.py     # 127.0.0.1:11999
NOUR_CODE_OLLAMA_BASE_URL=http://127.0.0.1:11999 python -m nour_code --port 8000
```

## Build

```bash
npm run build          # type-check + bundle into landing/dist
npm run preview        # serve the build on :4173
```

`dist/` is ignored by git. Deploy it anywhere static (Vercel/Netlify/nginx);
point the same two proxy rules at your API, or drop the status panel if the page
is hosted somewhere the API is not reachable.

## How the UX skill was applied

The skill's components were ported from its Tailwind snippets to this page's
design system, keeping the behaviour intact.

| Skill pattern | Where it lives | What it guarantees here |
|---|---|---|
| `useAsync` (timeout, abort, stale guard, retry) | `src/lib/useAsync.ts` → `StatusPanel` | a health probe cannot hang forever or overwrite a newer result |
| `useMutation` (double-submit guard) | `src/lib/useMutation.ts` → `AsyncButton`, `CopyCommand` | “preload” and “copy” cannot fire twice |
| `<AsyncButton>` | `src/components/AsyncButton.tsx` | busy state, `aria-busy`, no console spam on failure |
| slow indicator | `src/lib/useSlowIndicator.ts` | after 2.5 s the panel says it is taking longer, instead of looking frozen |
| delayed spinner | `src/components/DelayedSpinner.tsx` | no flicker for fast answers; used by the API filter |
| debounced input | `src/lib/useDebounced.ts` → `ApiExplorer` | filtering 15 endpoints does not run per keystroke |
| offline banner | `src/components/OfflineBanner.tsx` | explains what happens to the page while offline |
| error boundary per section | `src/components/ErrorBoundary.tsx` | one broken widget cannot blank the page |
| skeleton matching real layout | `.status__row` + `.skeleton--*` | no layout shift when data arrives |
| state checklist | `StatusPanel` | loading / empty / error / success all designed, each with one action |
| reduced motion + honest states | `TerminalDemo`, `Reveal`, `global.css` | the animation is a still image and content is visible without motion |

Patterns from the skill that are intentionally **not** used: `useDraft`,
optimistic list updates and table skeletons — this page has no forms or editable
lists to protect.

## Structure

```
landing/
├── index.html
├── vite.config.ts          # dev proxy + allowed hosts
├── public/favicon.svg
├── scripts/mock-ollama.py  # dev-only fake daemon
└── src/
    ├── App.tsx             # section order + error boundaries
    ├── components/         # Nav, Hero, StatusPanel, Features, …
    ├── data/content.ts     # copy, endpoints, FAQ (single source)
    ├── lib/                # useAsync, useMutation, useSlowIndicator, …
    └── styles/global.css   # tokens + layout + all component styles
```
