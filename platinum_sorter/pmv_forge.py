"""PMV Forge timeline auto-assembler and Premiere Pro / FCP7 XML exporter.

Maps analyzed video takes onto the musical beat grid using the Chaos Knob grammar,
placing high-prominence hits on drops, retrigger fills on pre-drop builds, and half-time
holds through breakdowns.
"""
from __future__ import annotations

import hashlib
import html
import math
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import quote


@dataclass
class CutSlice:
    clip_path: str
    clip_name: str
    timeline_start_s: float
    timeline_end_s: float
    duration_s: float
    clip_in_s: float
    clip_out_s: float
    tag: str = "normal"  # normal, fill, choke, peak, breakdown, outro, intro
    handle_in_s: float = 0.25
    handle_out_s: float = 0.25


def assemble_pmv_timeline(
    audio_grid: dict,
    candidate_clips: list[dict],
    chaos: float = 0.5,
    seed: int = 7,
    fps: int | float = 30,
) -> list[CutSlice]:
    """Auto-edit video takes onto the song's beat grid according to musical grammar.

    Enforces recency penalties so clips don't repeat consecutively, reserves transition handles
    for NLE cross-dissolves, and maps peak visual assets onto musical drops.
    """
    if not candidate_clips:
        return []

    rng = random.Random(seed)
    try:
        nominal_total = float(audio_grid.get("total_duration_s") or 0.0)
    except (TypeError, ValueError):
        nominal_total = 0.0

    raw_bars = audio_grid.get("bars") or []
    bars: list[float] = []
    for b in raw_bars:
        try:
            val = float(b)
            if math.isfinite(val):
                bars.append(val)
        except (TypeError, ValueError):
            pass

    raw_beats = audio_grid.get("beats") or []
    beats: list[float] = []
    for b in raw_beats:
        try:
            val = float(b)
            if math.isfinite(val):
                beats.append(val)
        except (TypeError, ValueError):
            pass

    drop_bars: set[int] = set()
    for b in (audio_grid.get("drop_bars") or []):
        try:
            drop_bars.add(int(b))
        except (TypeError, ValueError):
            pass

    breakdown_bars: set[int] = set()
    for b in (audio_grid.get("breakdown_bars") or []):
        try:
            breakdown_bars.add(int(b))
        except (TypeError, ValueError):
            pass

    try:
        beat_period = float(audio_grid.get("beat_period") or 0.5)
        if beat_period <= 0.0 or not math.isfinite(beat_period):
            beat_period = 0.5
    except (TypeError, ValueError):
        beat_period = 0.5

    if not bars:
        return []

    default_bar_dur = (bars[1] - bars[0]) if len(bars) > 1 else (4.0 * beat_period)
    total_duration = max(nominal_total, bars[-1] + default_bar_dur)

    # Sort candidates by prominence descending for peak moments (safe against None)
    def _score(c: dict) -> float:
        for k in ("prominence", "aspect_ratio", "sustained_wow"):
            val = c.get(k)
            if val is not None:
                try:
                    f = float(val)
                    if math.isfinite(f) and f > 0.0:
                        return f
                except (TypeError, ValueError):
                    pass
        return 0.0

    ranked_candidates = sorted(candidate_clips, key=_score, reverse=True)

    # Recency history to ensure deep cut variety (enforces alternation when len >= 2)
    recent_paths: list[str] = []
    max_recent = min(3, len(candidate_clips) - 1) if len(candidate_clips) >= 2 else 0

    def pick_candidate(pool: list[dict], prefer_unused: bool = True) -> dict:
        if prefer_unused and max_recent > 0:
            available = [c for c in pool if str(c.get("path") or "") not in recent_paths]
            if available:
                chosen = rng.choice(available)
                recent_paths.append(str(chosen.get("path") or ""))
                if len(recent_paths) > max_recent:
                    recent_paths.pop(0)
                return chosen
        chosen = rng.choice(pool)
        if max_recent > 0:
            recent_paths.append(str(chosen.get("path") or ""))
            if len(recent_paths) > max_recent:
                recent_paths.pop(0)
        return chosen

    def pick_clip_in_with_handles(clip: dict, slice_dur: float, tag: str = "normal") -> tuple[float, float, float, float]:
        try:
            clip_dur = float(clip.get("duration_s") or 5.0)
            if clip_dur <= 0.0 or not math.isfinite(clip_dur):
                clip_dur = 5.0
        except (TypeError, ValueError):
            clip_dur = 5.0
        handle = 0.25  # 8-15 frame handle
        try:
            best_ts = float(clip.get("best_timestamp_s") or 0.0)
            if not math.isfinite(best_ts) or best_ts < 0.0:
                best_ts = 0.0
        except (TypeError, ValueError):
            best_ts = 0.0

        # For peak moments on drops, center the cut slice around the focal climax timestamp
        if tag == "peak" and best_ts > 0.0 and clip_dur > slice_dur:
            target_in = max(0.0, best_ts - (slice_dur / 2.0))
            if target_in + slice_dur > clip_dur:
                target_in = max(0.0, clip_dur - slice_dur)
            h_in = min(handle, target_in)
            h_out = min(handle, max(0.0, clip_dur - (target_in + slice_dur)))
            return target_in, target_in + slice_dur, h_in, h_out

        if clip_dur >= slice_dur + (handle * 2):
            in_pt = rng.uniform(handle, clip_dur - slice_dur - handle)
            return in_pt, in_pt + slice_dur, handle, handle
        elif clip_dur > slice_dur:
            in_pt = rng.uniform(0.0, clip_dur - slice_dur)
            return in_pt, in_pt + slice_dur, in_pt, clip_dur - (in_pt + slice_dur)
        else:
            return 0.0, slice_dur, 0.0, 0.0

    cuts: list[CutSlice] = []
    first_beat = beats[0] if beats else 0.0

    # Optional intro pad before first beat
    if first_beat > 0.05:
        intro_clip = pick_candidate(candidate_clips)
        cin, cout, hin, hout = pick_clip_in_with_handles(intro_clip, first_beat, tag="intro")
        intro_path = str(intro_clip.get("path") or "")
        cuts.append(CutSlice(
            clip_path=intro_path,
            clip_name=Path(intro_path).name if intro_path else "intro",
            timeline_start_s=0.0,
            timeline_end_s=round(first_beat, 4),
            duration_s=round(first_beat, 4),
            clip_in_s=round(cin, 3),
            clip_out_s=round(cout, 3),
            tag="intro",
            handle_in_s=round(hin, 3),
            handle_out_s=round(hout, 3),
        ))

    n_bars = len(bars)
    for b_idx in range(n_bars):
        bar_start = bars[b_idx]
        bar_end = bars[b_idx + 1] if b_idx + 1 < n_bars else total_duration
        bar_dur = bar_end - bar_start
        if bar_dur <= 0:
            continue

        is_drop = b_idx in drop_bars
        is_predrop = (b_idx + 1) in drop_bars
        is_breakdown = b_idx in breakdown_bars

        # 1. Pre-drop build: Retrigger FILL into the drop
        if is_predrop:
            sub_cuts = 4 if chaos >= 0.4 else 2
            sub_dur = bar_dur / sub_cuts
            fill_clip = pick_candidate(candidate_clips)
            base_in, _, hin, hout = pick_clip_in_with_handles(fill_clip, sub_dur, tag="fill")
            fill_path = str(fill_clip.get("path") or "")
            for s in range(sub_cuts):
                t0 = bar_start + s * sub_dur
                t1 = t0 + sub_dur
                tag = "choke" if s == sub_cuts - 1 else "fill"
                cuts.append(CutSlice(
                    clip_path=fill_path,
                    clip_name=Path(fill_path).name if fill_path else "fill",
                    timeline_start_s=round(t0, 4),
                    timeline_end_s=round(t1, 4),
                    duration_s=round(sub_dur, 4),
                    clip_in_s=round(base_in + (s * 0.15), 3),
                    clip_out_s=round(base_in + (s * 0.15) + sub_dur, 3),
                    tag=tag,
                    handle_in_s=round(hin, 3),
                    handle_out_s=round(hout, 3),
                ))
            continue

        # 2. Drop bar: High prominence peak cut right on the downbeat
        if is_drop:
            peak_clip = ranked_candidates[0] if ranked_candidates else pick_candidate(candidate_clips)
            if len(ranked_candidates) > 1:
                ranked_candidates = ranked_candidates[1:] + [ranked_candidates[0]]
            cin, cout, hin, hout = pick_clip_in_with_handles(peak_clip, bar_dur, tag="peak")
            peak_path = str(peak_clip.get("path") or "")
            cuts.append(CutSlice(
                clip_path=peak_path,
                clip_name=Path(peak_path).name if peak_path else "peak",
                timeline_start_s=round(bar_start, 4),
                timeline_end_s=round(bar_end, 4),
                duration_s=round(bar_dur, 4),
                clip_in_s=round(cin, 3),
                clip_out_s=round(cout, 3),
                tag="peak",
                handle_in_s=round(hin, 3),
                handle_out_s=round(hout, 3),
            ))
            continue

        # 3. Breakdown: Half-time pacing (full bar hold)
        if is_breakdown:
            clip = pick_candidate(candidate_clips)
            cin, cout, hin, hout = pick_clip_in_with_handles(clip, bar_dur, tag="breakdown")
            bd_path = str(clip.get("path") or "")
            cuts.append(CutSlice(
                clip_path=bd_path,
                clip_name=Path(bd_path).name if bd_path else "breakdown",
                timeline_start_s=round(bar_start, 4),
                timeline_end_s=round(bar_end, 4),
                duration_s=round(bar_dur, 4),
                clip_in_s=round(cin, 3),
                clip_out_s=round(cout, 3),
                tag="breakdown",
                handle_in_s=round(hin, 3),
                handle_out_s=round(hout, 3),
            ))
            continue

        # 4. Standard bar: Driven by Chaos Knob with syncopation
        if chaos < 0.25:
            num_slices = 1
        elif chaos < 0.75:
            num_slices = 2 if rng.random() < (chaos * 1.5) else 1
        else:
            num_slices = 4 if rng.random() < 0.5 else 2

        slice_dur = bar_dur / num_slices
        for s in range(num_slices):
            t0 = bar_start + s * slice_dur
            t1 = t0 + slice_dur
            clip = pick_candidate(candidate_clips)
            cin, cout, hin, hout = pick_clip_in_with_handles(clip, slice_dur, tag="normal")
            c_path = str(clip.get("path") or "")
            cuts.append(CutSlice(
                clip_path=c_path,
                clip_name=Path(c_path).name if c_path else "clip",
                timeline_start_s=round(t0, 4),
                timeline_end_s=round(t1, 4),
                duration_s=round(slice_dur, 4),
                clip_in_s=round(cin, 3),
                clip_out_s=round(cout, 3),
                tag="normal",
                handle_in_s=round(hin, 3),
                handle_out_s=round(hout, 3),
            ))

    # Mark the final slice as outro
    if cuts:
        cuts[-1].tag = "outro"

    return cuts


def xml_media_id(path: Path | str, prefix: str = "file") -> str:
    norm = os.path.normcase(str(Path(path).resolve()))
    return prefix + "-" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:20]


def export_fcp7_xml(
    cuts: Sequence[CutSlice],
    audio_path: Path | str,
    output_xml_path: Path | str,
    fps: int | float = 30,
    width: int = 1920,
    height: int = 1080,
    sequence_name: str = "Love Sensation PMV",
) -> Path:
    """Export frame-quantized Final Cut Pro 7 / Premiere Pro XML with gapless continuity and stereo audio."""
    output_xml_path = Path(output_xml_path)
    audio_path = Path(audio_path)
    output_xml_path.parent.mkdir(parents=True, exist_ok=True)

    # Calculate timebase and NTSC flag
    try:
        float_fps = float(fps)
        if not math.isfinite(float_fps) or float_fps <= 0:
            float_fps = 30.0
    except (TypeError, ValueError):
        float_fps = 30.0
    if abs(float_fps - 23.976) < 0.05 or abs(float_fps - 23.98) < 0.05:
        timebase = 24
        ntsc_str = "TRUE"
    elif abs(float_fps - 29.97) < 0.05:
        timebase = 30
        ntsc_str = "TRUE"
    elif abs(float_fps - 59.94) < 0.05:
        timebase = 60
        ntsc_str = "TRUE"
    else:
        timebase = int(round(float_fps))
        ntsc_str = "FALSE"

    def to_pathurl(p: Path | str) -> str:
        resolved = str(Path(p).resolve()).replace("\\", "/")
        return "file://localhost/" + quote(resolved, safe="/")

    # Quantize timeline frames gaplessly: each cut starts strictly at previous end frame
    timeline_frame_spans = []
    curr_frame = 0
    for cut in cuts:
        start_f = curr_frame
        target_end_f = int(round(cut.timeline_end_s * float_fps))
        end_f = max(start_f + 1, target_end_f)
        dur_f = end_f - start_f
        curr_frame = end_f
        timeline_frame_spans.append((start_f, end_f, dur_f))

    total_frames = curr_frame if cuts else 0

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE xmeml>',
        '<xmeml version="4">',
        '  <sequence id="pmv-sequence">',
        f'    <name>{html.escape(sequence_name)}</name>',
        f'    <duration>{total_frames}</duration>',
        f'    <rate><timebase>{timebase}</timebase><ntsc>{ntsc_str}</ntsc></rate>',
        '    <media>',
        '      <video>',
        '        <format>',
        '          <samplecharacteristics>',
        f'            <rate><timebase>{timebase}</timebase><ntsc>{ntsc_str}</ntsc></rate>',
        f'            <width>{width}</width>',
        f'            <height>{height}</height>',
        '          </samplecharacteristics>',
        '        </format>',
        '        <track>',
    ]

    # Add video clipitems with deduplicated file nodes and transition handles
    seen_files: set[str] = set()
    for idx, cut in enumerate(cuts):
        clip_path = Path(cut.clip_path)
        start_frame, end_frame, dur_frames = timeline_frame_spans[idx]
        in_frame = int(round(cut.clip_in_s * float_fps))
        out_frame = in_frame + dur_frames
        file_dur = max(out_frame + int(round(cut.handle_out_s * float_fps)), dur_frames + 30)
        fid = xml_media_id(clip_path)

        # Cross Dissolve transition on breakdown entrances
        if cut.tag == "breakdown" and idx > 0:
            trans_dur = min(int(round(0.5 * float_fps)), dur_frames // 2, 16)
            if trans_dur >= 4:
                t_start = max(0, start_frame - (trans_dur // 2))
                t_end = t_start + trans_dur
                xml_lines.extend([
                    '          <transitionitem>',
                    f'            <start>{t_start}</start>',
                    f'            <end>{t_end}</end>',
                    '            <alignment>center</alignment>',
                    f'            <rate><timebase>{timebase}</timebase><ntsc>{ntsc_str}</ntsc></rate>',
                    '            <effect>',
                    '              <name>Cross Dissolve</name>',
                    '              <effectid>Cross Dissolve</effectid>',
                    '              <effectcategory>Dissolve</effectcategory>',
                    '              <effecttype>transition</effecttype>',
                    '              <mediatype>video</mediatype>',
                    '              <wipecode>0</wipecode>',
                    '              <wipeaccuracy>100</wipeaccuracy>',
                    '              <startratio>0</startratio>',
                    '              <endratio>1</endratio>',
                    '              <reverse>FALSE</reverse>',
                    '            </effect>',
                    '          </transitionitem>',
                ])

        if fid not in seen_files:
            seen_files.add(fid)
            fnode = [
                f'            <file id="{fid}">',
                f'              <name>{html.escape(cut.clip_name)}</name>',
                f'              <pathurl>{to_pathurl(clip_path)}</pathurl>',
                f'              <rate><timebase>{timebase}</timebase><ntsc>{ntsc_str}</ntsc></rate>',
                f'              <duration>{file_dur}</duration>',
                '              <media><video><samplecharacteristics>',
                f'                <width>{width}</width><height>{height}</height>',
                '              </samplecharacteristics></video></media>',
                '            </file>',
            ]
        else:
            fnode = [f'            <file id="{fid}"/>']

        xml_lines.extend([
            f'          <clipitem id="ci-{idx}">',
            f'            <name>{html.escape(cut.clip_name)} [{cut.tag.upper()}]</name>',
            f'            <duration>{dur_frames}</duration>',
            f'            <rate><timebase>{timebase}</timebase><ntsc>{ntsc_str}</ntsc></rate>',
            f'            <start>{start_frame}</start>',
            f'            <end>{end_frame}</end>',
            f'            <in>{in_frame}</in>',
            f'            <out>{out_frame}</out>',
            *fnode,
            '          </clipitem>',
        ])

    xml_lines.extend([
        '        </track>',
        '      </video>',
        '      <audio>',
        '        <track>',
        '          <clipitem id="song-item-1">',
        f'            <name>{html.escape(audio_path.name)} [L]</name>',
        f'            <duration>{total_frames}</duration>',
        f'            <rate><timebase>{timebase}</timebase><ntsc>{ntsc_str}</ntsc></rate>',
        '            <start>0</start>',
        f'            <end>{total_frames}</end>',
        '            <in>0</in>',
        f'            <out>{total_frames}</out>',
        '            <file id="song-file">',
        f'              <name>{html.escape(audio_path.name)}</name>',
        f'              <pathurl>{to_pathurl(audio_path)}</pathurl>',
        f'              <rate><timebase>{timebase}</timebase><ntsc>{ntsc_str}</ntsc></rate>',
        f'              <duration>{total_frames}</duration>',
        '              <media><audio><samplecharacteristics>',
        '                <depth>16</depth><samplerate>48000</samplerate><nbchannels>2</nbchannels>',
        '              </samplecharacteristics></audio></media>',
        '            </file>',
        '            <sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>',
        '          </clipitem>',
        '        </track>',
        '        <track>',
        '          <clipitem id="song-item-2">',
        f'            <name>{html.escape(audio_path.name)} [R]</name>',
        f'            <duration>{total_frames}</duration>',
        f'            <rate><timebase>{timebase}</timebase><ntsc>{ntsc_str}</ntsc></rate>',
        '            <start>0</start>',
        f'            <end>{total_frames}</end>',
        '            <in>0</in>',
        f'            <out>{total_frames}</out>',
        '            <file id="song-file"/>',
        '            <sourcetrack><mediatype>audio</mediatype><trackindex>2</trackindex></sourcetrack>',
        '          </clipitem>',
        '        </track>',
        '      </audio>',
        '    </media>',
    ])

    MARKER_COLORS = {
        "PEAK": "Gold",
        "DROP": "Magenta",
        "CHOKE": "Red",
        "FILL": "Orange",
        "BREAKDOWN": "Cyan",
        "OUTRO": "Purple",
        "INTRO": "Blue",
    }

    for idx, cut in enumerate(cuts):
        if cut.tag in {"drop", "peak", "choke", "fill", "breakdown"}:
            frame, _, _ = timeline_frame_spans[idx]
            tag_upper = cut.tag.upper()
            comment = f"Love Sensation PMV: {tag_upper} hit on {cut.clip_name}"
            color = MARKER_COLORS.get(tag_upper, "Green")
            xml_lines.extend([
                '    <marker>',
                f'      <name>{tag_upper}</name>',
                f'      <comment>{html.escape(comment)}</comment>',
                f'      <color>{color}</color>',
                f'      <in>{frame}</in>',
                '      <out>-1</out>',
                '    </marker>',
            ])

    xml_lines.extend([
        '  </sequence>',
        '</xmeml>',
    ])

    output_xml_path.write_text("\n".join(xml_lines), encoding="utf-8")
    return output_xml_path
