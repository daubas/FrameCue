# FrameCue

FrameCue is a small offline subtitle review tool for dubbed video workflows.

It builds a portable browser review package from:

- a video
- target subtitles
- optional original-language subtitles
- optional per-cue TTS audio

The generated viewer lets reviewers step through cues with representative scene frames, inspect bilingual subtitle overlays, play each cue audio, add prompt notes, edit subtitles, and export a change list.

## Requirements

- Python 3.9+
- FFmpeg available on `PATH`

No Python packages are required.

## Build A Review Package

```bash
./framecue.py \
  --video input.mp4 \
  --subtitle target.srt \
  --original-subtitle original.srt \
  --cue-audio-template 'audio/seg_{id:04d}.wav' \
  --out-dir review
```

Then serve the folder:

```bash
cd review
python3 -m http.server 8000
```

Open:

```text
http://localhost:8000
```

## Audio Options

Prefer per-cue audio when available:

```bash
--cue-audio-template 'audio/seg_{id:04d}.wav'
```

Fallback to cutting from a full audio track:

```bash
--audio-source dubbed_audio.wav
```

Per-cue audio is more accurate for pronunciation review.

## Hotkeys

- `←` / `↑`: previous cue
- `→` / `↓`: next cue
- `Space`: play or pause current cue

## Outputs

The review package contains:

- `index.html`
- `review_package.json`
- `frames/*.jpg`
- `audio/*.mp3` when audio is provided

The browser viewer can download:

- `edited_subtitles.srt`
- `subtitle_change_list.json`
- `edited_review_package.json`

`subtitle_change_list.json` includes only cues with changed subtitles or prompt notes.

## Name

FrameCue means: review each subtitle cue against a representative video frame.
