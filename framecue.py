#!/usr/bin/env python3
import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path


def tool(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"{name} not found")
    return path


FFMPEG = tool("ffmpeg")

RISK_TERMS = [
    "還", "行", "重", "長", "著", "得", "了", "樂", "只", "少", "差", "傳",
    "OpenClaw", "Moltbook", "MCP", "GitHub", "2026", "160", "驚訝", "還有",
]


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, text=True, **kwargs)


def fmt_time(seconds):
    ms = round(float(seconds) * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_time(value):
    h, m, rest = value.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def parse_srt(path):
    cues = []
    blocks = re.split(r"\n\s*\n", Path(path).read_text(encoding="utf-8-sig").strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start, end = [parse_time(x.strip()) for x in lines[1].split("-->")]
        cues.append({
            "id": len(cues) + 1,
            "start": start,
            "end": end,
            "text": "\n".join(lines[2:]),
        })
    if not cues:
        raise SystemExit(f"no cues parsed from {path}")
    return cues


def scene_times(video, threshold):
    proc = run([
        FFMPEG, "-hide_banner", "-nostdin", "-i", str(video),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return [float(x) for x in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]


def nearest_boundary(t, cues, window):
    best = None
    for cue in cues:
        if cue["start"] - window <= t <= cue["end"] + window:
            for key in ("start", "end"):
                d = abs(t - cue[key])
                if d <= window and (best is None or d < best[0]):
                    best = (d, cue[key])
    return best[1] if best else t


def normalize_scenes(raw_times, cues, min_gap, snap_window, max_gap):
    cuts = [0.0]
    for t in raw_times:
        t = nearest_boundary(t, cues, snap_window)
        if t - cuts[-1] >= min_gap:
            cuts.append(t)
    # ponytail: sparse fallback only prevents dead review stretches; replace with smarter sampling if reviewers ask.
    end = max(cue["end"] for cue in cues)
    t = max_gap
    while t < end:
        if all(abs(t - cut) > min_gap for cut in cuts):
            cuts.append(t)
        t += max_gap
    cuts = sorted(set(round(max(0.0, t), 3) for t in cuts))
    scenes = []
    for i, start in enumerate(cuts):
        end_time = cuts[i + 1] if i + 1 < len(cuts) else end
        scenes.append({"id": i + 1, "start": start, "end": end_time})
    return scenes


def assign_scene(cue, scenes):
    midpoint = (cue["start"] + cue["end"]) / 2
    for scene in scenes:
        if scene["start"] <= midpoint < scene["end"]:
            return scene["id"]
    return scenes[-1]["id"]


def extract_frames(video, out_dir, scenes, offset):
    frames = out_dir / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    for scene in scenes:
        image = frames / f"scene_{scene['id']:04d}.jpg"
        scene["image"] = str(image.relative_to(out_dir))
        if image.exists() and image.stat().st_size > 1000:
            continue
        ts = max(0.0, scene["start"] + offset)
        run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{ts:.3f}", "-i", str(video),
            "-frames:v", "1", "-q:v", "3", str(image),
        ])


def pronunciation_risks(text):
    risks = [term for term in RISK_TERMS if term in text]
    if re.search(r"[A-Za-z]", text):
        risks.append("英文混中文")
    if re.search(r"\d", text):
        risks.append("數字")
    return list(dict.fromkeys(risks))


def extract_audio(audio_source, cue_audio_template, out_dir, cues):
    if not audio_source and not cue_audio_template:
        return
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    for cue in cues:
        audio = audio_dir / f"cue_{cue['id']:04d}.mp3"
        cue["audio"] = str(audio.relative_to(out_dir))
        if audio.exists() and audio.stat().st_size > 1000:
            continue
        if cue_audio_template:
            src = Path(cue_audio_template.format(id=cue["id"]))
            if not src.exists():
                raise SystemExit(f"cue audio missing: {src}")
            run([
                FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(src), "-vn", "-ac", "1", "-ar", "24000", "-b:a", "64k", str(audio),
            ])
            continue
        run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{cue['start']:.3f}", "-t", f"{max(0.05, cue['end'] - cue['start']):.3f}",
            "-i", str(audio_source), "-vn", "-ac", "1", "-ar", "24000", "-b:a", "64k", str(audio),
        ])


def add_original_text(cues, original_subtitle):
    if not original_subtitle:
        return
    originals = parse_srt(original_subtitle)
    by_id = {cue["id"]: cue for cue in originals}
    for cue in cues:
        cue["original_text"] = by_id.get(cue["id"], {}).get("text", "")


def write_package(video, subtitle, original_subtitle, out_dir, cues, scenes):
    for cue in cues:
        cue["scene_id"] = assign_scene(cue, scenes)
        cue["pronunciation_risks"] = pronunciation_risks(cue["text"])
    package = {
        "video": str(Path(video).resolve()),
        "subtitle": str(Path(subtitle).resolve()),
        "original_subtitle": str(Path(original_subtitle).resolve()) if original_subtitle else "",
        "scenes": scenes,
        "cues": cues,
    }
    (out_dir / "review_package.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    shutil.copy2(Path(__file__).with_name("viewer.html"), out_dir / "index.html")


def self_check():
    sample = "1\n00:00:01,000 --> 00:00:02,500\nhi\n\n2\n00:00:03,000 --> 00:00:04,000\nthere\n"
    tmp = Path("/tmp/subtitle_review_selfcheck.srt")
    tmp.write_text(sample, encoding="utf-8")
    cues = parse_srt(tmp)
    scenes = normalize_scenes([1.2, 1.4, 3.1], cues, 1.0, 0.25, 10.0)
    assert len(cues) == 2
    assert fmt_time(cues[0]["start"]) == "00:00:01,000"
    assert scenes[0]["start"] == 0.0
    assert assign_scene(cues[1], scenes) == scenes[-1]["id"]
    print("self-check ok")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video")
    parser.add_argument("--subtitle")
    parser.add_argument("--original-subtitle")
    parser.add_argument("--audio-source")
    parser.add_argument("--cue-audio-template")
    parser.add_argument("--out-dir")
    parser.add_argument("--threshold", type=float, default=0.30)
    parser.add_argument("--min-gap", type=float, default=2.0)
    parser.add_argument("--snap-window", type=float, default=0.25)
    parser.add_argument("--max-gap", type=float, default=45.0)
    parser.add_argument("--frame-offset", type=float, default=0.10)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if not args.video or not args.subtitle or not args.out_dir:
        parser.error("--video, --subtitle, and --out-dir are required")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cues = parse_srt(args.subtitle)
    add_original_text(cues, args.original_subtitle)
    scenes = normalize_scenes(
        scene_times(args.video, args.threshold),
        cues,
        args.min_gap,
        args.snap_window,
        args.max_gap,
    )
    extract_frames(args.video, out_dir, scenes, args.frame_offset)
    extract_audio(args.audio_source, args.cue_audio_template, out_dir, cues)
    write_package(args.video, args.subtitle, args.original_subtitle, out_dir, cues, scenes)
    print(out_dir / "index.html")
    print(f"cues={len(cues)} scenes={len(scenes)}")


if __name__ == "__main__":
    main()
