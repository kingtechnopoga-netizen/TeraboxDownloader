# Terabox Downloader

A simple Flask web app that resolves Terabox share links and provides direct download links (with an optional server-side proxy stream so the browser can download with the original filename).

## Features

- Paste any `terabox.com` / `1024terabox.com` / `terasharelink.com` style share URL.
- See the file list with name, size and thumbnail.
- One-click **Download** (proxied through the server, sets the filename and Content-Disposition).
- One-click **Direct link** (the raw `dlink` straight from Terabox).
- Optional cookie field for shares that require a logged-in `ndus`.

## Quick start (local)

```bash
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000.

## Production

```bash
pip install -r requirements.txt
gunicorn -w 2 -k gthread --threads 8 -b 0.0.0.0:${PORT:-5000} app:app
```

`gthread` is recommended over `sync` workers because the proxy endpoint streams data, which is I/O-bound.

## Deploy

This repo includes configs for the most common platforms. Pick one:

### Render.com (recommended free tier)

1. Push the repo to GitHub.
2. Go to <https://dashboard.render.com/blueprints> and click **New Blueprint Instance**.
3. Select this repo. Render will read [`render.yaml`](./render.yaml) and provision the service.
4. Done. Health check is wired to `/healthz`.

### Railway / Heroku / Fly Procfile-style hosts

The included [`Procfile`](./Procfile) and [`runtime.txt`](./runtime.txt) are enough.
On Railway: **New Project → Deploy from GitHub** and it just works.

### Fly.io

```bash
fly launch --copy-config --no-deploy   # uses fly.toml + Dockerfile
fly deploy
```

### Docker (any host: VPS, Coolify, Dokku, etc.)

```bash
docker build -t terabox-downloader .
docker run -p 5000:5000 terabox-downloader
```

### Vercel (with caveats)

A [`vercel.json`](./vercel.json) is included, but **the `/api/proxy` streaming endpoint is not a great fit for Vercel** because serverless functions have strict timeouts (10s on Hobby) and request/response size limits. The `/` and `/api/info` endpoints work fine. If you deploy to Vercel, expect users to use the **Direct link** button rather than the proxied **Download** button.

```bash
npm i -g vercel
vercel
```

## How it works

1. The browser POSTs the share URL to `/api/info`.
2. `terabox.resolve()`:
   - extracts `surl` from the URL,
   - fetches the share page and scrapes `jsToken` / `dp-logid`,
   - calls `https://www.1024terabox.com/share/list` with the right params,
   - returns a normalized list of `{filename, size, download_link, thumbnail, ...}`.
3. The frontend renders cards with two download options:
   - **Download** → `/api/proxy?url=<dlink>&filename=...` (server streams the bytes).
   - **Direct link** → opens the raw Terabox URL in a new tab.

## Notes & limitations

- Terabox `dlink` URLs are **temporary and signed** — they expire and are only valid for a short window after `/api/info` is called.
- Some shares require an authenticated `ndus` cookie. If a download fails with HTTP 403, paste your cookie in the **Advanced** section.
- This project is for personal use. Respect Terabox's terms of service and the rights of content owners.

## Project layout

```
.
├── app.py              # Flask routes
├── terabox.py          # Terabox API client (resolve + stream)
├── requirements.txt
├── templates/
│   └── index.html      # UI (Tailwind via CDN)
└── README.md
```
