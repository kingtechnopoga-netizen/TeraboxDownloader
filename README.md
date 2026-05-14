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
