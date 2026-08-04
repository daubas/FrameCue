#!/usr/bin/env python3
"""Build, validate, migrate, and collect immutable FrameCue v2 bundles."""

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
ADAPTER_PATH = ROOT / "adapters" / "hyperframes-player.html"
VIEWER_VERSION = "2.4.0"
PACKAGE_SCHEMA = "framecue_package_v2"
RESULT_SCHEMA = "framecue_review_result_v1"
MANIFEST_SCHEMA = "framecue_manifest_v2"
WORKFLOW_ACTIONS = {
    "subtitle": {"use_edit", "rewrite", "resegment", "retime"},
    "redraw": {"use_edit", "rewrite", "resegment", "retime"},
    "boundary": {"use_edit", "rewrite", "resegment", "retime"},
    "hyperframes": {"use_edit", "rewrite", "resegment", "retime"},
    "image_carousel": {"use_edit", "replace_asset", "rewrite_copy", "recrop", "reorder"},
    "markdown": {"use_edit", "rewrite", "cut", "split", "needs_source"},
}
WORKFLOW_KINDS = set(WORKFLOW_ACTIONS)
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SLIDE_PATTERN = re.compile(r"^slide-(\d+)\.png$", re.IGNORECASE)
LIST_PATTERN = re.compile(r"^\s*(?:[-*+] |\d+\. )")
HORIZONTAL_RULE_PATTERN = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")


class FrameCueError(ValueError):
    pass


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FrameCueError(f"file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise FrameCueError(f"invalid JSON in {path}: {error}") from error


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_key(value):
    return "".join(
        char for char in unicodedata.normalize("NFKC", value or "").casefold()
        if not char.isspace() and unicodedata.category(char)[0] not in {"P", "Z"}
    )


def block_content_error(block, cue_by_id):
    joined = " ".join(cue_by_id[cue_id]["text"] for cue_id in block["cue_ids"])
    if content_key(block["target_text"]) != content_key(joined):
        return f"block {block['id']} target_text does not match its cues"
    if content_key(block["speech_text"]) != content_key(block["target_text"]):
        return f"block {block['id']} speech_text does not match target_text"
    return ""


def package_checksum(package):
    payload = copy.deepcopy(package)
    payload.pop("content_checksum", None)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def as_text(value, label):
    if not isinstance(value, str):
        raise FrameCueError(f"{label} must be a string")
    return value


def as_ms(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FrameCueError(f"{label} must be milliseconds")
    rounded = round(value)
    if rounded < 0 or abs(value - rounded) > 0.0001:
        raise FrameCueError(f"{label} must be a non-negative integer millisecond value")
    return int(rounded)


def source_ms(row, key, label):
    if key in row and row[key] is not None:
        return as_ms(row[key], f"{label}.{key}")
    legacy_key = key.removesuffix("_ms")
    if legacy_key in row:
        value = row[legacy_key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FrameCueError(f"{label}.{legacy_key} must be seconds")
        return round(float(value) * 1000)
    raise FrameCueError(f"{label} is missing {key}")


def ensure_id(value, label):
    value = as_text(str(value), label)
    if not ID_PATTERN.fullmatch(value):
        raise FrameCueError(f"{label} must match {ID_PATTERN.pattern}: {value!r}")
    return value


def migrated_id(prefix, value, fallback):
    raw = str(value if value is not None else fallback)
    if raw.isdigit():
        return f"{prefix}{int(raw):04d}"
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", raw):
        return raw
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")
    return f"{prefix}-{slug or fallback}"


def ensure_list(value, label):
    if not isinstance(value, list):
        raise FrameCueError(f"{label} must be an array")
    return value


def markdown_parts(value):
    lines = str(value).splitlines()
    frontmatter = ""
    if lines[:1] == ["---"]:
        try:
            end = lines.index("---", 1)
        except ValueError as error:
            raise FrameCueError("Markdown frontmatter is missing its closing ---") from error
        frontmatter = "\n".join(lines[:end + 1])
        lines = lines[end + 1:]

    notes_at = next(
        (index for index, line in enumerate(lines) if line.strip().startswith("## 編輯備註")),
        None,
    )
    editorial_notes = ""
    if notes_at is not None:
        editorial_notes = "\n".join(lines[notes_at:]).strip()
        lines = lines[:notes_at]
    while lines and (not lines[-1].strip() or HORIZONTAL_RULE_PATTERN.fullmatch(lines[-1])):
        lines.pop()

    blocks = []
    current = []
    current_kind = ""

    def flush():
        nonlocal current, current_kind
        text = "\n".join(current).strip()
        if text:
            blocks.append({"kind": current_kind or "paragraph", "text": text})
        current = []
        current_kind = ""

    for line in lines:
        if not line.strip():
            flush()
            continue
        if HORIZONTAL_RULE_PATTERN.fullmatch(line):
            flush()
            continue
        if re.match(r"^#{1,6}\s+", line):
            flush()
            blocks.append({"kind": "heading", "text": line.strip()})
            continue
        if LIST_PATTERN.match(line):
            if current_kind != "list":
                flush()
                current_kind = "list"
            current.append(line)
            continue
        if current_kind == "list" or current_kind == "paragraph":
            current.append(line)
        else:
            flush()
            current_kind = "paragraph"
            current.append(line)
    flush()
    if not blocks:
        raise FrameCueError("Markdown article has no reviewable content blocks")
    return frontmatter, editorial_notes, blocks


def markdown_source(article_path, review_id, revision="r1", label=""):
    article_path = Path(article_path).expanduser().resolve()
    try:
        article = article_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise FrameCueError(f"file not found: {article_path}") from error
    frontmatter, editorial_notes, blocks = markdown_parts(article)
    scenes = []
    cues = []
    for index, block in enumerate(blocks, 1):
        start = (index - 1) * 1000
        scene_id = f"s{index:04d}"
        cue_id = f"c{index:04d}"
        scenes.append({"id": scene_id, "start_ms": start, "end_ms": start + 1000, "image": ""})
        cues.append({
            "id": cue_id,
            "start_ms": start,
            "end_ms": start + 1000,
            "scene_id": scene_id,
            "text": block["text"],
            "speech_text": block["text"],
            "original_text": "",
            "markdown": {"kind": block["kind"]},
        })
    return {
        "review_id": review_id,
        "revision": revision,
        "workflow": {"kind": "markdown", "label": label or article_path.stem},
        "subtitle_policy": {"display_punctuation": "preserved", "speech_punctuation": "preserved"},
        "media": {"markdown": {
            "source_name": article_path.name,
            "frontmatter": frontmatter,
            "editorial_notes": editorial_notes,
        }},
        "scenes": scenes,
        "cues": cues,
        "blocks": [],
        "provenance": {"source_article": str(article_path)},
    }


def carousel_source(cards_dir, review_id, revision="r1", label=""):
    cards_dir = Path(cards_dir).expanduser().resolve()
    if not cards_dir.is_dir():
        raise FrameCueError(f"cards directory not found: {cards_dir}")
    slides = sorted(
        (path for path in cards_dir.iterdir() if path.is_file() and SLIDE_PATTERN.fullmatch(path.name)),
        key=lambda path: int(SLIDE_PATTERN.fullmatch(path.name).group(1)),
    )
    if not slides:
        raise FrameCueError("cards directory has no slide-NN.png files")
    contact_sheets = sorted(cards_dir.glob("contact-sheet*.png"))
    mobile_audits = sorted(cards_dir.glob("mobile-audit*.png"))
    if len(contact_sheets) != 1 or len(mobile_audits) != 1:
        raise FrameCueError("cards directory must contain one contact-sheet*.png and one mobile-audit*.png")

    scenes = []
    cues = []
    for index, slide in enumerate(slides, 1):
        start = (index - 1) * 1000
        scene_id = f"s{index:04d}"
        cue_id = f"c{index:04d}"
        scenes.append({"id": scene_id, "start_ms": start, "end_ms": start + 1000, "image": slide.name})
        cues.append({
            "id": cue_id,
            "start_ms": start,
            "end_ms": start + 1000,
            "scene_id": scene_id,
            "text": slide.name,
            "speech_text": slide.name,
            "original_text": "",
        })
    return {
        "review_id": review_id,
        "revision": revision,
        "workflow": {"kind": "image_carousel", "label": label or cards_dir.parent.name},
        "subtitle_policy": {"display_punctuation": "preserved", "speech_punctuation": "preserved"},
        "media": {"carousel": {
            "contact_sheet": contact_sheets[0].name,
            "mobile_audit": mobile_audits[0].name,
        }},
        "scenes": scenes,
        "cues": cues,
        "blocks": [],
        "provenance": {"source_cards_dir": str(cards_dir)},
    }


def relative_path(value, label):
    value = as_text(value, label)
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise FrameCueError(f"{label} must be a non-empty bundle-relative path")
    return path.as_posix()


def asset_exists(root, value, label):
    relative = relative_path(value, label)
    candidate = (Path(root) / relative).resolve()
    try:
        candidate.relative_to(Path(root).resolve())
    except ValueError as error:
        raise FrameCueError(f"{label} escapes the bundle: {value}") from error
    if not candidate.is_file():
        raise FrameCueError(f"{label} is missing from bundle: {relative}")
    return relative


def nested_asset_paths(row, label):
    paths = []
    for nested_key in ("redraw", "boundary"):
        nested = row.get(nested_key)
        if not nested:
            continue
        if not isinstance(nested, dict):
            raise FrameCueError(f"{label}.{nested_key} must be an object")
        for key in ("before_image", "after_image", "comparison_image", "current_image"):
            if nested.get(key):
                paths.append((nested, key, f"{label}.{nested_key}.{key}"))
    return paths


def validate_package(package, package_dir=None, check_assets=True):
    if not isinstance(package, dict):
        raise FrameCueError("package must be an object")
    if package.get("schema_version") != PACKAGE_SCHEMA:
        raise FrameCueError(f"package.schema_version must be {PACKAGE_SCHEMA}")
    ensure_id(package.get("review_id", ""), "package.review_id")
    revision = as_text(package.get("revision", ""), "package.revision")
    if not re.fullmatch(r"r[1-9][0-9]*", revision):
        raise FrameCueError("package.revision must look like r1")
    as_text(package.get("viewer_version", ""), "package.viewer_version")
    checksum = as_text(package.get("content_checksum", ""), "package.content_checksum")
    if not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise FrameCueError("package.content_checksum must be a SHA-256 hex digest")
    if checksum != package_checksum(package):
        raise FrameCueError("package.content_checksum does not match package content")

    workflow = package.get("workflow")
    if not isinstance(workflow, dict) or workflow.get("kind") not in WORKFLOW_KINDS:
        raise FrameCueError("package.workflow.kind is invalid")
    workflow_kind = workflow["kind"]
    policy = package.get("subtitle_policy")
    if not isinstance(policy, dict) or not policy.get("display_punctuation") or not policy.get("speech_punctuation"):
        raise FrameCueError("package.subtitle_policy is incomplete")
    if not isinstance(package.get("media", {}), dict):
        raise FrameCueError("package.media must be an object")
    lineage = package.get("lineage")
    if not isinstance(lineage, dict) or "previous_package_checksum" not in lineage or "previous_result" not in lineage:
        raise FrameCueError("package.lineage is incomplete")

    scenes = ensure_list(package.get("scenes"), "package.scenes")
    cues = ensure_list(package.get("cues"), "package.cues")
    blocks = ensure_list(package.get("blocks"), "package.blocks")
    if not cues:
        raise FrameCueError("package.cues must not be empty")

    scene_ids = set()
    for index, scene in enumerate(scenes):
        label = f"package.scenes[{index}]"
        if not isinstance(scene, dict):
            raise FrameCueError(f"{label} must be an object")
        scene_id = ensure_id(scene.get("id", ""), f"{label}.id")
        if scene_id in scene_ids:
            raise FrameCueError(f"duplicate scene id: {scene_id}")
        scene_ids.add(scene_id)
        start = as_ms(scene.get("start_ms"), f"{label}.start_ms")
        end = as_ms(scene.get("end_ms"), f"{label}.end_ms")
        if end < start:
            raise FrameCueError(f"{label}.end_ms must not precede start_ms")
        image = as_text(scene.get("image", ""), f"{label}.image")
        if not image and workflow_kind != "markdown":
            raise FrameCueError(f"{label}.image must not be empty")
        if check_assets and image:
            asset_exists(package_dir, image, f"{label}.image")
            for nested, key, asset_label in nested_asset_paths(scene, label):
                asset_exists(package_dir, nested[key], asset_label)

    cue_ids = set()
    for index, cue in enumerate(cues):
        label = f"package.cues[{index}]"
        if not isinstance(cue, dict):
            raise FrameCueError(f"{label} must be an object")
        cue_id = ensure_id(cue.get("id", ""), f"{label}.id")
        if cue_id in cue_ids:
            raise FrameCueError(f"duplicate cue id: {cue_id}")
        cue_ids.add(cue_id)
        start = as_ms(cue.get("start_ms"), f"{label}.start_ms")
        end = as_ms(cue.get("end_ms"), f"{label}.end_ms")
        if end < start:
            raise FrameCueError(f"{label}.end_ms must not precede start_ms")
        if cue.get("scene_id") not in scene_ids:
            raise FrameCueError(f"{label}.scene_id does not exist")
        as_text(cue.get("text", ""), f"{label}.text")
        as_text(cue.get("speech_text", ""), f"{label}.speech_text")
        if cue.get("audio") and check_assets:
            asset_exists(package_dir, cue["audio"], f"{label}.audio")
        risks = cue.get("risks", [])
        if not isinstance(risks, list) or not all(isinstance(item, str) for item in risks):
            raise FrameCueError(f"{label}.risks must be a string array")
        markdown = cue.get("markdown")
        if workflow_kind == "markdown":
            if not isinstance(markdown, dict) or markdown.get("kind") not in {"paragraph", "heading", "list"}:
                raise FrameCueError(f"{label}.markdown.kind is invalid")
        if check_assets:
            for nested, key, asset_label in nested_asset_paths(cue, label):
                asset_exists(package_dir, nested[key], asset_label)

    block_ids = set()
    cue_by_id = {cue["id"]: cue for cue in cues}
    for index, block in enumerate(blocks):
        label = f"package.blocks[{index}]"
        if not isinstance(block, dict):
            raise FrameCueError(f"{label} must be an object")
        block_id = ensure_id(block.get("id", ""), f"{label}.id")
        if block_id in block_ids:
            raise FrameCueError(f"duplicate block id: {block_id}")
        block_ids.add(block_id)
        start = as_ms(block.get("start_ms"), f"{label}.start_ms")
        end = as_ms(block.get("end_ms"), f"{label}.end_ms")
        if end < start:
            raise FrameCueError(f"{label}.end_ms must not precede start_ms")
        cue_refs = ensure_list(block.get("cue_ids"), f"{label}.cue_ids")
        if not cue_refs or any(str(cue_id) not in cue_ids for cue_id in cue_refs):
            raise FrameCueError(f"{label}.cue_ids must refer to package cues")
        for key in ("source_text", "target_text", "speech_text"):
            as_text(block.get(key, ""), f"{label}.{key}")
        if "budget_ms" in block:
            as_ms(block["budget_ms"], f"{label}.budget_ms")
        content_error = block_content_error(block, cue_by_id)
        if content_error:
            raise FrameCueError(content_error)

    hyperframes = package["media"].get("hyperframes")
    if hyperframes:
        if not isinstance(hyperframes, dict):
            raise FrameCueError("package.media.hyperframes must be an object")
        if check_assets:
            asset_exists(package_dir, hyperframes.get("entry", ""), "package.media.hyperframes.entry")
            config = hyperframes.get("config")
            if config:
                entry_dir = Path(hyperframes["entry"]).parent
                asset_exists(package_dir, (entry_dir / config).as_posix(), "package.media.hyperframes.config")

    video = package["media"].get("video")
    if video:
        if not isinstance(video, dict):
            raise FrameCueError("package.media.video must be an object")
        as_text(video.get("src", ""), "package.media.video.src")
        as_text(video.get("captions", ""), "package.media.video.captions")
        if check_assets:
            asset_exists(package_dir, video["src"], "package.media.video.src")
            asset_exists(package_dir, video["captions"], "package.media.video.captions")

    if workflow_kind == "image_carousel":
        carousel = package["media"].get("carousel")
        if not isinstance(carousel, dict):
            raise FrameCueError("package.media.carousel must be an object")
        if len(scenes) != len(cues) or len({cue["scene_id"] for cue in cues}) != len(cues):
            raise FrameCueError("image carousel must contain one independent cue per scene")
        for key in ("contact_sheet", "mobile_audit"):
            as_text(carousel.get(key, ""), f"package.media.carousel.{key}")
            if check_assets:
                asset_exists(package_dir, carousel[key], f"package.media.carousel.{key}")

    if workflow_kind == "markdown":
        markdown = package["media"].get("markdown")
        if not isinstance(markdown, dict):
            raise FrameCueError("package.media.markdown must be an object")
        for key in ("source_name", "frontmatter", "editorial_notes"):
            as_text(markdown.get(key, ""), f"package.media.markdown.{key}")
        if len(scenes) != len(cues) or len({cue["scene_id"] for cue in cues}) != len(cues):
            raise FrameCueError("Markdown mode must contain one cue per content block")

    return {
        "review_id": package["review_id"],
        "revision": package["revision"],
        "cue_count": len(cues),
        "block_count": len(blocks),
        "workflow": workflow_kind,
    }


def validate_result(result, package, require_approved=False):
    if not isinstance(result, dict):
        raise FrameCueError("result must be an object")
    if result.get("schema_version") != RESULT_SCHEMA:
        raise FrameCueError(f"result.schema_version must be {RESULT_SCHEMA}")
    for key, package_key in (("review_id", "review_id"), ("revision", "revision"), ("package_checksum", "content_checksum")):
        if result.get(key) != package.get(package_key):
            raise FrameCueError(f"result.{key} does not match package")
    if result.get("viewer_version") != package.get("viewer_version"):
        raise FrameCueError("result.viewer_version does not match package")
    status = result.get("status")
    if status not in {"draft", "approved"}:
        raise FrameCueError("result.status must be draft or approved")
    if require_approved and status != "approved":
        raise FrameCueError("result is not finally approved")
    if status == "approved" and not result.get("approved_at"):
        raise FrameCueError("approved result is missing approved_at")

    result_cues = ensure_list(result.get("cues"), "result.cues")
    result_blocks = ensure_list(result.get("blocks"), "result.blocks")
    actions = WORKFLOW_ACTIONS[package["workflow"]["kind"]]
    expected_cues = {cue["id"] for cue in package["cues"]}
    expected_blocks = {block["id"] for block in package["blocks"]}
    cue_ids = [as_text(row.get("id", ""), "result.cues[].id") for row in result_cues if isinstance(row, dict)]
    block_ids = [as_text(row.get("id", ""), "result.blocks[].id") for row in result_blocks if isinstance(row, dict)]
    if len(cue_ids) != len(result_cues) or set(cue_ids) != expected_cues or len(set(cue_ids)) != len(cue_ids):
        raise FrameCueError("result.cues must be a complete unique snapshot")
    if len(block_ids) != len(result_blocks) or set(block_ids) != expected_blocks or len(set(block_ids)) != len(block_ids):
        raise FrameCueError("result.blocks must be a complete unique snapshot")

    for row in result_cues:
        if not isinstance(row, dict):
            raise FrameCueError("result.cues entries must be objects")
        as_text(row.get("text", ""), "result.cues[].text")
        as_text(row.get("speech_text", ""), "result.cues[].speech_text")
        if row.get("action") not in actions:
            raise FrameCueError(f"result.cues[].action is invalid for {package['workflow']['kind']}")
        as_text(row.get("instruction", ""), "result.cues[].instruction")
    for row in result_blocks:
        if not isinstance(row, dict):
            raise FrameCueError("result.blocks entries must be objects")
        as_text(row.get("target_text", ""), "result.blocks[].target_text")
        as_text(row.get("speech_text", ""), "result.blocks[].speech_text")
        if row.get("action") not in actions:
            raise FrameCueError(f"result.blocks[].action is invalid for {package['workflow']['kind']}")
        as_text(row.get("instruction", ""), "result.blocks[].instruction")
        if not isinstance(row.get("approved"), bool):
            raise FrameCueError("result.blocks[].approved must be boolean")
    if status == "approved" and expected_blocks and not all(row["approved"] for row in result_blocks):
        raise FrameCueError("all blocks must be approved before final approval")
    if status == "approved":
        package_blocks = {row["id"]: row for row in package["blocks"]}
        result_cues_by_id = {row["id"]: row for row in result_cues}
        for row in result_blocks:
            content_error = block_content_error(
                {**row, "cue_ids": package_blocks[row["id"]]["cue_ids"]},
                result_cues_by_id,
            )
            if content_error:
                raise FrameCueError(content_error)
    return {
        "review_id": result["review_id"],
        "revision": result["revision"],
        "status": status,
        "cue_count": len(result_cues),
        "block_count": len(result_blocks),
    }


def normalize_source(source, viewer_version=VIEWER_VERSION):
    if not isinstance(source, dict):
        raise FrameCueError("build input must be a JSON object")
    source = copy.deepcopy(source)
    review_id = ensure_id(str(source.get("review_id", "")), "source.review_id")
    revision = as_text(source.get("revision", ""), "source.revision")
    if not re.fullmatch(r"r[1-9][0-9]*", revision):
        raise FrameCueError("source.revision must look like r1")
    workflow = source.get("workflow", {})
    if isinstance(workflow, str):
        workflow = {"kind": workflow}
    if not isinstance(workflow, dict) or workflow.get("kind") not in WORKFLOW_KINDS:
        raise FrameCueError("source.workflow.kind is invalid")
    policy = source.get("subtitle_policy") or {
        "display_punctuation": "stripped_before_framecue",
        "speech_punctuation": "preserved",
    }
    if not isinstance(policy, dict):
        raise FrameCueError("source.subtitle_policy must be an object")
    policy.setdefault("display_punctuation", "stripped_before_framecue")
    policy.setdefault("speech_punctuation", "preserved")

    raw_scenes = ensure_list(source.get("scenes"), "source.scenes")
    raw_cues = ensure_list(source.get("cues"), "source.cues")
    if not raw_cues:
        raise FrameCueError("source.cues must not be empty")
    scenes = []
    scene_map = {}
    for index, row in enumerate(raw_scenes, 1):
        if not isinstance(row, dict):
            raise FrameCueError(f"source.scenes[{index}] must be an object")
        old_id = str(row.get("id", index))
        scene_id = ensure_id(old_id, f"source.scenes[{index}].id")
        if old_id in scene_map:
            raise FrameCueError(f"duplicate scene id: {old_id}")
        scene_map[old_id] = scene_id
        item = copy.deepcopy(row)
        item.update({
            "id": scene_id,
            "start_ms": source_ms(row, "start_ms", f"source.scenes[{index}]"),
            "end_ms": source_ms(row, "end_ms", f"source.scenes[{index}]"),
            "image": as_text(row.get("image", ""), f"source.scenes[{index}].image"),
        })
        item.pop("start", None)
        item.pop("end", None)
        scenes.append(item)

    cues = []
    cue_map = {}
    for index, row in enumerate(raw_cues, 1):
        if not isinstance(row, dict):
            raise FrameCueError(f"source.cues[{index}] must be an object")
        old_id = str(row.get("id", index))
        cue_id = ensure_id(old_id, f"source.cues[{index}].id")
        if old_id in cue_map:
            raise FrameCueError(f"duplicate cue id: {old_id}")
        cue_map[old_id] = cue_id
        old_scene_id = str(row.get("scene_id", ""))
        if old_scene_id not in scene_map:
            raise FrameCueError(f"source.cues[{index}].scene_id does not exist")
        item = copy.deepcopy(row)
        item.update({
            "id": cue_id,
            "start_ms": source_ms(row, "start_ms", f"source.cues[{index}]"),
            "end_ms": source_ms(row, "end_ms", f"source.cues[{index}]"),
            "scene_id": scene_map[old_scene_id],
            "text": as_text(row.get("text", ""), f"source.cues[{index}].text"),
            "speech_text": as_text(row.get("speech_text", row.get("text", "")), f"source.cues[{index}].speech_text"),
            "original_text": str(row.get("original_text", "")),
            "risks": list(row.get("risks", row.get("pronunciation_risks", [])) or []),
        })
        if not all(isinstance(risk, str) for risk in item["risks"]):
            raise FrameCueError(f"source.cues[{index}].risks must be strings")
        item.pop("start", None)
        item.pop("end", None)
        item.pop("pronunciation_risks", None)
        cues.append(item)

    blocks = []
    raw_blocks = source.get("blocks", [])
    if not isinstance(raw_blocks, list):
        raise FrameCueError("source.blocks must be an array")
    for index, row in enumerate(raw_blocks, 1):
        if not isinstance(row, dict):
            raise FrameCueError(f"source.blocks[{index}] must be an object")
        cue_refs = row.get("cue_ids") or row.get("input_ids") or row.get("source_cue_ids") or []
        if not isinstance(cue_refs, list):
            raise FrameCueError(f"source.blocks[{index}].cue_ids must be an array")
        item = copy.deepcopy(row)
        item.update({
            "id": ensure_id(str(row.get("id", f"b{index:04d}")), f"source.blocks[{index}].id"),
            "cue_ids": [cue_map.get(str(cue_id), str(cue_id)) for cue_id in cue_refs],
            "start_ms": source_ms(row, "start_ms", f"source.blocks[{index}]"),
            "end_ms": source_ms(row, "end_ms", f"source.blocks[{index}]"),
            "source_text": str(row.get("source_text", "")),
            "target_text": str(row.get("target_text", "")),
            "speech_text": str(row.get("speech_text", row.get("target_text", ""))),
        })
        item.setdefault("budget_ms", max(0, item["end_ms"] - item["start_ms"]))
        item.pop("start", None)
        item.pop("end", None)
        item.pop("input_ids", None)
        item.pop("source_cue_ids", None)
        blocks.append(item)

    package = {
        "schema_version": PACKAGE_SCHEMA,
        "review_id": review_id,
        "revision": revision,
        "viewer_version": source.get("viewer_version", viewer_version),
        "workflow": workflow,
        "subtitle_policy": policy,
        "media": copy.deepcopy(source.get("media", {})),
        "scenes": scenes,
        "cues": cues,
        "blocks": blocks,
        "risks": copy.deepcopy(source.get("risks", [])),
        "provenance": copy.deepcopy(source.get("provenance", {})),
        "lineage": copy.deepcopy(source.get("lineage", {
            "previous_package_checksum": "",
            "previous_result": "",
        })),
    }
    if source.get("created_at"):
        package["created_at"] = as_text(source["created_at"], "source.created_at")
    return package


def resolve_source_path(value, source_root, label):
    value = as_text(value, label)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path(source_root) / candidate
    candidate = candidate.resolve()
    if not candidate.exists():
        raise FrameCueError(f"{label} does not exist: {candidate}")
    return candidate


def copy_file(source, out_dir, relative_target):
    source = Path(source)
    target = Path(out_dir) / relative_target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return Path(relative_target).as_posix()


def vtt_time(milliseconds):
    milliseconds = max(0, int(round(milliseconds)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def write_video_captions(cues, path):
    lines = ["WEBVTT", ""]
    for cue in cues:
        text = "\n".join(value for value in (cue.get("original_text", ""), cue.get("text", "")) if value)
        lines.extend([
            cue["id"],
            f"{vtt_time(cue['start_ms'])} --> {vtt_time(cue['end_ms'])}",
            text,
            "",
        ])
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def copy_nested_assets(row, source_root, out_dir, target_dir, label):
    for nested, key, asset_label in nested_asset_paths(row, label):
        source = resolve_source_path(nested[key], source_root, asset_label)
        suffix = source.suffix or ".bin"
        nested[key] = copy_file(source, out_dir, Path(target_dir) / f"{key}{suffix}")


def copy_viewer(out_dir):
    if not (DIST_DIR / "index.html").is_file():
        raise FrameCueError("FrameCue viewer build is missing; run npm install then npm run build")
    for child in DIST_DIR.iterdir():
        target = Path(out_dir) / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def materialize_assets(package, source_root, out_dir):
    for scene in package["scenes"]:
        if scene["image"]:
            source = resolve_source_path(scene["image"], source_root, f"scene {scene['id']} image")
            suffix = source.suffix or ".bin"
            scene["image"] = copy_file(source, out_dir, Path("assets/scenes") / f"{scene['id']}{suffix}")
        copy_nested_assets(scene, source_root, out_dir, Path("assets/scenes") / scene["id"], f"scene {scene['id']}")
    for cue in package["cues"]:
        if cue.get("audio"):
            source = resolve_source_path(cue["audio"], source_root, f"cue {cue['id']} audio")
            suffix = source.suffix or ".bin"
            cue["audio"] = copy_file(source, out_dir, Path("assets/audio") / f"{cue['id']}{suffix}")
        copy_nested_assets(cue, source_root, out_dir, Path("assets/cues") / cue["id"], f"cue {cue['id']}")

    video = package["media"].get("video")
    if video:
        if not isinstance(video, dict) or not video.get("source"):
            raise FrameCueError("media.video.source is required to build a portable bundle")
        source = resolve_source_path(video["source"], source_root, "media.video.source")
        if not source.is_file():
            raise FrameCueError("media.video.source must be a file")
        output = copy.deepcopy(video)
        output.pop("source", None)
        output["src"] = copy_file(source, out_dir, Path("assets/video") / f"source{source.suffix or '.mp4'}")
        captions = Path(out_dir) / "assets/video/captions.vtt"
        captions.parent.mkdir(parents=True, exist_ok=True)
        write_video_captions(package["cues"], captions)
        output["captions"] = "assets/video/captions.vtt"
        package["media"]["video"] = output

    carousel = package["media"].get("carousel")
    if carousel:
        if not isinstance(carousel, dict):
            raise FrameCueError("media.carousel must be an object")
        output = copy.deepcopy(carousel)
        for key in ("contact_sheet", "mobile_audit"):
            source = resolve_source_path(carousel.get(key, ""), source_root, f"media.carousel.{key}")
            output[key] = copy_file(source, out_dir, Path("assets/context") / f"{key}{source.suffix or '.png'}")
        package["media"]["carousel"] = output

    hyperframes = package["media"].get("hyperframes")
    if not hyperframes:
        return
    if not isinstance(hyperframes, dict):
        raise FrameCueError("media.hyperframes must be an object")
    if not hyperframes.get("source_dir"):
        raise FrameCueError("media.hyperframes.source_dir is required to build a portable bundle")
    source_dir = resolve_source_path(hyperframes["source_dir"], source_root, "media.hyperframes.source_dir")
    if not source_dir.is_dir():
        raise FrameCueError("media.hyperframes.source_dir must be a directory")
    target_dir = Path(out_dir) / "assets" / "hyperframes"
    shutil.copytree(source_dir, target_dir)
    if not ADAPTER_PATH.is_file():
        raise FrameCueError("generic HyperFrames adapter is missing")
    shutil.copy2(ADAPTER_PATH, target_dir / "framecue-player.html")
    config = relative_path(hyperframes.get("config", "review-player.json"), "media.hyperframes.config")
    if not (target_dir / config).is_file():
        raise FrameCueError(f"media.hyperframes.config is missing from source_dir: {config}")
    output = copy.deepcopy(hyperframes)
    output.pop("source_dir", None)
    output.pop("entry", None)
    output["entry"] = "assets/hyperframes/framecue-player.html"
    output["config"] = config
    package["media"]["hyperframes"] = output


def atomic_build(out_dir, callback):
    out_dir = Path(out_dir).expanduser().resolve()
    if out_dir.exists():
        raise FrameCueError(f"refusing to overwrite immutable bundle: {out_dir}")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.", dir=out_dir.parent))
    try:
        result = callback(temp_dir)
        temp_dir.replace(out_dir)
        return result
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def build_package(source, source_root, out_dir, viewer_version=VIEWER_VERSION):
    package = normalize_source(source, viewer_version)

    def build(temp_dir):
        copy_viewer(temp_dir)
        materialize_assets(package, source_root, temp_dir)
        package["content_checksum"] = package_checksum(package)
        write_json(temp_dir / "review_package.json", package)
        return validate_package(package, temp_dir)

    return atomic_build(out_dir, build)


def bundle_summary(package_path):
    package_path = Path(package_path).expanduser().resolve()
    package = read_json(package_path)
    summary = validate_package(package, package_path.parent)
    summary["package"] = str(package_path)
    return package, summary


def migrate_v1(v1_package_path, semantic_blocks_path, review_id, revision, workflow):
    v1_path = Path(v1_package_path).expanduser().resolve()
    v1 = read_json(v1_path)
    if not isinstance(v1, dict) or not isinstance(v1.get("cues"), list):
        raise FrameCueError("v1 review package is missing cues")
    scene_map = {
        str(scene.get("id", index)): migrated_id("s", scene.get("id"), index)
        for index, scene in enumerate(v1.get("scenes", []), 1)
    }
    scenes = []
    for scene in v1.get("scenes", []):
        old_id = str(scene.get("id"))
        migrated_scene = {
            "id": scene_map[old_id],
            "start_ms": round(float(scene.get("start", 0)) * 1000),
            "end_ms": round(float(scene.get("end", 0)) * 1000),
            "image": scene.get("image", ""),
        }
        if scene.get("compare_image") or scene.get("full_image") or scene.get("redraw_prompt"):
            migrated_scene["redraw"] = {
                "comparison_image": scene.get("compare_image", ""),
                "current_image": scene.get("full_image", ""),
                "trace": {"prompt": scene.get("redraw_prompt", "")},
            }
        if isinstance(scene.get("boundary"), dict):
            migrated_scene["boundary"] = copy.deepcopy(scene["boundary"])
        scenes.append(migrated_scene)
    cues = []
    cue_map = {}
    for index, cue in enumerate(v1["cues"], 1):
        old_id = str(cue.get("id", index))
        cue_id = migrated_id("c", cue.get("id"), index)
        cue_map[old_id] = cue_id
        migrated_cue = {
            "id": cue_id,
            "start_ms": round(float(cue.get("start", 0)) * 1000),
            "end_ms": round(float(cue.get("end", 0)) * 1000),
            "scene_id": scene_map[str(cue.get("scene_id"))],
            "text": cue.get("text", ""),
            "speech_text": cue.get("speech_text", cue.get("text", "")),
            "original_text": cue.get("original_text", ""),
            "audio": cue.get("audio", ""),
            "risks": cue.get("pronunciation_risks", cue.get("risks", [])),
        }
        if cue.get("redraw_prompt"):
            migrated_cue["redraw"] = {"trace": {"prompt": cue["redraw_prompt"]}}
        if isinstance(cue.get("boundary"), dict):
            migrated_cue["boundary"] = copy.deepcopy(cue["boundary"])
        cues.append(migrated_cue)
    raw_blocks = []
    if semantic_blocks_path:
        semantic = read_json(semantic_blocks_path)
        raw_blocks = semantic.get("blocks", [])
    blocks = []
    for index, block in enumerate(raw_blocks, 1):
        cue_refs = block.get("cue_ids") or block.get("input_ids") or block.get("source_cue_ids") or []
        blocks.append({
            "id": str(block.get("id", f"b{index:04d}")),
            "cue_ids": [cue_map.get(str(cue_id), str(cue_id)) for cue_id in cue_refs],
            "start_ms": source_ms(block, "start_ms", f"blocks[{index}]"),
            "end_ms": source_ms(block, "end_ms", f"blocks[{index}]"),
            "budget_ms": block.get("budget_ms", max(0, source_ms(block, "end_ms", f"blocks[{index}]") - source_ms(block, "start_ms", f"blocks[{index}]"))),
            "source_text": block.get("source_text", ""),
            "target_text": block.get("target_text", ""),
            "speech_text": block.get("speech_text", block.get("target_text", "")),
        })
    return {
        "review_id": review_id,
        "revision": revision,
        "workflow": {"kind": workflow, "label": v1_path.parent.name},
        "subtitle_policy": v1.get("subtitle_policy", {
            "display_punctuation": "stripped_before_framecue",
            "speech_punctuation": "preserved",
        }),
        "scenes": scenes,
        "cues": cues,
        "blocks": blocks,
        "provenance": {
            "legacy_review_package": str(v1_path),
            "legacy_semantic_blocks": str(Path(semantic_blocks_path).expanduser().resolve()) if semantic_blocks_path else "",
        },
        "lineage": {"previous_package_checksum": "", "previous_result": ""},
    }


def build_manifest(items, out_dir):
    parsed = []
    seen_ids = set()
    for item in items:
        if "=" not in item:
            raise FrameCueError("--item must be ID=PACKAGE_DIRECTORY_OR_JSON")
        item_id, raw_path = item.split("=", 1)
        item_id = ensure_id(item_id, "manifest item id")
        if item_id in seen_ids:
            raise FrameCueError(f"duplicate manifest item id: {item_id}")
        seen_ids.add(item_id)
        source = Path(raw_path).expanduser().resolve()
        package_path = source / "review_package.json" if source.is_dir() else source
        _, summary = bundle_summary(package_path)
        parsed.append((item_id, package_path, summary))

    def build(temp_dir):
        copy_viewer(temp_dir)
        manifest_items = []
        for item_id, package_path, summary in parsed:
            item_dir = temp_dir / "items" / item_id
            shutil.copytree(package_path.parent, item_dir)
            manifest_items.append({
                "id": item_id,
                "label": summary["review_id"],
                "review_package": f"items/{item_id}/review_package.json",
            })
        manifest = {"schema_version": MANIFEST_SCHEMA, "items": manifest_items}
        write_json(temp_dir / "framecue_manifest.json", manifest)
        return {"item_count": len(manifest_items), "items": [item["id"] for item in manifest_items]}

    return atomic_build(out_dir, build)


def default_result(package, approved=False):
    return {
        "schema_version": RESULT_SCHEMA,
        "review_id": package["review_id"],
        "revision": package["revision"],
        "package_checksum": package["content_checksum"],
        "viewer_version": package["viewer_version"],
        "status": "approved" if approved else "draft",
        "approved_at": utc_now() if approved else "",
        "generated_at": utc_now(),
        "blocks": [{
            "id": block["id"],
            "target_text": block["target_text"],
            "speech_text": block["speech_text"],
            "action": "use_edit",
            "instruction": "",
            "approved": approved,
        } for block in package["blocks"]],
        "cues": [{
            "id": cue["id"],
            "text": cue["text"],
            "speech_text": cue["speech_text"],
            "action": "use_edit",
            "instruction": "",
        } for cue in package["cues"]],
    }


def self_check():
    with tempfile.TemporaryDirectory(prefix="framecue-self-check-") as temp:
        root = Path(temp)
        (root / "frame.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="100%" height="100%" fill="#123"/></svg>',
            encoding="utf-8",
        )
        source = {
            "review_id": "self-check",
            "revision": "r1",
            "workflow": {"kind": "subtitle"},
            "scenes": [{"id": "s1", "start_ms": 0, "end_ms": 1000, "image": "frame.svg"}],
            "cues": [{
                "id": "c1", "start_ms": 0, "end_ms": 1000, "scene_id": "s1",
                "text": "字幕", "speech_text": "字幕。", "original_text": "subtitle",
            }],
            "blocks": [{
                "id": "b1", "cue_ids": ["c1"], "start_ms": 0, "end_ms": 1000,
                "source_text": "subtitle", "target_text": "字幕", "speech_text": "字幕。",
            }],
        }
        out_dir = root / "bundle"
        build_package(source, root, out_dir)
        package, _ = bundle_summary(out_dir / "review_package.json")
        result = default_result(package, approved=True)
        validate_result(result, package, require_approved=True)
        assert (out_dir / "index.html").is_file()
        assert (out_dir / "assets/scenes/s1.svg").is_file()
    print("self-check ok")


def run_legacy(arguments):
    legacy = ROOT / "legacy" / "framecue_v1.py"
    if not legacy.is_file():
        raise FrameCueError("legacy v1 builder is missing")
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    subprocess.run([sys.executable, str(legacy), *arguments], check=True)


def command_build(args):
    source_path = Path(args.input).expanduser().resolve()
    source = read_json(source_path)
    summary = build_package(source, source_path.parent, args.out_dir, args.viewer_version)
    print(json.dumps(summary, ensure_ascii=False))


def command_build_carousel(args):
    cards_dir = Path(args.cards_dir).expanduser().resolve()
    source = carousel_source(cards_dir, args.review_id, args.revision, args.label)
    summary = build_package(source, cards_dir, args.out_dir, args.viewer_version)
    print(json.dumps(summary, ensure_ascii=False))


def command_build_markdown(args):
    article = Path(args.article).expanduser().resolve()
    source = markdown_source(article, args.review_id, args.revision, args.label)
    summary = build_package(source, article.parent, args.out_dir, args.viewer_version)
    print(json.dumps(summary, ensure_ascii=False))


def command_validate(args):
    package, summary = bundle_summary(args.package)
    if args.result:
        result = read_json(args.result)
        summary["result"] = validate_result(result, package, args.require_approved)
    print(json.dumps(summary, ensure_ascii=False))


def command_collect(args):
    package, _ = bundle_summary(args.package)
    result = read_json(args.result)
    summary = validate_result(result, package, require_approved=True)
    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        if out_path.exists():
            raise FrameCueError(f"refusing to overwrite collected result: {out_path}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(out_path, result)
        summary["collected_result"] = str(out_path)
    print(json.dumps(summary, ensure_ascii=False))


def command_migrate(args):
    source = migrate_v1(args.package, args.semantic_blocks, args.review_id, args.revision, args.workflow)
    summary = build_package(source, Path(args.package).expanduser().resolve().parent, args.out_dir, args.viewer_version)
    print(json.dumps(summary, ensure_ascii=False))


def command_manifest(args):
    summary = build_manifest(args.item, args.out_dir)
    print(json.dumps(summary, ensure_ascii=False))


def command_serve(args):
    directory = Path(args.dir).expanduser().resolve()
    if not (directory / "index.html").is_file():
        raise FrameCueError(f"not a FrameCue bundle: {directory}")
    miniserve = shutil.which("miniserve")
    if miniserve:
        subprocess.run([miniserve, "--interfaces", "127.0.0.1", "--port", str(args.port), str(directory)], check=True)
        return
    print("warning: Python http.server may not support byte ranges needed by video playback", file=sys.stderr)
    subprocess.run([sys.executable, "-m", "http.server", str(args.port), "--directory", str(directory)], check=True)


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--version", action="version", version=VIEWER_VERSION)
    commands = root.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build an immutable v2 review bundle")
    build.add_argument("--input", required=True)
    build.add_argument("--out-dir", required=True)
    build.add_argument("--viewer-version", default=VIEWER_VERSION)
    build.set_defaults(func=command_build)

    carousel = commands.add_parser("build-carousel", help="build one independent review cue per slide-NN.png")
    carousel.add_argument("--cards-dir", required=True)
    carousel.add_argument("--review-id", required=True)
    carousel.add_argument("--revision", default="r1")
    carousel.add_argument("--label", default="")
    carousel.add_argument("--out-dir", required=True)
    carousel.add_argument("--viewer-version", default=VIEWER_VERSION)
    carousel.set_defaults(func=command_build_carousel)

    markdown = commands.add_parser("build-markdown", help="build one review cue per Markdown content block")
    markdown.add_argument("--article", required=True)
    markdown.add_argument("--review-id", required=True)
    markdown.add_argument("--revision", default="r1")
    markdown.add_argument("--label", default="")
    markdown.add_argument("--out-dir", required=True)
    markdown.add_argument("--viewer-version", default=VIEWER_VERSION)
    markdown.set_defaults(func=command_build_markdown)

    validate = commands.add_parser("validate", help="validate a v2 package and optional result")
    validate.add_argument("--package", required=True)
    validate.add_argument("--result")
    validate.add_argument("--require-approved", action="store_true")
    validate.set_defaults(func=command_validate)

    collect = commands.add_parser("collect", help="validate and retain an approved result")
    collect.add_argument("--package", required=True)
    collect.add_argument("--result", required=True)
    collect.add_argument("--out")
    collect.set_defaults(func=command_collect)

    migrate = commands.add_parser("migrate-v1", help="create a new v2 revision from a v1 package")
    migrate.add_argument("--package", required=True)
    migrate.add_argument("--semantic-blocks")
    migrate.add_argument("--review-id", required=True)
    migrate.add_argument("--revision", default="r1")
    migrate.add_argument("--workflow", choices=sorted(WORKFLOW_KINDS), default="subtitle")
    migrate.add_argument("--out-dir", required=True)
    migrate.add_argument("--viewer-version", default=VIEWER_VERSION)
    migrate.set_defaults(func=command_migrate)

    manifest = commands.add_parser("manifest", help="bundle several immutable v2 packages into one page")
    manifest.add_argument("--out-dir", required=True)
    manifest.add_argument("--item", action="append", required=True)
    manifest.set_defaults(func=command_manifest)

    serve = commands.add_parser("serve", help="serve an immutable FrameCue bundle")
    serve.add_argument("--dir", required=True)
    serve.add_argument("--port", type=int, default=3069)
    serve.set_defaults(func=command_serve)

    legacy = commands.add_parser("legacy-build", help="run the frozen v1 builder")
    legacy.add_argument("arguments", nargs=argparse.REMAINDER)
    legacy.set_defaults(func=lambda args: run_legacy(args.arguments))

    check = commands.add_parser("self-check", help="run the smallest v2 bundle round-trip check")
    check.set_defaults(func=lambda args: self_check())
    return root


def main():
    args = parser().parse_args()
    try:
        args.func(args)
    except (FrameCueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"framecue: {error}") from error


if __name__ == "__main__":
    main()
