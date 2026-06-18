"""Self-contained local HTML report. Thumbnails are embedded as base64 so the
file is a single artifact you open from disk (file://). Nothing is served or
uploaded.
"""

from __future__ import annotations

import base64
import html
import io
import os
from typing import Optional

from .analyze import Findings
from .cluster import DuplicateGroup
from .model import Config, Record
from .quality import keeper_score


def _thumb_data_uri(rec: Record, px: int = 240) -> Optional[str]:
    """Make a small JPEG thumbnail (prefer the smallest existing derivative)."""
    try:
        from PIL import Image
    except Exception:
        return None
    candidates = list(rec.derivatives)
    if rec.path:
        candidates.append(rec.path)
    for p in sorted(candidates, key=lambda x: _size(x)):  # smallest first = fastest
        try:
            with Image.open(p) as im:
                im = im.convert("RGB")
                im.thumbnail((px, px))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=70)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        except Exception:
            continue
    return None


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 1 << 62  # missing files sort last


def _esc(s: str) -> str:
    return html.escape(s or "")


def _card(rec: Record, cfg: Config, *, kind: str, subtitle: str = "") -> str:
    uri = _thumb_data_uri(rec)
    img = (f'<img src="{uri}" loading="lazy">' if uri
           else '<div class="noimg">no preview</div>')
    sharp = f"{rec.laplacian:.0f}" if rec.laplacian is not None else "–"
    meta = (f"score {keeper_score(rec, cfg):+.2f} · focus {sharp} · "
            f"{rec.width}×{rec.height}")
    star = " ★" if rec.favorite else ""
    return f"""
    <figure class="card {kind}">
      {img}
      <figcaption>
        <div class="fn">{_esc(rec.original_filename)}{star}</div>
        <div class="sub">{_esc(subtitle)}</div>
        <div class="meta">{_esc(meta)}</div>
      </figcaption>
    </figure>"""


def _screenshot_section(f: Findings, cfg: Config) -> str:
    if not f.work_screenshots:
        return "<p class='empty'>No high-confidence work screenshots found.</p>"
    cards = []
    for rec, verdict in f.work_screenshots:
        snippet = rec.detected_text.strip().replace("\n", " ")[:120]
        sub = " · ".join(verdict.reasons)
        if snippet:
            sub += f" — “{snippet}…”"
        cards.append(_card(rec, cfg, kind="discard", subtitle=sub))
    return f'<div class="grid">{"".join(cards)}</div>'


def _dup_section(f: Findings, cfg: Config) -> str:
    if not f.duplicate_groups:
        return "<p class='empty'>No near-duplicate groups found.</p>"
    blocks = []
    for i, g in enumerate(f.duplicate_groups, 1):
        keep = "".join(_card(r, cfg, kind="keep", subtitle="KEEP") for r in g.keepers)
        disc = "".join(_card(r, cfg, kind="discard", subtitle="discard") for r in g.discards)
        blocks.append(f"""
        <div class="group">
          <h3>Group {i} — {g.size} shots · keep {len(g.keepers)} · discard {len(g.discards)}</h3>
          <div class="grid">{keep}{disc}</div>
        </div>""")
    return "".join(blocks)


_CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 24px; }
h1 { margin: 0 0 4px; } h2 { margin: 32px 0 8px; border-bottom: 1px solid #8884; padding-bottom: 4px; }
.note { background: #2e7d3220; border-left: 3px solid #2e7d32; padding: 8px 12px; border-radius: 4px; }
.stats { display: flex; gap: 24px; margin: 12px 0; flex-wrap: wrap; }
.stat b { font-size: 22px; display: block; }
.grid { display: flex; flex-wrap: wrap; gap: 10px; }
.group { margin: 16px 0; padding: 12px; border: 1px solid #8883; border-radius: 8px; }
.card { width: 160px; margin: 0; border-radius: 6px; overflow: hidden; border: 2px solid transparent; background: #8881; }
.card img, .card .noimg { width: 160px; height: 160px; object-fit: cover; display: block; }
.noimg { display: flex; align-items: center; justify-content: center; color: #888; }
.card.keep { border-color: #2e7d32; } .card.discard { border-color: #c62828; }
figcaption { padding: 6px 8px; }
.fn { font-weight: 600; font-size: 12px; word-break: break-all; }
.sub { font-size: 11px; color: #c62828; } .card.keep .sub { color: #2e7d32; }
.meta { font-size: 11px; color: #888; }
.empty { color: #888; font-style: italic; }
"""


def render_html(f: Findings, cfg: Config) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Photo cleanup — review</title><style>{_CSS}</style></head><body>
<h1>Photo cleanup — review</h1>
<p class="note"><b>Dry run.</b> Nothing in your library has been changed. All
analysis ran on-device — no uploads. Review below, then run with <code>--apply</code>
to tag discards (<code>cleanup:*</code> keywords) and Favorite the keepers.</p>
<div class="stats">
  <div class="stat"><b>{f.total_scanned}</b> photos scanned</div>
  <div class="stat"><b>{len(f.work_screenshots)}</b> work screenshots</div>
  <div class="stat"><b>{len(f.duplicate_groups)}</b> duplicate groups</div>
  <div class="stat"><b>{f.n_discards}</b> discard candidates</div>
</div>
<h2>Work screenshots → <code>cleanup:screenshot</code></h2>
{_screenshot_section(f, cfg)}
<h2>Near-duplicate photoshoots → keep best, discard rest</h2>
{_dup_section(f, cfg)}
</body></html>"""


def render_dedup_html(groups, total: int, cfg: Config, label: str = "") -> str:
    """Standalone near-duplicate review report (used by the `dedup` command)."""
    groups = sorted(groups, key=lambda g: g.size, reverse=True)
    discard = sum(len(g.discards) for g in groups)
    blocks = []
    for i, g in enumerate(groups, 1):
        keep = "".join(_card(r, cfg, kind="keep", subtitle="KEEP")
                       for r in sorted(g.keepers, key=lambda r: r.timestamp or 0))
        disc = "".join(_card(r, cfg, kind="discard", subtitle="discard")
                       for r in sorted(g.discards, key=lambda r: r.timestamp or 0))
        blocks.append(
            f"<div class='group'><h3>Burst {i} — {g.size} shots · "
            f"keep {len(g.keepers)} · discard {len(g.discards)}</h3>"
            f"<div class='grid'>{keep}{disc}</div></div>")
    body = "".join(blocks) if blocks else "<p class='empty'>No near-duplicate bursts found.</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Dedup review {_esc(label)}</title><style>{_CSS}</style></head><body>
<h1>Near-duplicate review {_esc(label)}</h1>
<p class="note"><b>Dry run.</b> On-device Apple Vision embeddings (distance ≤
{cfg.embedding_max_distance}). Green = suggested keeper, red = near-duplicate.
On <code>--apply</code>: the <b>whole burst</b> is tagged <code>cleanup:duplicate</code>
and the green keepers are <b>Favorited</b>, so you review full bursts with picks
pre-marked, add any Favorites you want, then delete
<code>cleanup:duplicate AND not Favorite</code>. Nothing changed yet.</p>
<div class="stats">
  <div class="stat"><b>{total}</b> photos in scope</div>
  <div class="stat"><b>{len(groups)}</b> bursts</div>
  <div class="stat"><b>{discard}</b> proposed discards</div>
  <div class="stat"><b>{total - discard}</b> would remain</div>
</div>
{body}</body></html>"""


def render_expired_html(items, total: int, cfg: Config, label: str = "") -> str:
    """Flat grid of flagged expired-utility photos (each with its reason)."""
    cards = []
    for rec, verdict in items:
        snippet = (rec.detected_text or "").strip().replace("\n", " ")[:90]
        sub = " · ".join(verdict.reasons)
        if snippet:
            sub += f" — “{snippet}…”"
        cards.append(_card(rec, cfg, kind="discard", subtitle=sub))
    body = f'<div class="grid">{"".join(cards)}</div>' if cards else \
        "<p class='empty'>No expired-utility photos found.</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Expired-utility review {_esc(label)}</title><style>{_CSS}</style></head><body>
<h1>Expired single-purpose photos {_esc(label)}</h1>
<p class="note"><b>Dry run.</b> On-device only (Apple OCR + labels + age ≥
{cfg.expired_min_age_years}y). These look like utility shots (receipts, wifi,
parking, tickets…) past their usefulness. On <code>--apply</code> they're tagged
<code>cleanup:expired</code>; review, Favorite (♥) anything to keep, then delete
the rest. Photos with people/pets/food/scenery are never flagged. Nothing changed.</p>
<div class="stats">
  <div class="stat"><b>{total}</b> photos in scope</div>
  <div class="stat"><b>{len(items)}</b> flagged expired</div>
</div>
{body}</body></html>"""


def _video_card(rec: Record, kind: str, size: int, sub: str) -> str:
    uri = _thumb_data_uri(rec)
    img = (f'<img src="{uri}" loading="lazy">' if uri
           else '<div class="noimg">video</div>')
    mb = size / (1024 * 1024)
    return f"""<figure class="card {kind}">{img}<figcaption>
      <div class="fn">{_esc(rec.original_filename)}</div>
      <div class="sub">{_esc(sub)}</div>
      <div class="meta">{mb:.0f} MB</div></figcaption></figure>"""


def render_videos_html(dup_groups, larges, total: int, cfg: Config, label: str = "") -> str:
    from .video import video_size
    dup_blocks = []
    reclaim_dup = 0
    for i, g in enumerate(dup_groups, 1):
        keep = "".join(_video_card(r, "keep", video_size(r), "KEEP (largest take)")
                       for r in g.keepers)
        disc = ""
        for r in g.discards:
            reclaim_dup += video_size(r)
            disc += _video_card(r, "discard", video_size(r), "extra take")
        dup_blocks.append(f"<div class='group'><h3>Take group {i} — {g.size} videos · "
                          f"keep {len(g.keepers)} · discard {len(g.discards)}</h3>"
                          f"<div class='grid'>{keep}{disc}</div></div>")
    dup_body = "".join(dup_blocks) if dup_blocks else \
        "<p class='empty'>No near-duplicate video takes found.</p>"

    large_cards = "".join(_video_card(lv.rec, "keep", lv.size, "large — ♥ kept; un-♥ to drop")
                          for lv in larges)
    reclaim_large = sum(lv.size for lv in larges)
    large_body = f"<div class='grid'>{large_cards}</div>" if larges else \
        "<p class='empty'>No oversized videos.</p>"

    gb = 1024 ** 3
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Video cleanup {_esc(label)}</title><style>{_CSS}</style></head><body>
<h1>Video cleanup {_esc(label)}</h1>
<p class="note"><b>Dry run.</b> On-device (poster-frame embeddings + size).
Everything here is tagged <code>cleanup:video</code> on apply. <b>Favorited (♥) =
keep:</b> the best size/quality take in each group, and ALL large videos (un-♥
the ones you decide to drop). Extra takes stay un-♥ = delete candidates.
Delete <code>cleanup:video AND not Favorite</code>. Nothing changed yet.</p>
<div class="stats">
  <div class="stat"><b>{total}</b> videos in scope</div>
  <div class="stat"><b>{len(dup_groups)}</b> take groups</div>
  <div class="stat"><b>{reclaim_dup/gb:.1f} GB</b> in extra takes</div>
  <div class="stat"><b>{len(larges)}</b> oversized · {reclaim_large/gb:.1f} GB</div>
</div>
<h2>Near-duplicate takes (keep best ratio ♥, drop extra takes)</h2>
{dup_body}
<h2>Oversized videos ≥{cfg.large_video_mb:.0f} MB (all ♥ — un-♥ to drop)</h2>
{large_body}
</body></html>"""


def write_report(f: Findings, cfg: Config, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(render_html(f, cfg))
    return os.path.abspath(path)
