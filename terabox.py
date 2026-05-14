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
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger(__name__)

# A real-looking desktop browser UA. Terabox blocks obviously-automated UAs.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

# Hosts to try (in order) when fetching the share landing page and calling the
# share/list API. Terabox runs the same backend on multiple mirrors; if one
# refuses or rate-limits us, the next usually works.
TERABOX_HOSTS = (
    "https://www.terabox.com",
    "https://www.1024terabox.com",
    "https://www.terabox.app",
    "https://www.1024tera.com",
)

# Default API host used for share/list calls. We'll fall back to other hosts
# in `TERABOX_HOSTS` if this one fails.
TERABOX_API_HOST = TERABOX_HOSTS[0]

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
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "sec-ch-ua": '"Chromium";v="135", "Not-A.Brand";v="8"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
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
    """Extract the `surl` parameter from any Terabox-style share URL.

    Terabox short share URLs come in two flavours:
        https://host/s/1<surl>          (path form, with a leading "1")
        https://host/sharing/link?surl=<surl>   (query form, no leading "1")
    We always return the bare `surl` (no leading "1") because that's what the
    `share/list` API expects in the `shorturl` parameter.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "surl" in qs and qs["surl"]:
        s = qs["surl"][0]
        # Some share URLs put the leading "1" into the query form too.
        return s[1:] if s.startswith("1") else s
    if "/s/" in parsed.path:
        tail = parsed.path.split("/s/", 1)[1]
        tail = tail.split("/")[0].split("?")[0]
        # Terabox prefixes shortcodes with "1" in the path form.
        if tail.startswith("1"):
            tail = tail[1:]
        return tail
    raise TeraboxError("Could not find share id (surl) in the link.")


def _resolve_share_page(
    sess: requests.Session, original_url: str, surl: str
) -> Tuple[str, str]:
    """Fetch the canonical share landing page and return (final_url, html).

    Terabox now redirects all `/s/<surl>` URLs to `/error/404.html`, so we
    skip that path entirely and hit `/sharing/link?surl=<surl>` directly.
    We try several mirror hosts in turn — if one returns a page that doesn't
    contain the auth tokens (e.g. due to a regional block or temporary
    error), we fall through to the next.
    """
    # Try the host the user gave us first, then the known mirrors.
    user_host = ""
    try:
        user_host = "https://" + (urlparse(original_url).netloc or "")
        if user_host == "https://":
            user_host = ""
    except Exception:
        user_host = ""

    candidates: List[str] = []
    if user_host and user_host not in TERABOX_HOSTS:
        candidates.append(user_host)
    candidates.extend(TERABOX_HOSTS)

    last_error: Optional[str] = None
    for host in candidates:
        share_url = f"{host}/sharing/link?surl={surl}"
        try:
            resp = sess.get(
                share_url, timeout=DEFAULT_TIMEOUT, allow_redirects=True
            )
        except requests.RequestException as e:
            last_error = f"network error from {host}: {e}"
            log.warning("Share page fetch failed for %s: %s", share_url, e)
            continue

        if resp.status_code >= 400:
            last_error = f"{host} returned HTTP {resp.status_code}"
            log.warning(last_error)
            continue

        # The share landing page must contain the inline `fn("<jsToken>")`
        # call. If it doesn't, the share is private/expired/removed or this
        # mirror is serving an error/captcha page.
        if "fn%28%22" in resp.text or "jsToken" in resp.text:
            return resp.url, resp.text

        last_error = f"{host} did not include auth tokens (share may be private/expired)"
        log.warning(last_error)

    raise TeraboxError(
        "Could not extract auth tokens from the share page. "
        "The link may be private, expired, or require login cookies."
    )


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

    url = url.strip()
    sess = _build_session(cookie)
    surl = _extract_surl(url)
    final_url, html = _resolve_share_page(sess, url, surl)

    tokens = _extract_tokens(html)
    if not tokens["jsToken"]:
        # _resolve_share_page already filters out pages without jsToken, so
        # this is a defensive fallback.
        raise TeraboxError(
            "Could not extract auth tokens from the share page. "
            "The link may be private, expired, or require login cookies."
        )

    # `dp-logid` is no longer embedded in the share page HTML on the new
    # Terabox frontend. The API still requires *some* value for the field,
    # but accepts any opaque string. Use a millisecond timestamp as a
    # browser-like log id; this matches what real browsers send.
    logid = tokens["logid"] or str(int(time.time() * 1000))

    params = {
        "app_id": "250528",
        "web": "1",
        "channel": "dubox",
        "clienttype": "0",
        "jsToken": tokens["jsToken"],
        "dp-logid": logid,
        "page": "1",
        "num": "20",
        "by": "name",
        "order": "asc",
        "site_referer": final_url,
        "shorturl": surl,
        "root": "1,",
    }

    # The same jsToken works on every Terabox mirror, so try each in turn
    # and return the first non-error response.
    final_host = "https://" + (urlparse(final_url).netloc or "www.terabox.com")
    api_hosts = [final_host] + [h for h in TERABOX_HOSTS if h != final_host]

    last_msg = ""
    sess.headers["Referer"] = final_url
    for host in api_hosts:
        list_url = f"{host}/share/list"
        try:
            api_resp = sess.get(
                list_url,
                params=params,
                timeout=DEFAULT_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            last_msg = f"network error calling {host}: {e}"
            log.warning(last_msg)
            continue

        try:
            data = api_resp.json()
        except ValueError:
            last_msg = f"{host} returned non-JSON response"
            log.warning("share/list non-JSON from %s: %s", host, api_resp.text[:200])
            continue

        errno = data.get("errno")
        if errno in (0, None) and data.get("list"):
            return [_format_file(i) for i in data["list"]]

        # Capture the most informative error and try the next host.
        last_msg = data.get("errmsg") or f"errno={errno}"
        log.info("share/list error from %s: %s", host, last_msg)

        # Some errors are terminal — no point trying other mirrors.
        if errno in (105, 9019, -21, -130):
            break

    raise TeraboxError(
        f"Terabox API error: {last_msg or 'unknown error'}"
    )


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
