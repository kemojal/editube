"""AI ad performance learner.

Aggregates manually-entered (or, later, connected) metrics per variation, ranks
the winners, summarizes what they have in common, and produces a ``dimensions``
object the variation engine can consume to "generate more like the top
performer". Degrades to a deterministic heuristic when Gemini is unavailable.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import UgcAvatar, UgcCampaign, UgcPerformance, UgcVariation

logger = logging.getLogger(__name__)

_METRIC_PRIORITY = ["roas", "cvr", "ctr", "conversions", "clicks", "impressions"]


def aggregate_performance(rows: list[UgcPerformance]) -> dict[str, float | None]:
    """Sum raw counts across a variation's metric rows; derive ctr/cvr/roas."""
    spend = sum(float(r.spend or 0) for r in rows)
    impressions = sum(int(r.impressions or 0) for r in rows)
    clicks = sum(int(r.clicks or 0) for r in rows)
    conversions = sum(int(r.conversions or 0) for r in rows)
    roas_vals = [float(r.roas) for r in rows if r.roas is not None]
    ctr = (clicks / impressions) if impressions else None
    cvr = (conversions / clicks) if clicks else None
    roas = (sum(roas_vals) / len(roas_vals)) if roas_vals else None
    return {
        "spend": spend or None,
        "impressions": impressions or None,
        "clicks": clicks or None,
        "conversions": conversions or None,
        "ctr": ctr,
        "cvr": cvr,
        "roas": roas,
    }


def primary_metric(aggs: list[dict[str, Any]]) -> str:
    """Highest-priority metric that at least one variation reports."""
    for metric in _METRIC_PRIORITY:
        if any(a.get(metric) is not None for a in aggs):
            return metric
    return "impressions"


def recommended_dimensions(top: list[dict[str, Any]]) -> dict[str, Any]:
    """Build engine ``dimensions`` biased toward the winning attributes."""
    avatars: list[dict[str, Any]] = []
    seen: set[str] = set()
    for v in top:
        pid = v.get("provider_avatar_id")
        if pid and pid not in seen:
            seen.add(pid)
            avatars.append({"provider_avatar_id": pid, "name": v.get("avatar_name")})
    lengths = sorted({int(v["length_sec"]) for v in top if v.get("length_sec")})
    aspects = sorted({v["aspect_ratio"] for v in top if v.get("aspect_ratio")})
    hooks = [v["hook"] for v in top if v.get("hook")][:8]
    dims: dict[str, Any] = {}
    if avatars:
        dims["avatars"] = avatars
    if lengths:
        dims["lengths"] = lengths
    if aspects:
        dims["aspect_ratios"] = aspects
    if hooks:
        dims["hooks"] = hooks
    return dims


def _length_bucket(sec: int | None) -> str:
    s = int(sec or 0)
    if s <= 20:
        return "short (≤20s)"
    if s <= 35:
        return "mid (20–35s)"
    return "long (35s+)"


def _top_key(counter: dict[str, int]) -> str | None:
    return max(counter, key=counter.get) if counter else None  # type: ignore[arg-type]


def attribute_summary(top: list[dict[str, Any]]) -> dict[str, Any]:
    def freq(key_fn) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in top:
            k = key_fn(v)
            if k:
                out[k] = out.get(k, 0) + 1
        return out

    return {
        "angle": freq(lambda v: (v.get("angle") or "").replace("-", " ") or None),
        "length_bucket": freq(lambda v: _length_bucket(v.get("length_sec"))),
        "aspect": freq(lambda v: v.get("aspect_ratio")),
        "gender": freq(lambda v: v.get("gender")),
    }


def _heuristic_insight(attr: dict[str, Any], metric: str) -> str:
    parts: list[str] = []
    g = _top_key(attr["gender"])
    if g:
        parts.append(f"{g} creators")
    a = _top_key(attr["angle"])
    if a:
        parts.append(f"{a} scripts")
    lb = _top_key(attr["length_bucket"])
    if lb:
        parts.append(lb)
    asp = _top_key(attr["aspect"])
    if asp:
        parts.append(asp)
    if not parts:
        return f"Your top ads by {metric} are listed below — add more metrics to sharpen the read."
    return f"Your best-performing ads (by {metric}) skew " + ", ".join(parts) + "."


def analyze_campaign(db: Session, campaign_id: int) -> dict[str, Any]:
    campaign = db.query(UgcCampaign).filter(UgcCampaign.id == campaign_id).first()
    if not campaign:
        raise ValueError(f"campaign {campaign_id} not found")

    variations = db.query(UgcVariation).filter(UgcVariation.campaign_id == campaign_id).all()
    if not variations:
        return {"has_data": False, "insights": "No variations yet.", "top_variations": [], "recommended_dimensions": {}}
    var_by_id = {v.id: v for v in variations}

    perf_rows = (
        db.query(UgcPerformance)
        .filter(UgcPerformance.variation_id.in_(list(var_by_id.keys())))
        .all()
    )
    if not perf_rows:
        return {
            "has_data": False,
            "insights": "Add performance metrics to your ads to unlock recommendations.",
            "top_variations": [],
            "recommended_dimensions": {},
        }

    grouped: dict[int, list[UgcPerformance]] = {}
    for p in perf_rows:
        grouped.setdefault(p.variation_id, []).append(p)

    # Gender lookup from the avatar catalog for the involved creators.
    pids = {v.provider_avatar_id for v in variations if v.provider_avatar_id}
    gender_map: dict[str, str] = {}
    if pids:
        for a in db.query(UgcAvatar).filter(UgcAvatar.provider_avatar_id.in_(list(pids))).all():
            if a.gender_presentation:
                gender_map[a.provider_avatar_id] = a.gender_presentation

    scored: list[dict[str, Any]] = []
    for vid, rows in grouped.items():
        v = var_by_id.get(vid)
        if not v:
            continue
        agg = aggregate_performance(rows)
        scored.append(
            {
                "id": v.id,
                "name": v.name,
                "angle": v.angle,
                "length_sec": v.length_sec,
                "aspect_ratio": v.aspect_ratio,
                "provider_avatar_id": v.provider_avatar_id,
                "avatar_name": v.avatar_name,
                "gender": gender_map.get(v.provider_avatar_id or ""),
                "hook": v.hook,
                "thumbnail_url": v.thumbnail_url,
                "storage_url": v.storage_url,
                "metrics": agg,
            }
        )

    metric = primary_metric([s["metrics"] for s in scored])
    scored.sort(key=lambda s: (s["metrics"].get(metric) or 0), reverse=True)
    n_top = max(1, len(scored) // 4)
    top = scored[:n_top]

    attr = attribute_summary(top)
    rec = recommended_dimensions(top)
    insights = _heuristic_insight(attr, metric)
    try:
        from app.services.ai_client import generate_text

        nicer = generate_text(
            "In one punchy sentence for a marketer, summarize what makes these UGC ads win. "
            f"Ranking metric: {metric}. Winning attribute counts: {attr}."
        )
        if nicer:
            insights = nicer.strip()
    except Exception as exc:  # noqa: BLE001
        logger.info("learner insight using heuristic (AI unavailable): %s", exc)

    return {
        "has_data": True,
        "primary_metric": metric,
        "analyzed": len(scored),
        "top_variations": top,
        "attribute_summary": attr,
        "recommended_dimensions": rec,
        "insights": insights,
    }
