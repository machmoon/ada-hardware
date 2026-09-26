# Ada marketing site

One page, built with Vite + React + TypeScript + Tailwind CSS v4 + motion into a
static bundle in `dist/`. No server, no SSR, no client routing: `index.html`
plus hashed assets, deployable to S3 + CloudFront or GitHub Pages as they are.

Layout, type scale, palette and spacing are measured from manus.im's shipped
CSS, band for band (header, announcement strip, hero prompt, dark footer), and
the middle bands follow manus's own feature page
(`https://manus.im/features/webapp`). The animated parts are real components
from Aceternity UI and Skiper UI, restyled with manus's tokens (see
"Third-party UI code" below). The words are Ada's own.

## Develop, build, preview

Node 22.12 or newer (Vite 8's engines range; CI pins 22).

    cd site
    npm ci            # one node_modules, in site/ only
    npm run dev       # http://localhost:5173
    npm run build     # tsc --noEmit, then vite build into site/dist/
    npm run preview   # serves dist/ at http://localhost:4173

The build uses a relative base (`base: "./"` in `vite.config.ts`), so one
bundle serves both hosts: GitHub Pages under `/ada-hardware/` and CloudFront at
`/`. Check a subpath with `npx vite preview --base /ada-hardware/`. Opening
`dist/index.html` from `file://` does not work: browsers refuse module scripts
from a file origin. Use `npm run preview`.

## Where things are

    index.html                 entry: title, meta, og tags, the theme script (runs before first paint)
    vite.config.ts             base "./"; copies assets/board.png to dist/og/board.png for og:image
    components.json            shadcn provenance: the registries the components came from
    assets/                    the images and the mark (unchanged; gen_app_icon.py writes icon.png here)
    src/content.ts             every string, number and link on the page, each with its backing file
    src/styles.css             manus's tokens (light and dark) as CSS variables, Tailwind v4 theme
    src/sections/              the bands, in page order (App.tsx)
    src/components/aceternity/ vendored Aceternity UI components (not MIT, see below)
    src/components/skiper/     Skiper-derived mechanisms, re-implemented (not MIT, see below)
    src/components/Mark.tsx    the differential-pair mark, paths from assets/mark.svg

`assets/schematic.png` is not imported by the page, so the build does not ship
it. `assets/mark.svg` is the mark's source; the page inlines its paths.

## Every number is backed

Every number and claim on the page is one the repository can back, and
`src/content.ts` names the file beside each one:

- "6 of 6 test boards pass KiCad's own checks": `docs/measurements/board-eval-2026-09-16.json`
  (all six cases: 0 ERC errors, 0 DRC violations, 0 unconnected, 0 parity
  issues). These are fixed circuits answered by a scripted model
  (`scripts/board_eval.py`), which is why the page says "test boards".
- "a fixed 0.25 mm gap": `DIFF_PAIR_GAP_NM` in `engine/silkscreen/diffpair.py`,
  pinned by `engine/tests/test_diffpair.py`.
- The receipt's ERC, DRC and parity rows: the measured clean state on KiCad
  10.0.6 in CLAUDE.md, "Verifying against real KiCad".
- The round 1 / round 2 log: `engine/tests/test_verify.py` and `engine/tests/test_harness.py`.
- The hero's example prompts: `EXAMPLE_PROMPTS` in `app/src/pages/kaleo/components/PromptBar.tsx`.
- The command the hero hands over, `silkscreen '<intent>'`: the generate CLI
  (`engine/silkscreen/cli.py`), reached through `onboard.dispatch`.

Held back until someone names the run they came from: the old page's SPICE
("Vout 3.30 V, margin +0.12 V") and CASE ("13 of 13 clauses, min wall +0.4 mm")
receipt rows and its per-check timings. Nothing in the repo backs those values,
so the receipt shows three rows and "Passed 3". The three review findings were
captioned "A real review of an ESP32 dev board, Sep 15, 2026" on the old page,
but no file in the repo carries them, so the page now captions them "An example
of a review"; put the date back only with the run that produced them.

Two sentences carried over from the old page were wrong and are corrected: the
case is not checked by ERC and DRC (those run on the schematic and the board;
the case is gated by the enclosure kernel's clauses,
`engine/silkscreen/enclosure/kernel.py`), and it is not true that "nothing that
costs money runs until you approve it" (`service/steps.py` starts the case and
the BOM on model calls as soon as `place` succeeds). The KiCad tile now says
what the code does: every stage opens in the editor, and the fab order is
prepared, never placed.

Change the page when the measurement changes, not before. Add no metric,
customer, logo or testimonial the repo cannot back.

## Motion

`src/main.tsx` wraps the page in `<MotionConfig reducedMotion="user">`, which
turns motion's transform animations off when the system asks for reduced
motion. What that switch cannot reach checks `useReducedMotion()` itself: the
board reveal shows its flat final frame, the differential pair is drawn in
full, the terminal prints its commands at once with a steady cursor, the
prompt's placeholder does not rotate, and submitting clears the field without
the particle vanish. CSS transitions carry `motion-safe:` or
`motion-reduce:transition-none`, and smooth scrolling is only on under
`prefers-reduced-motion: no-preference`.

## Theme

Light and dark follow the system. The footer button cycles System, Light and
Dark and remembers a choice in `localStorage["ada-theme"]`; the script in
`index.html` applies it before the first paint.

## Without JavaScript

React draws every band, so without JavaScript the page shows only a
`<noscript>` fallback in `index.html`: the headline, one sentence and links to
GitHub, the install guide and a demo, in the light theme. Its words are copied
from `src/content.ts` by hand; change both together. Opening `dist/index.html`
from `file://` shows nothing at all (see above).

## Deploy

**GitHub Pages.** `.github/workflows/site.yml` runs on every push to `main`
that touches `site/`: `npm ci`, `npm run build`, then it uploads `site/dist`
and publishes it with `actions/deploy-pages`. Pull requests that touch `site/`
run the build only, so a broken site fails on the PR. Pages must be enabled
once in the repository settings with "GitHub Actions" as the source; until
then the deploy step fails naming that setting.

**AWS S3 + CloudFront.** The previous static page has been live there since
2026-09-21, at https://d1l2uamq911fx1.cloudfront.net (private bucket
`ada-site-361147857635` behind distribution `E34D7QV323WAY7` with origin access
control, us-east-1; no custom domain yet). Hashed files are cached for a year;
`index.html` and the unhashed `og/board.png` are not:

    cd site && npm ci && npm run build
    aws s3 sync dist/ s3://ada-site-361147857635/ --delete \
      --exclude index.html --exclude "og/*" \
      --cache-control "public,max-age=31536000,immutable"
    aws s3 sync dist/ s3://ada-site-361147857635/ \
      --exclude "*" --include index.html --include "og/*" \
      --cache-control "no-cache"
    aws cloudfront create-invalidation --distribution-id E34D7QV323WAY7 --paths "/*"

The first sync's `--delete` removes the old page's `styles.css`, `stars.js`
and unhashed `assets/*` from the bucket; the files it excludes are uploaded
by the second sync. `og:image` and `og:url` in `index.html` point at the
CloudFront host because Open Graph needs absolute URLs; change them when a
custom domain lands. Two consequences, both deliberate: `og/board.png` exists
only once this bundle has been synced to S3 (until then the CloudFront URL
answers 403 and link previews show no image), and the GitHub Pages copy
previews with the CloudFront image and URL too, since one bundle serves both
hosts.

## Third-party UI code

The two component directories below are **not** covered by this repository's
MIT licence. Each file starts with a header naming its source, the terms it is
used under and what was changed for Ada.

| Directory | What | Terms |
|---|---|---|
| `src/components/aceternity/` | Aceternity UI free components, from the registry at `https://ui.aceternity.com/registry/<name>.json` (fetched 2026-09-25): `resizable-navbar`, `placeholders-and-vanish-input`, `container-scroll-animation`, `bento-grid`, `tabs`, `terminal`. Modified; each file lists its changes. | The Aceternity License, https://ui.aceternity.com/licence: use in end products, including commercial websites, is permitted; redistributing the components themselves as a template or component library is not. No attribution is required; the footer credits Aceternity UI anyway. |
| `src/components/skiper/` | Two mechanisms from Skiper UI free components, re-implemented on Ada's tokens and geometry (no Skiper file ships verbatim): `PairTrace.tsx`, a stroke drawn by scroll progress, from skiper19 (`https://skiper-ui.com/registry/skiper19.json`); `InkLink.tsx`, a hover underline, from skiper40 `Link001` (`https://skiper-ui.com/registry/skiper40.json`). | Skiper UI's terms, https://skiper-ui.com/docs/quick-start: "Free to use and modify in both personal and commercial projects. Attribution to Skiper UI is required when using the free version." The footer's bottom row carries that attribution. For commercial use Skiper asks that components be integrated into your own design system rather than used as-is, which is why only the mechanisms are taken. |

Re-pulling a component with `npx shadcn@latest add @aceternity/<name>`
(`components.json` names the registries) overwrites the Ada changes; read the
file header first.

Packages the bundle ships: React and React DOM (MIT), motion (MIT),
lucide-react (ISC), clsx and tailwind-merge (MIT), and Libre Baskerville (SIL
OFL 1.1, self-hosted through `@fontsource-variable/libre-baskerville`, latin
and latin-ext only), manus.im's serif. Tailwind CSS (MIT) runs at build time.
The sans is the system UI stack, as on manus.im, so no sans webfont is
downloaded.
