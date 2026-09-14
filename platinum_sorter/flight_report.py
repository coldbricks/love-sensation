"""Interactive HTML Flight Report and radar scope cockpit generator.

Generates a standalone HTML dashboard with embedded SVGs, category filters,
and a metadata catalog. Private media files are not embedded or linked.
"""
from __future__ import annotations

import html
import math
import os
import tempfile
from pathlib import Path

from .evidence import is_accepted_semantic

TAG_COLORS = {
    "peak": "#e8c58a",      # Champagne gold
    "fill": "#ff8a65",      # Coral
    "choke": "#ff1744",     # Crimson
    "breakdown": "#4dd0e1", # Cyan
    "outro": "#b388ff",     # Purple
    "normal": "#c4c7cd",    # Silver
    "intro": "#90a4ae",     # Slate
}

MOON_SPOON_SVG = """<svg width="42" height="42" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="M48 10C26 10 10 26 10 48C10 70 26 86 48 86C60 86 71 80 78 72C62 72 48 58 48 42C48 28 56 16 68 12C62 10 55 10 48 10Z" fill="url(#gold-grad)"/>
  <path d="M42 22C42 22 75 32 82 48C85 55 83 62 78 62C72 62 66 52 54 44L40 76C39 78 36 79 34 78C32 77 31 74 32 72L46 40C38 34 36 28 42 22Z" fill="url(#silver-grad)"/>
  <defs>
    <linearGradient id="gold-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#f0d6a3"/>
      <stop offset="100%" stop-color="#cda96e"/>
    </linearGradient>
    <linearGradient id="silver-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#ffffff"/>
      <stop offset="100%" stop-color="#9a96a0"/>
    </linearGradient>
  </defs>
</svg>"""


def _number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
        return max(0.0, number) if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _categories(result: dict) -> list[str]:
    value = result.get("categories") or []
    if isinstance(value, str):
        value = [value]
    return list(dict.fromkeys(str(item).strip() for item in value
                             if item is not None and str(item).strip()))


def _media_name(result: dict) -> str:
    source = result.get("source") or result.get("path")
    # Reports may be opened on a platform other than the one that made them.
    return str(source).replace("\\", "/").rsplit("/", 1)[-1] if source else "unnamed"


def _media_type(result: dict) -> str:
    explicit = str(result.get("media_type") or "").lower()
    if explicit:
        return explicit
    suffix = Path(_media_name(result)).suffix.lower()
    return "video" if suffix in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"} else "still"


def _waveform_points(value: str | None) -> str:
    """Accept coordinates, never SVG markup or arbitrary attribute content."""
    points = []
    for pair in str(value or "").split():
        coordinates = pair.split(",")
        if len(coordinates) != 2:
            return ""
        try:
            x, y = (float(coordinate) for coordinate in coordinates)
        except (ValueError, OverflowError):
            return ""
        if not (math.isfinite(x) and math.isfinite(y)):
            return ""
        points.append(f"{x:g},{y:g}")
    return " ".join(points)


def _write_report(path: Path, content: str) -> None:
    """A failed export must leave the previous report intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def generate_flight_report(
    report_title: str,
    output_path: Path | str,
    results: list[dict],
    audio_grid: dict | None = None,
    cuts: list[dict] | None = None,
    waveform_svg: str | None = None,
) -> Path:
    """Generate a standalone flight_report.html dashboard."""
    output_path = Path(output_path)

    # Calculate statistics
    total_items = len(results)
    included_items = sum(1 for result in results if result.get("included", True))
    skipped_items = total_items - included_items
    stills = sum(1 for r in results if _media_type(r) == "still")
    videos = sum(1 for r in results if _media_type(r) in {"video", "comp"})
    category_counts: dict[str, int] = {}
    for r in results:
        for cat in _categories(r):
            category_counts[cat] = category_counts.get(cat, 0) + 1

    sorted_categories = sorted(category_counts.items(), key=lambda x: -x[1])
    max_cat_count = max(category_counts.values()) if category_counts else 1

    # Audio timeline lanes if PMV
    timeline_html = ""
    if audio_grid and cuts:
        total_dur = max(_number(audio_grid.get("total_duration_s"), 1.0), 1.0)
        raw_bars = audio_grid.get("bars") or []
        cut_blocks = []
        for c in cuts:
            t0 = _number(c.get("timeline_start_s"))
            t1 = _number(c.get("timeline_end_s"))
            left = max(0.0, min(100.0, (t0 / total_dur) * 100.0))
            width = max(0.4, min(100.0, ((t1 - t0) / total_dur) * 100.0))
            tag = str(c.get("tag") or "normal")
            color = TAG_COLORS.get(tag, "#c4c7cd")
            name = html.escape(str(c.get("clip_name") or "clip"))
            cut_blocks.append(
                f'<div class="cut-block" style="left:{left:.2f}%; width:{width:.2f}%; background:{color};" '
                f'title="{name} ({html.escape(tag)}) [{t0:.2f}s - {t1:.2f}s]"></div>'
            )

        waveform_polygon = _waveform_points(waveform_svg)
        bpm_str = f"{_number(audio_grid.get('bpm')):.1f} BPM"
        drops_count = len(audio_grid.get("drop_bars") or [])
        timeline_html = f"""
        <div class="radar-card">
            <div class="card-header">
                <h3>PMV RADAR SCOPE &bull; {bpm_str}</h3>
                <div class="badges">
                    <span class="badge">{len(cuts)} CUTS</span>
                    <span class="badge">{len(raw_bars)} BARS</span>
                    <span class="badge">{drops_count} DROPS</span>
                </div>
            </div>
            <div class="timeline-scope">
                <svg class="waveform-svg" viewBox="0 0 800 60" preserveAspectRatio="none">
                    <polygon points="{waveform_polygon}" fill="url(#wave-grad)"/>
                    <defs>
                        <linearGradient id="wave-grad" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="0%" stop-color="#e8c58a" stop-opacity="0.8"/>
                            <stop offset="100%" stop-color="#cda96e" stop-opacity="0.2"/>
                        </linearGradient>
                    </defs>
                </svg>
                <div class="cut-lane">
                    {''.join(cut_blocks)}
                </div>
            </div>
            <div class="legend">
                <span class="legend-item"><span class="dot" style="background:#e8c58a"></span> Peak Hit</span>
                <span class="legend-item"><span class="dot" style="background:#ff8a65"></span> Retrigger Fill</span>
                <span class="legend-item"><span class="dot" style="background:#ff1744"></span> Choke</span>
                <span class="legend-item"><span class="dot" style="background:#4dd0e1"></span> Breakdown</span>
                <span class="legend-item"><span class="dot" style="background:#c4c7cd"></span> Groove Cut</span>
            </div>
        </div>
        """

    # Category distribution bars with click-to-filter
    cat_bars_html = []
    for cat, count in sorted_categories[:16]:
        pct = (count / max_cat_count) * 100.0
        escaped_cat = html.escape(cat)
        cat_bars_html.append(f"""
        <div class="cat-row" data-category="{escaped_cat}" role="button" tabindex="0" title="Click to filter by {escaped_cat}">
            <span class="cat-label">{escaped_cat}</span>
            <div class="cat-bar-bg">
                <div class="cat-bar-fill" style="width:{pct:.1f}%;"></div>
            </div>
            <span class="cat-count">{count}</span>
        </div>
        """)

    # Media items grid (top items by prominence)
    def _score(r: dict) -> float:
        for k in ("prominence", "aspect_ratio", "sustained_wow"):
            value = _number(r.get(k))
            if value > 0:
                return value
        return 0.0

    ranked_results = sorted(results, key=_score, reverse=True)
    items_html = []
    for r in ranked_results[:64]:
        name = html.escape(_media_name(r))
        m_type = html.escape(_media_type(r).upper())
        prom = _number(r.get("prominence"))
        sustained = _number(r.get("sustained_wow"))
        dur_val = _number(r.get("duration_s"))
        cats = ", ".join(_categories(r)) or "None"
        review_state = "Included" if r.get("included", True) else "Skipped"
        dur_str = f"{dur_val:.1f}s • " if dur_val > 0.0 else ""
        wow_lbl = f"{prom:.2f} PEAK / {sustained:.2f} SUSTAINED" if sustained > 0 else f"{prom:.2f} WOW"
        semantic = [d for d in (r.get("detections") or []) if is_accepted_semantic(d, _categories(r))]
        evidence_label = ""
        if semantic:
            margin = max(float(d["raw_margin"]) for d in semantic)
            evidence_label = f"Image-level covered-body match · margin {margin:+.2f} (not a probability)"
            if not (r.get("geometry_available") or r.get("best_box")):
                wow_lbl = "Region not localized"
            if r.get("media_type") == "video":
                evidence_label += f" · matching sample {_number(r.get('best_match_timestamp_s')):.2f}s"
        items_html.append(f"""
        <div class="media-card" data-name="{name.lower()}" data-cats="{html.escape(cats.lower())}" data-type="{m_type.lower()}" data-review-state="{review_state.lower()}">
            <div class="card-top">
                <span class="type-pill">{dur_str}{m_type}</span>
                <span class="prom-score">{wow_lbl}</span>
            </div>
            <div class="file-name" title="{name}">{name}</div>
            <div class="card-cats">{html.escape(cats)}</div>
            <div class="card-cats">{html.escape(evidence_label)}</div>
            <div class="review-state">{review_state} in this review</div>
        </div>
        """)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(report_title)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #101014;
    color: #f3eee4;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
    padding: 32px 48px;
    min-width: 900px;
  }}
  header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid #312d34;
    padding-bottom: 24px;
    margin-bottom: 32px;
  }}
  .brand {{
    display: flex;
    align-items: center;
    gap: 16px;
  }}
  h1 {{
    font-size: 26px;
    font-weight: 700;
    letter-spacing: -0.5px;
    color: #f3eee4;
  }}
  .eyebrow {{
    font-size: 11px;
    font-weight: 600;
    color: #c5b18f;
    letter-spacing: 1.5px;
    text-transform: uppercase;
  }}
  .cockpit-badge {{
    background: #19191e;
    border: 1px solid #655b4e;
    color: #e8c58a;
    padding: 6px 14px;
    border-radius: 16px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
  }}
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 32px;
  }}
  .stat-box {{
    background: #19191e;
    border: 1px solid #3e3b40;
    border-radius: 8px;
    padding: 16px;
  }}
  .stat-val {{
    font-size: 28px;
    font-weight: 700;
    color: #e8c58a;
    margin-bottom: 4px;
  }}
  .stat-lbl {{
    font-size: 12px;
    color: #aaa6aa;
    text-transform: uppercase;
    letter-spacing: 0.8px;
  }}
  .radar-card {{
    background: #141418;
    border: 1px solid #49464a;
    border-radius: 10px;
    padding: 24px;
    margin-bottom: 32px;
  }}
  .card-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
  }}
  .card-header h3 {{
    font-size: 15px;
    font-weight: 600;
    color: #e8c58a;
  }}
  .badge {{
    background: #262329;
    border: 1px solid #655b4e;
    color: #d8b477;
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 10px;
    font-weight: 600;
    margin-left: 8px;
  }}
  .timeline-scope {{
    position: relative;
    background: #19191e;
    border: 1px solid #312d34;
    border-radius: 6px;
    overflow: hidden;
    height: 90px;
  }}
  .waveform-svg {{
    position: absolute;
    top: 0; left: 0; width: 100%; height: 60px;
  }}
  .cut-lane {{
    position: absolute;
    bottom: 0; left: 0; width: 100%; height: 26px;
    background: rgba(16, 16, 20, 0.7);
    border-top: 1px solid #3e3b40;
  }}
  .cut-block {{
    position: absolute;
    top: 2px;
    bottom: 2px;
    border-radius: 2px;
    opacity: 0.88;
    transition: opacity 0.15s;
    cursor: pointer;
  }}
  .cut-block:hover {{
    opacity: 1.0;
    filter: brightness(1.2);
  }}
  .legend {{
    display: flex;
    gap: 16px;
    margin-top: 12px;
    font-size: 11px;
    color: #aaa6aa;
    flex-wrap: wrap;
  }}
  .legend-item {{
    display: flex;
    align-items: center;
    gap: 6px;
  }}
  .dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }}
  .cut-readout {{
    margin-top: 12px;
    padding: 8px 12px;
    background: #19191e;
    border: 1px solid #3e3b40;
    border-radius: 6px;
    font-size: 12px;
    color: #d8b477;
    font-family: monospace;
  }}
  .main-grid {{
    display: grid;
    grid-template-columns: 340px 1fr;
    gap: 24px;
  }}
  .cat-panel {{
    background: #141418;
    border: 1px solid #49464a;
    border-radius: 10px;
    padding: 20px;
    height: fit-content;
  }}
  .cat-row {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
    font-size: 12px;
    cursor: pointer;
    padding: 3px 6px;
    border-radius: 4px;
    transition: background 0.15s;
  }}
  .cat-row:hover {{
    background: #242128;
  }}
  .cat-label {{
    width: 140px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: #f3eee4;
  }}
  .cat-bar-bg {{
    flex: 1;
    height: 6px;
    background: #242128;
    border-radius: 3px;
    overflow: hidden;
  }}
  .cat-bar-fill {{
    height: 100%;
    background: linear-gradient(90deg, #cda96e, #e8c58a);
    border-radius: 3px;
  }}
  .cat-count {{
    width: 32px;
    text-align: right;
    color: #aaa6aa;
    font-size: 11px;
  }}
  .catalog-panel {{
    display: flex;
    flex-direction: column;
    gap: 16px;
  }}
  .catalog-controls {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
  }}
  .search-input {{
    background: #19191e;
    border: 1px solid #49464a;
    border-radius: 6px;
    padding: 8px 14px;
    color: #f3eee4;
    font-size: 12px;
    width: 280px;
  }}
  .search-input:focus {{
    outline: none;
    border-color: #d8b477;
  }}
  .catalog-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
  }}
  .media-card {{
    background: #19191e;
    border: 1px solid #3e3b40;
    border-radius: 8px;
    padding: 14px;
    transition: transform 0.15s, border-color 0.15s;
  }}
  .media-card:hover {{
    transform: translateY(-2px);
    border-color: #d8b477;
  }}
  .card-top {{
    display: flex;
    justify-content: space-between;
    margin-bottom: 8px;
  }}
  .type-pill {{
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.8px;
    padding: 2px 6px;
    border-radius: 4px;
    background: #312d34;
    color: #c5b18f;
  }}
  .prom-score {{
    font-size: 11px;
    font-weight: 700;
    color: #e8c58a;
  }}
  .file-name {{
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 6px;
    color: #f3eee4;
  }}
  .card-cats {{
    font-size: 11px;
    color: #aaa6aa;
  }}
  .review-state {{
    margin-top: 8px;
    color: #c5b18f;
    font-size: 11px;
  }}
</style>
</head>
<body>
  <header>
    <div class="brand">
      {MOON_SPOON_SVG}
      <div>
        <div class="eyebrow">Desktop Media Workstation &bull; Flight Report</div>
        <h1>{html.escape(report_title)}</h1>
      </div>
    </div>
    <div class="cockpit-badge">CUDA ACCELERATED &bull; ZERO EXTERNAL LIBS</div>
  </header>

  <div class="stats-grid">
    <div class="stat-box">
      <div class="stat-val">{total_items:,}</div>
      <div class="stat-lbl">Total Media Items</div>
    </div>
    <div class="stat-box">
      <div class="stat-val">{videos:,}</div>
      <div class="stat-lbl">Video Clips & Comps</div>
    </div>
    <div class="stat-box">
      <div class="stat-val">{stills:,}</div>
      <div class="stat-lbl">Still Photographs</div>
    </div>
    <div class="stat-box">
      <div class="stat-val">{len(category_counts)}</div>
      <div class="stat-lbl">Active Categories</div>
    </div>
    <div class="stat-box">
      <div class="stat-val" id="includedCount">{included_items:,}</div>
      <div class="stat-lbl">Included in Review</div>
    </div>
    <div class="stat-box">
      <div class="stat-val" id="skippedCount">{skipped_items:,}</div>
      <div class="stat-lbl">Skipped by Review</div>
    </div>
  </div>

  {timeline_html}

  <div class="main-grid">
    <div class="cat-panel">
      <h3 style="font-size:14px; font-weight:600; margin-bottom:16px; color:#e8c58a;">Category Distribution</h3>
      {''.join(cat_bars_html)}
      <button onclick="resetFilters()" style="margin-top:14px; width:100%; background:#242128; border:1px solid #49464a; color:#c5b18f; padding:6px; border-radius:4px; cursor:pointer; font-size:11px;">Reset Category Filters</button>
    </div>

    <div class="catalog-panel">
      <div class="catalog-controls">
        <h3 style="font-size:14px; font-weight:600; color:#e8c58a;">Media Assets Catalog</h3>
        <input type="text" id="reportSearch" class="search-input" placeholder="Search filenames or categories..." oninput="filterCatalog()">
      </div>
      <div class="catalog-grid" id="catalogGrid">
        {''.join(items_html)}
      </div>
    </div>
  </div>

  <script>
    function filterCatalog() {{
      const query = document.getElementById('reportSearch').value.toLowerCase();
      const cards = document.querySelectorAll('.media-card');
      cards.forEach(card => {{
        const name = card.getAttribute('data-name') || '';
        const cats = card.getAttribute('data-cats') || '';
        const type = card.getAttribute('data-type') || '';
        const reviewState = card.getAttribute('data-review-state') || '';
        const match = name.includes(query) || cats.includes(query) || type.includes(query) || reviewState.includes(query);
        card.style.display = match ? 'block' : 'none';
      }});
    }}

    function filterByCategory(cat) {{
      const input = document.getElementById('reportSearch');
      input.value = cat.toLowerCase();
      filterCatalog();
    }}

    function resetFilters() {{
      const input = document.getElementById('reportSearch');
      input.value = '';
      filterCatalog();
    }}

    document.querySelectorAll('.cat-row').forEach(row => {{
      const selectCategory = () => filterByCategory(row.getAttribute('data-category') || '');
      row.addEventListener('click', selectCategory);
      row.addEventListener('keydown', event => {{
        if (event.key === 'Enter' || event.key === ' ') {{
          event.preventDefault();
          selectCategory();
        }}
      }});
    }});

    // Cut block inspection
    document.querySelectorAll('.cut-block').forEach(block => {{
      block.addEventListener('click', () => {{
        const title = block.getAttribute('title');
        let readout = document.getElementById('cutReadout');
        if (!readout) {{
          readout = document.createElement('div');
          readout.id = 'cutReadout';
          readout.className = 'cut-readout';
          const scope = document.querySelector('.radar-card');
          if (scope) scope.appendChild(readout);
        }}
        if (readout) readout.innerText = 'Active Cut Inspector: ' + title;
      }});
    }});
  </script>
</body>
</html>
"""
    _write_report(output_path, html_content)
    return output_path
