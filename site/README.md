# Ada marketing site

One static page, no build step. Palette and type follow the product's own
tokens (`frontend/src/styles/tokens.css`). Layout follows a conventional
developer-tools landing page: nav, hero with the live demo, a "built on" row,
a measured pull quote, four feature blocks with terminal-style mockups, the
approval strip, engineering notes, download, closing call to action.

Every number and claim on the page is one the repository can back:
the ERC/DRC/parity quote is the measured clean state in CLAUDE.md, the
round-1/round-2 mockup is the `test_verify.py` case, the receipt lines are the
verifier names in `engine/silkscreen/verify/`. Change the page when the
measurement changes, not before.

Placeholders to replace before it goes live:
- `hello@ada.engineering` in the two "Book a demo" links and the footer.
- The download link points at the v0.1.0 GitHub release's `.dmg` (unsigned).
  Update it when a notarized build exists (`docs/production-plan.md` §3).

Preview: `open site/index.html`.

Status (2026-09-16): **not public.** No `infra/` directory exists, neither
GitHub remote has Pages enabled, and nothing has ever deployed this page.

Deploy: `.github/workflows/site.yml` publishes this directory to GitHub Pages
on every push to `main` that touches `site/`. It needs Pages enabled once in
the repository settings with "GitHub Actions" as the source; until then the
job fails at the deploy step naming that setting. The S3 + CloudFront path
below is the alternative once a bucket and distribution exist:

    aws s3 sync site/ s3://<bucket>/ --delete --exclude README.md
    aws cloudfront create-invalidation --distribution-id <id> --paths "/*"
