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

## Review Multiple Videos

Put each review package in its own folder and add `framecue_manifest.json` beside `index.html`:

```json
{
  "items": [
    {
      "id": "openclaw",
      "label": "OpenClaw",
      "review_package": "openclaw/review_package.json",
      "semantic_blocks": "openclaw/semantic_blocks/semantic_blocks.json"
    },
    {
      "id": "video-2",
      "label": "Video 2",
      "review_package": "video-2/review_package.json",
      "semantic_blocks": null
    }
  ]
}
```

FrameCue adds a review-file selector. Frames and audio are resolved relative to each `review_package.json`; browser drafts and exported filenames are kept separate by item `id`.

## Review A HyperFrames Timeline

Add an optional `review_player` object to a manifest item when the review package has a same-origin HyperFrames Review Player:

```json
{
  "items": [
    {
      "id": "computex-2026",
      "label": "COMPUTEX 2026",
      "review_package": "review/framecue/review_package.json",
      "semantic_blocks": "review/framecue/semantic_blocks/semantic_blocks.json",
      "review_player": {
        "type": "hyperframes",
        "src": "hyperframes/player.html"
      }
    }
  ]
}
```

The `review_player.src` path is relative to the FrameCue page. Serve a common project root so the FrameCue page and Player share one origin. The viewer keeps Still as the default and shows a Still/Video control only for items with a valid Player; Cue and Block editing stay available in both stage modes.

Use a static server that supports HTTP byte-range requests. Range support is required for reliable seeking in the Player's narration audio; Python 3.9's basic `http.server` is not sufficient for this mode. For example:

```bash
miniserve --interfaces 127.0.0.1 --port 3073 /path/to/project-root
```

Player configuration and the same-origin message contract are documented in the [implementation note](docs/hyperframes-review-implementation-note.md).

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

## Architecture Notes

- [HyperFrames Review Player integration proposal](docs/hyperframes-review-integration.md)
- [HyperFrames Review Player implementation note](docs/hyperframes-review-implementation-note.md)
