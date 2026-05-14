"""
Terabox share-link resolver.

Given a Terabox share URL (terabox.com / 1024terabox.com / terasharelink.com / teraboxapp.com / etc.)
this module resolves the share, scrapes the required tokens, calls Terabox's
internal `share/list` API and returns a normalized list of files including
direct download links (`dlink`).

The `dlink` returned by Terabox is a temporary, signed download URL. Some
shares require a logged-in `ndus` cookie to actually fetch the bytes; the
caller can pass `cookie` (raw "ndus=..." string) to support those.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger(__name__)

# A real-looking desktop browser UA. Terabox blocks obviously-automated UAs.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Hosts Terabox uses; we try them in order when normalizing/redirecting.
TERABOX_API_HOST = "https://www.1024terabox.com"

DEFAULT_TIMEOUT = 20


class TeraboxError(Exception):
    """Raised when Terabox cannot be resolved."""


def _build_session(cookie: Optional[str] = None) -> requests.Session:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    if cookie:
        sess.headers["Cookie"] = cookie
    return sess


def _find_between(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i == -1:
        return ""
    i += len(start)
    j = text.find(end, i)
    if j == -1:
        return ""
    return text[i:j]


def _extract_surl(url: str) -> str:
    """Extract the `surl` parameter from any Terabox-style share URL."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "surl" in qs and qs["surl"]:
        return qs["surl"][0]
    if "/s/" in parsed.path:
        tail = parsed.path.split("/s/", 1)[1]
        tail = tail.split("/")[0].split("?")[0]
        # Terabox prefixes shortcodes with "1" in the path form.
        if tail.startswith("1"):
            tail = tail[1:]
        return tail
    raise TeraboxError("Could not find share id (surl) in the link.")


def _resolve_share_page(
    sess: requests.Session, url: str
) -> Tuple[str, str]:
    """Follow redirects to the canonical share page; return (final_url, html)."""
    try:
        resp = sess.get(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        raise TeraboxError(f"Network error fetching share page: {e}") from e
    if resp.status_code >= 400:
        raise TeraboxError(
            f"Share page returned HTTP {resp.status_code}. The link may be invalid or removed."
        )
    return resp.url, resp.text


def _extract_tokens(html: str) -> Dict[str, str]:
    js_token = _find_between(html, "fn%28%22", "%22%29")
    logid = _find_between(html, "dp-logid=", "&")
    bdstoken = _find_between(html, 'bdstoken":"', '"')
    return {"jsToken": js_token, "logid": logid, "bdstoken": bdstoken}


def _human_size(n: int) -> str:
    if n is None:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def _format_file(item: Dict[str, Any]) -> Dict[str, Any]:
    size_bytes = int(item.get("size") or 0)
    thumbs = item.get("thumbs") or {}
    return {
        "filename": item.get("server_filename") or item.get("filename") or "Unknown",
        "size": _human_size(size_bytes),
        "size_bytes": size_bytes,
        "is_directory": str(item.get("isdir")) == "1",
        "fs_id": str(item.get("fs_id") or ""),
        "path": item.get("path") or "",
        "download_link": item.get("dlink") or "",
        "thumbnail": (
            thumbs.get("url3")
            or thumbs.get("url2")
            or thumbs.get("url1")
            or ""
        ),
    }


def resolve(url: str, cookie: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Resolve a Terabox share URL into a list of file metadata dicts.

    Args:
        url: The full Terabox share URL.
        cookie: Optional raw cookie string (e.g. "ndus=...") to authenticate
            the request. Some shares require this.

    Returns:
        A list of file dicts with keys:
            filename, size, size_bytes, is_directory, fs_id, path,
            download_link, thumbnail.

    Raises:
        TeraboxError: if the URL is invalid, tokens cannot be extracted, or
            the Terabox API returns a non-zero errno.
    """
    if not url or not isinstance(url, str):
        raise TeraboxError("A Terabox share URL is required.")

    sess = _build_session(cookie)
    final_url, html = _resolve_share_page(sess, url.strip())

    surl = ""
    # Prefer surl from final URL query; fallback to original.
    try:
        surl = _extract_surl(final_url)
    except TeraboxError:
        pass
    if not surl:
        surl = _extract_surl(url)

    tokens = _extract_tokens(html)
    if not tokens["jsToken"] or not tokens["logid"]:
        raise TeraboxError(
            "Could not extract auth tokens from the share page. "
            "The link may be private, expired, or require login cookies."
        )

    params = {
        "app_id": "250528",
        "web": "1",
        "channel": "dubox",
        "clienttype": "0",
        "jsToken": tokens["jsToken"],
        "dp-logid": tokens["logid"],
        "page": "1",
        "num": "20",
        "by": "name",
        "order": "asc",
        "site_referer": final_url,
        "shorturl": surl,
        "root": "1,",
    }

    list_url = f"{TERABOX_API_HOST}/share/list"
    try:
        api_resp = sess.get(
            list_url, params=params, timeout=DEFAULT_TIMEOUT, allow_redirects=True
        )
    except requests.RequestException as e:
        raise TeraboxError(f"Network error calling share/list: {e}") from e

    try:
        data = api_resp.json()
    except ValueError as e:
        raise TeraboxError("Terabox returned an unparseable response.") from e

    errno = data.get("errno")
    if errno not in (0, None):
        msg = data.get("errmsg") or f"errno={errno}"
        # 9019 / 105 are common "needs verification" / invalid share errors.
        raise TeraboxError(f"Terabox API error: {msg}")

    items = data.get("list") or []
    if not items:
        raise TeraboxError("Share contains no files (or could not be read).")

    return [_format_file(i) for i in items]


def stream_download(
    dlink: str, cookie: Optional[str] = None, range_header: Optional[str] = None
) -> requests.Response:
    """
    Open a streaming GET to a Terabox `dlink` so the caller can proxy the
    bytes through to its own client.

    Returns the live `requests.Response`; the caller is responsible for
    closing it (`with stream_download(...) as r:`).
    """
    if not dlink:
        raise TeraboxError("Empty download link.")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": "https://www.1024terabox.com/",
    }
    if cookie:
        headers["Cookie"] = cookie
    if range_header:
        headers["Range"] = range_header

    try:
        resp = requests.get(dlink, headers=headers, stream=True, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        raise TeraboxError(f"Network error opening download stream: {e}") from e

    return resp


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._\- ]+")


def safe_filename(name: str, fallback: str = "download") -> str:
    name = (name or "").strip()
    if not name:
        return fallback
    name = _FILENAME_SAFE_RE.sub("_", name)
    return name[:200] or fallback
