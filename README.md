# FrameCue

FrameCue is a portable static human-review runtime for media-aligned content.
It is the review gate between upstream content generation and downstream work
such as TTS, rendering, image regeneration, or publishing.

FrameCue v2 owns the review package, static viewer, draft isolation, final
approval, and full result export. It does not translate, synthesize speech,
retime media, render video, or call an AI provider.

## v2 Quick Start

Build the viewer once from this pinned source checkout:

```bash
npm install
npm run build
```

Build an immutable review bundle from a v2 source JSON:

```bash
python3 framecue.py build \
  --input package.source.json \
  --out-dir review-r1
```

The two content-first modes also have deterministic builders:

```bash
python3 framecue.py build-carousel \
  --cards-dir /path/to/cards \
  --review-id ep01-cards \
  --out-dir review-ep01-cards-r1

python3 framecue.py build-markdown \
  --article /path/to/article.md \
  --review-id ep01-article \
  --out-dir review-ep01-article-r1
```

`build-carousel` reviews only `slide-NN.png` files. One contact sheet and one
`mobile-audit*.png` are bundled as non-reviewable context. A whole-set note,
such as an ordering issue, belongs in the first Cue's free-text instruction.
`build-markdown`
reviews headings, paragraphs, and lists as independent Cues; YAML frontmatter
and the trailing `## 編輯備註` section remain read-only context.

The output is self-contained:

```text
review-r1/
  index.html
  assets/
  review_package.json
```

Serve it with byte-range support when video is present:

```bash
python3 framecue.py serve --dir review-r1 --port 3069
```

The reviewer exports one `framecue_review_result_v1` JSON snapshot. Validate
and retain an approved result before downstream work begins:

```bash
python3 framecue.py collect \
  --package review-r1/review_package.json \
  --result ~/Downloads/framecue_review_result.json \
  --out approved-review-result.json
```

`collect` rejects a stale checksum, wrong revision, incomplete snapshot, or
unapproved result.

## Contract

`framecue_package_v2` is immutable and records:

- stable `review_id`, immutable `revision`, viewer version, and SHA-256 content checksum;
- relative media assets, scenes, cues, optional semantic blocks, and pronunciation risks;
- punctuation policy, provenance, and previous-revision lineage.

`framecue_review_result_v1` is a complete snapshot of every cue and block.
Follow-up actions are restricted by the package workflow:

| Workflow | Actions |
|---|---|
| Subtitle, redraw, boundary, HyperFrames | `use_edit`, `rewrite`, `resegment`, `retime` |
| Image carousel | `use_edit`, `replace_asset`, `rewrite_copy`, `recrop`, `reorder` |
| Markdown | `use_edit`, `rewrite`, `cut`, `split`, `needs_source` |

The result schema lists the legal tokens because the result intentionally does
not repeat the workflow kind. The package-aware validator and `collect` enforce
the narrower per-mode vocabulary, so existing subtitle packages retain their
original validation behavior.

When blocks exist, they own meaning and `speech_text`; cues own review display
segmentation. Cue timings are read-only. Each block can be approved, then the
package receives one final approval. Any content edit invalidates final
approval.

Schemas live in [schemas](schemas/).

## Supported Workflows

v2 supports one contract across six review modes:

1. Cue and semantic-block subtitle review.
2. Redraw before/after review with generation trace.
3. Subtitle-boundary review.
4. HyperFrames playback review.
5. Image-carousel review with one independent Cue per image and no set verdict.
6. Traditional-Chinese Markdown review with one Cue per content block.

The viewer switches media stage mode without changing the review data model.
Risk and All are cue filters, not separate viewer implementations.

## Source Video

Subtitle packages can include the source video without exposing a local path to
the browser:

```json
{
  "media": {
    "video": {
      "source": "/path/to/source.mp4"
    }
  }
}
```

The CLI copies it into the immutable bundle, generates a bilingual WebVTT track,
and writes `media.video.src` and `media.video.captions` as bundle-relative paths.
In Video mode, selecting a Cue seeks to its start. `Play cue` stops at the Cue
end. Space marks the current Cue reviewed and selects the next Cue; once every
Cue in a valid semantic block is reviewed, FrameCue approves that block
automatically. The native controls remain available for scrubbing or continuous
playback.

## Cue Range Resegment

For a badly split sentence or protected name, click the first Cue and
Shift-click the last. FrameCue shows the selected range as a small timeline
with combined source, display, and speech text. `標記這段為重新切分` writes the
existing `resegment` action and one shared instruction to every selected Cue.
It does not alter timing or the immutable package; AgenticDub must create a new
revision before downstream work continues.

## Multi-Package Review

Build individual bundles first, then create a dashboard without shared drafts:

```bash
python3 framecue.py manifest \
  --out-dir review-dashboard \
  --item openclaw=/path/to/openclaw-review-r1 \
  --item computex=/path/to/computex-review-r1
```

Each package keeps its own review ID, revision, checksum-bound browser draft,
and exported result.

## HyperFrames

For a portable HyperFrames package, the source JSON declares a project bundle:

```json
{
  "media": {
    "hyperframes": {
      "source_dir": "/path/to/hyperframes",
      "config": "review-player.json"
    }
  }
}
```

FrameCue copies that directory and injects its generic player adapter. The
project provides only composition, narration, assets, and `review-player.json`.
The adapter and FrameCue viewer remain same-origin and synchronize by playback
time.

## v1 Compatibility

Existing v1 review directories remain usable because they already contain their
own viewer. FrameCue v2 intentionally does not load a v1 package in the v2
viewer.

Create a new v2 revision explicitly when needed:

```bash
python3 framecue.py migrate-v1 \
  --package old-review/review_package.json \
  --semantic-blocks old-review/semantic_blocks/semantic_blocks.json \
  --review-id openclaw-interpreter \
  --revision r1 \
  --out-dir openclaw-framecue-v2-r1
```

The frozen v1 builder remains available only for historical package recovery:

```bash
python3 framecue.py legacy-build -- --video input.mp4 --subtitle target.srt --out-dir v1-review
```

Its source lives in [legacy](legacy/); it is not the v2 runtime.

## Development Checks

```bash
npm run build
python3 -m unittest discover -s tests -v
python3 framecue.py self-check
```

The checks cover subtitle, redraw, boundary, HyperFrames, image-carousel, and
Markdown bundles, including workflow-specific result actions.

## Architecture Notes

- [FrameCue v2 refactor implementation note](docs/framecue-v2-refactor-implementation-note.md)
- [HyperFrames Review Player integration proposal](docs/hyperframes-review-integration.md)
- [HyperFrames Review Player implementation note](docs/hyperframes-review-implementation-note.md)
