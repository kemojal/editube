"""Product-URL importer.

Extracts a normalized product/offer from a pasted URL. Prefers official,
structured endpoints (Shopify ``<handle>.json``, the iTunes Lookup API) over
brittle HTML scraping, with an OpenGraph/JSON-LD parser and a Gemini enrichment
pass for arbitrary landing pages. Never trust scraped values blindly — the user
edits everything before generation.

Returns a dict shaped to the ``UgcProduct`` columns.
"""

from __future__ import annotations

import json
import logging
import re
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; EditubeUGC/1.0; +https://editube.app)"
_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


# --- dispatch --------------------------------------------------------------


def detect_source_type(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    path = (urlparse(url).path or "").lower()
    if "apps.apple.com" in host or "itunes.apple.com" in host:
        return "app_store"
    if "play.google.com" in host:
        return "play"
    if "myshopify.com" in host or "/products/" in path:
        return "shopify"
    return "landing"


def import_product(url: str) -> dict[str, Any]:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("A valid http(s) product URL is required")
    source_type = detect_source_type(url)
    try:
        if source_type == "shopify":
            data = _shopify(url)
        elif source_type == "app_store":
            data = _app_store(url)
        elif source_type == "play":
            data = _play(url)
        else:
            data = _generic(url)
    except Exception as exc:  # noqa: BLE001 — fall back to generic, then bare
        logger.warning("primary extractor (%s) failed for %s: %s", source_type, url, exc)
        try:
            data = _generic(url)
            source_type = "landing"
        except Exception as exc2:  # noqa: BLE001
            logger.warning("generic extractor failed for %s: %s", url, exc2)
            data = {"name": None, "description": None, "image_urls": [], "reviews": []}

    data["source_type"] = source_type
    data["source_url"] = url
    _enrich_with_ai(data)
    # Guarantee the list/dict shape the model expects.
    for k in ("benefits", "pain_points", "use_cases", "reviews", "image_urls"):
        if not isinstance(data.get(k), list):
            data[k] = []
    return data


# --- HTTP ------------------------------------------------------------------


def _get(url: str) -> httpx.Response:
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA}) as c:
        r = c.get(url)
        r.raise_for_status()
        return r


# --- Shopify ---------------------------------------------------------------


def _shopify(url: str) -> dict[str, Any]:
    base = url.split("?")[0].rstrip("/")
    json_url = base if base.endswith(".json") else f"{base}.json"
    r = _get(json_url)
    product = (r.json() or {}).get("product") or {}
    if not product:
        raise ValueError("not a Shopify product JSON")
    variants = product.get("variants") or []
    price = str(variants[0].get("price")) if variants and variants[0].get("price") is not None else None
    images = [img.get("src") for img in (product.get("images") or []) if img.get("src")]
    return {
        "name": product.get("title"),
        "brand": product.get("vendor"),
        "price": price,
        "currency": None,
        "description": _strip_html(product.get("body_html") or ""),
        "image_urls": images,
        "reviews": [],
        "raw_scrape": {"shopify_handle": product.get("handle"), "tags": product.get("tags")},
    }


# --- App Store -------------------------------------------------------------


def _app_store(url: str) -> dict[str, Any]:
    m = re.search(r"/id(\d+)", url) or re.search(r"[?&]id=(\d+)", url)
    if not m:
        raise ValueError("could not find App Store track id in URL")
    track_id = m.group(1)
    r = _get(f"https://itunes.apple.com/lookup?id={track_id}")
    results = (r.json() or {}).get("results") or []
    if not results:
        raise ValueError("empty iTunes lookup result")
    app = results[0]
    return {
        "name": app.get("trackName"),
        "brand": app.get("sellerName") or app.get("artistName"),
        "price": str(app.get("price")) if app.get("price") is not None else "0",
        "currency": app.get("currency"),
        "description": app.get("description"),
        "image_urls": list(app.get("screenshotUrls") or [])[:6] or ([app["artworkUrl512"]] if app.get("artworkUrl512") else []),
        "reviews": [],
        "raw_scrape": {
            "genres": app.get("genres"),
            "averageUserRating": app.get("averageUserRating"),
            "userRatingCount": app.get("userRatingCount"),
            "primaryGenreName": app.get("primaryGenreName"),
        },
    }


# --- Google Play -----------------------------------------------------------


def _play(url: str) -> dict[str, Any]:
    html = _get(url).text
    og = _extract_og(html)
    return {
        "name": og.get("title"),
        "brand": None,
        "price": None,
        "currency": None,
        "description": og.get("description"),
        "image_urls": [og["image"]] if og.get("image") else [],
        "reviews": [],
        "raw_scrape": {"og": og},
    }


# --- Generic landing page --------------------------------------------------


def _generic(url: str) -> dict[str, Any]:
    html = _get(url).text
    og = _extract_og(html)
    ld = _extract_jsonld_product(html)
    name = (ld.get("name") if ld else None) or og.get("title")
    description = (ld.get("description") if ld else None) or og.get("description")
    image_urls: list[str] = []
    if ld and ld.get("image"):
        img = ld["image"]
        image_urls = img if isinstance(img, list) else [img]
    elif og.get("image"):
        image_urls = [og["image"]]
    price = None
    currency = None
    if ld and isinstance(ld.get("offers"), dict):
        price = _str_or_none(ld["offers"].get("price"))
        currency = ld["offers"].get("priceCurrency")
    price = price or og.get("price_amount")
    currency = currency or og.get("price_currency")
    reviews = _jsonld_reviews(ld) if ld else []
    return {
        "name": name,
        "brand": (ld.get("brand", {}) or {}).get("name") if isinstance(ld and ld.get("brand"), dict) else None,
        "price": price,
        "currency": currency,
        "description": description,
        "image_urls": [u for u in image_urls if u],
        "reviews": reviews,
        "raw_scrape": {"og": og, "jsonld": bool(ld)},
    }


# --- parsing helpers -------------------------------------------------------


def _strip_html(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def _str_or_none(v: Any) -> str | None:
    return None if v is None else str(v)


def _extract_og(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"<meta\b[^>]*>", html, flags=re.I):
        tag = m.group(0)
        prop = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        content = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if not prop or not content:
            continue
        key, val = prop.group(1).lower(), unescape(content.group(1)).strip()
        if key in ("og:title", "twitter:title") and "title" not in out:
            out["title"] = val
        elif key in ("og:description", "twitter:description", "description") and "description" not in out:
            out["description"] = val
        elif key in ("og:image", "twitter:image") and "image" not in out:
            out["image"] = val
        elif key in ("product:price:amount", "og:price:amount"):
            out["price_amount"] = val
        elif key in ("product:price:currency", "og:price:currency"):
            out["price_currency"] = val
    return out


def _extract_jsonld_product(html: str) -> dict[str, Any] | None:
    for m in re.finditer(
        r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    ):
        try:
            data = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _iter_jsonld_nodes(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(str(x).lower() == "product" for x in types if x):
                return node
    return None


def _iter_jsonld_nodes(data: Any):
    if isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            for n in data["@graph"]:
                if isinstance(n, dict):
                    yield n
        else:
            yield data
    elif isinstance(data, list):
        for n in data:
            if isinstance(n, dict):
                yield n


def _jsonld_reviews(ld: dict[str, Any]) -> list[str]:
    out: list[str] = []
    reviews = ld.get("review")
    if isinstance(reviews, dict):
        reviews = [reviews]
    for rv in reviews or []:
        if not isinstance(rv, dict):
            continue
        body = rv.get("reviewBody") or rv.get("description")
        if body:
            out.append(str(body)[:500])
    return out[:10]


# --- AI enrichment ---------------------------------------------------------


def _enrich_with_ai(data: dict[str, Any]) -> None:
    """Fill benefits / pain_points / use_cases / target_audience from the copy.

    Best-effort: product feeds rarely carry these. Silently no-ops if Gemini is
    unconfigured so import never hard-fails on a missing key.
    """
    needs = not (data.get("benefits") and data.get("pain_points"))
    if not needs:
        return
    source_text = "\n".join(
        str(x) for x in [data.get("name"), data.get("description"), " ".join(data.get("reviews") or [])] if x
    )[:6000]
    if not source_text.strip():
        return
    try:
        from app.services.ai_client import generate_json

        result = generate_json(
            "From this product copy, infer marketing fundamentals for short-form UGC ads.\n"
            'Return JSON: {"benefits":[".."],"pain_points":[".."],"use_cases":[".."],'
            '"target_audience":{"who":"..","demographics":"..","psychographics":".."}}\n\n'
            f"PRODUCT:\n{source_text}",
            fallback={},
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("AI enrichment skipped for product import: %s", exc)
        return
    for key in ("benefits", "pain_points", "use_cases"):
        if not data.get(key) and isinstance(result.get(key), list):
            data[key] = [str(x) for x in result[key]][:10]
    if not data.get("target_audience") and isinstance(result.get("target_audience"), dict):
        data["target_audience"] = result["target_audience"]
