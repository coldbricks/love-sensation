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
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Sequence
from urllib.parse import quote
from .video_engine import probe_media_file, is_video_path


def frame_rate(fps: int | float | str) -> tuple[Fraction, int, str]:
    """Return the exact rate and xmeml timebase, keeping integer rates distinct."""
    try:
        rate = Fraction(str(fps))
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"Invalid frame rate: {fps}") from None
    aliases = {Fraction('23.976'): Fraction(24000, 1001), Fraction('23.98'): Fraction(24000, 1001),
               Fraction('29.97'): Fraction(30000, 1001), Fraction('59.94'): Fraction(60000, 1001)}
    rate = aliases.get(rate, rate)
    if rate <= 0 or rate > 240:
        raise ValueError(f"Unsupported frame rate: {fps}")
    if rate.denominator == 1:
        return rate, rate.numerator, "FALSE"
    if rate.denominator == 1001 and rate.numerator in {24000, 30000, 60000, 120000}:
        return rate, rate.numerator // 1000, "TRUE"
    raise ValueError(f"Unsupported variable or nonstandard frame rate: {fps}; conform the media first")


def prepare_candidate_clips(clips: Sequence[dict]) -> list[dict]:
    """Validate and probe real local candidates; never invent a source duration."""
    prepared = []
    for clip in clips:
        raw = clip.get("path") or clip.get("source")
        if not raw:
            raise ValueError("A candidate has no media path")
        path = Path(raw).resolve()
        if not path.is_file() or not is_video_path(path):
            raise ValueError(f"Video file is missing or unsupported: {path.name}")
        info = probe_media_file(path)
        if info.get("duration_s", 0) <= 0 or info.get("width", 0) <= 0:
            raise ValueError(f"Could not probe video: {path.name}")
        prepared.append({**clip, **info, "path": str(path), "source": str(path), "media_type": "video"})
    return prepared


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
    if not candidate_clips or audio_grid.get("reliable") is False:
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

    if not bars or not math.isfinite(nominal_total) or nominal_total <= 0:
        return []
    rate, _, _ = frame_rate(fps)
    total_duration = math.floor(nominal_total * rate + 1e-8) / float(rate)
    bars = sorted(set(b for b in bars if 0 <= b < total_duration))
    beats = sorted(set(b for b in beats if 0 <= b < total_duration))
    if not bars:
        return []
    for clip in candidate_clips:
        duration = float(clip.get("duration_s") or 0)
        if not math.isfinite(duration) or duration < 1 / float(rate):
            raise ValueError("Every candidate needs a valid duration of at least one timeline frame")

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
    first_beat = bars[0]

    # Optional intro pad before first beat
    if first_beat > 0:
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

    # A long requested hold is repeated in source-sized pieces rather than
    # reading beyond EOF. Recompute handles after fill offsets and splitting.
    source_durations = {str(c.get("path") or ""): float(c["duration_s"]) for c in candidate_clips}
    bounded = []
    cursor_frame = 0
    total_frames = int(round(total_duration * rate))
    for cut in cuts:
        duration = source_durations[cut.clip_path]
        end_frame = min(total_frames, int(round(cut.timeline_end_s * rate)))
        available_frames = max(1, math.floor(duration * rate + 1e-8))
        while cursor_frame < end_frame:
            frames = min(end_frame - cursor_frame, available_frames)
            cursor = cursor_frame / float(rate)
            span = frames / float(rate)
            cin = min(max(0.0, cut.clip_in_s), max(0.0, duration - span))
            bounded.append(replace(cut, timeline_start_s=cursor, timeline_end_s=cursor + span,
                duration_s=span, clip_in_s=cin, clip_out_s=cin + span,
                handle_in_s=min(.25, cin), handle_out_s=min(.25, max(0.0, duration - cin - span))))
            cursor_frame += frames
    if bounded:
        bounded[-1].tag = "outro"
    return bounded


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

    rate, timebase, ntsc_str = frame_rate(fps)
    float_fps = float(rate)
    if not cuts:
        raise ValueError("No timeline cuts to export")
    if not audio_path.is_file():
        raise ValueError("The analyzed soundtrack is missing")
    audio_info = probe_media_file(audio_path)
    if not audio_info.get("has_audio") or audio_info.get("duration_s", 0) <= 0:
        raise ValueError("The soundtrack has no readable audio stream")
    metadata = {}
    for cut in cuts:
        if cut.clip_path not in metadata:
            metadata[cut.clip_path] = prepare_candidate_clips([{"path": cut.clip_path}])[0]
        info = metadata[cut.clip_path]
        if not all(math.isfinite(v) for v in (cut.clip_in_s, cut.clip_out_s, cut.timeline_start_s, cut.timeline_end_s)):
            raise ValueError("Non-finite cut timing")
        if cut.clip_in_s < 0 or cut.clip_out_s > info["duration_s"] + 1e-6 or cut.clip_out_s <= cut.clip_in_s:
            raise ValueError(f"Cut extends outside source: {cut.clip_name}")
    if cuts[-1].timeline_end_s > audio_info["duration_s"] + 1e-6:
        raise ValueError("Timeline extends beyond the soundtrack")

    def to_pathurl(p: Path | str) -> str:
        resolved = str(Path(p).resolve()).replace("\\", "/")
        return "file://localhost/" + quote(resolved, safe="/")

    # Quantize timeline frames gaplessly: each cut starts strictly at previous end frame
    timeline_frame_spans = []
    curr_frame = 0
    for cut in cuts:
        start_f = curr_frame
        target_end_f = int(round(cut.timeline_end_s * float_fps))
        end_f = target_end_f
        if end_f <= start_f:
            raise ValueError("A timeline cut is shorter than one frame")
        if abs(cut.timeline_start_s * float_fps - start_f) > 1.01:
            raise ValueError("Timeline contains a gap or overlap")
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
        info = metadata[cut.clip_path]
        source_rate, source_timebase, source_ntsc = frame_rate(info.get("fps_ratio") or info["fps"])
        source_fps = float(source_rate)
        start_frame, end_frame, dur_frames = timeline_frame_spans[idx]
        file_dur = int(info.get("frame_count") or round(info["duration_s"] * source_fps))
        source_span = max(1, int(round((end_frame - start_frame) / float_fps * source_fps)))
        in_frame = min(int(round(cut.clip_in_s * source_fps)), max(0, file_dur - source_span))
        out_frame = in_frame + source_span
        if out_frame > file_dur:
            raise ValueError(f"Quantized cut exceeds source: {cut.clip_name}")
        fid = xml_media_id(clip_path)

        # Cross Dissolve transition on breakdown entrances
        if cut.tag == "breakdown" and idx > 0 and cut.handle_in_s >= .25 and cuts[idx - 1].handle_out_s >= .25:
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
                f'              <rate><timebase>{source_timebase}</timebase><ntsc>{source_ntsc}</ntsc></rate>',
                f'              <duration>{file_dur}</duration>',
                '              <media><video><samplecharacteristics>',
                f'                <width>{info["width"]}</width><height>{info["height"]}</height>',
                '              </samplecharacteristics></video></media>',
                '            </file>',
            ]
        else:
            fnode = [f'            <file id="{fid}"/>']

        xml_lines.extend([
            f'          <clipitem id="ci-{idx}">',
            f'            <name>{html.escape(cut.clip_name)} [{html.escape(cut.tag.upper())}]</name>',
            f'            <duration>{file_dur}</duration>',
            f'            <rate><timebase>{source_timebase}</timebase><ntsc>{source_ntsc}</ntsc></rate>',
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
    ])
    channels = int(audio_info.get("audio_channels", 0))
    sample_rate = int(audio_info.get("audio_sample_rate", 0))
    if channels < 1 or sample_rate < 1:
        raise ValueError("Soundtrack channel count or sample rate is unavailable")
    for channel in range(1, channels + 1):
        label = ("L" if channel == 1 else "R") if channels == 2 else f"CH {channel}"
        xml_lines.extend([
            '        <track>',
            f'          <clipitem id="song-item-{channel}">',
            f'            <name>{html.escape(audio_path.name)} [{label}]</name>',
            f'            <duration>{total_frames}</duration>',
            f'            <rate><timebase>{timebase}</timebase><ntsc>{ntsc_str}</ntsc></rate>',
            '            <start>0</start>', f'            <end>{total_frames}</end>',
            '            <in>0</in>', f'            <out>{total_frames}</out>',
        ])
        if channel == 1:
            xml_lines.extend([
                '            <file id="song-file">',
                f'              <name>{html.escape(audio_path.name)}</name>',
                f'              <pathurl>{to_pathurl(audio_path)}</pathurl>',
                f'              <rate><timebase>{timebase}</timebase><ntsc>{ntsc_str}</ntsc></rate>',
                f'              <duration>{int(audio_info["duration_s"] * float_fps)}</duration>',
                '              <media><audio><samplecharacteristics>',
                f'                <samplerate>{sample_rate}</samplerate><nbchannels>{channels}</nbchannels>',
                '              </samplecharacteristics></audio></media>',
                '            </file>',
            ])
        else:
            xml_lines.append('            <file id="song-file"/>')
        xml_lines.extend([
            f'            <sourcetrack><mediatype>audio</mediatype><trackindex>{channel}</trackindex></sourcetrack>',
            '          </clipitem>', '        </track>',
        ])
    xml_lines.extend([
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
