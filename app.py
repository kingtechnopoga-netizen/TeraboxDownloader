"""Flask app: Terabox file downloader."""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)

from terabox import TeraboxError, resolve, safe_filename, stream_download

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/info")
def api_info():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    cookie = (payload.get("cookie") or "").strip() or None

    if not url:
        return jsonify({"error": "Please provide a Terabox share URL."}), 400

    try:
        files = resolve(url, cookie=cookie)
    except TeraboxError as e:
        msg = str(e)
        # Provide a more helpful message for the common "need verify" case.
        if "need verify" in msg.lower():
            msg = (
                "Terabox requires authentication for this share. "
                "Please paste your Terabox login cookie (ndus=...) "
                "in the Advanced section below and try again."
            )
        return jsonify({"error": msg}), 400
    except Exception as e:  # noqa: BLE001
        app.logger.exception("Unexpected error resolving %s", url)
        return jsonify({"error": f"Unexpected error: {e}"}), 500

    return jsonify({"files": files})


# Headers we want to pass through from Terabox to the client when proxying.
_PASSTHROUGH_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "ETag",
    "Last-Modified",
)


@app.get("/api/proxy")
def api_proxy():
    """
    Stream a Terabox `dlink` through this server so the browser gets a clean
    download with the original filename and no CORS/hotlink issues.

    Query params:
        url:      the dlink returned by /api/info
        filename: optional desired filename
        cookie:   optional raw cookie string (forwarded to Terabox)
    """
    dlink = request.args.get("url", "").strip()
    if not dlink:
        return jsonify({"error": "Missing url parameter"}), 400

    filename = safe_filename(request.args.get("filename", ""), "download")
    cookie = request.args.get("cookie") or None
    range_header = request.headers.get("Range")

    try:
        upstream = stream_download(dlink, cookie=cookie, range_header=range_header)
    except TeraboxError as e:
        return jsonify({"error": str(e)}), 502

    if upstream.status_code >= 400:
        body = upstream.text[:300]
        upstream.close()
        return (
            jsonify(
                {
                    "error": f"Terabox refused download (HTTP {upstream.status_code}). "
                    "The link may have expired or require valid cookies.",
                    "details": body,
                }
            ),
            502,
        )

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    headers = {}
    for h in _PASSTHROUGH_HEADERS:
        v = upstream.headers.get(h)
        if v:
            headers[h] = v

    # Force a download with the requested filename (RFC 5987 for unicode).
    headers["Content-Disposition"] = (
        f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=headers,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
