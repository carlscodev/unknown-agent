# Unknown Agent Website

Vue 3 + Vite marketing site, separate from the authenticated control panel in `web/frontend`.

## Local development

```bash
cd website
npm install
npm run dev
```

## Deploy to Vercel

Import the repository in Vercel and set **Root Directory** to `website`. The included `vercel.json` configures the Vite build, SPA rewrites, immutable asset caching, and security headers.

Production build:

```bash
npm ci
npm run build
```

Download buttons point to the repository's stable `/releases/latest` URL, so no website change is needed when a new release is published.
