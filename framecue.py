#!/usr/bin/env python3
"""Build, validate, migrate, and collect immutable FrameCue v2 bundles."""

import argparse
import copy
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
ADAPTER_PATH = ROOT / "adapters" / "hyperframes-player.html"
VIEWER_VERSION = "2.6.0"
PACKAGE_SCHEMA = "framecue_package_v2"
RESULT_SCHEMA = "framecue_review_result_v1"
MANIFEST_SCHEMA = "framecue_manifest_v2"
SUBTITLE_DOCUMENT_SCHEMA = "framecue_subtitle_document_v1"
WORK_ORDER_SCHEMA = "framecue_work_order_v1"
CANDIDATE_REVISION_SCHEMA = "framecue_candidate_revision_v1"
SUBTITLE_DOCUMENT_V2_SCHEMA = "framecue_subtitle_document_v2"
WORK_ORDER_V2_SCHEMA = "framecue_work_order_v2"
CANDIDATE_REVISION_V2_SCHEMA = "framecue_candidate_revision_v2"
TIMING_PROFILES = {"synchronous_dub", "interpreter_lag"}
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


def document_checksum(document):
    payload = copy.deepcopy(document)
    payload.pop("checksum", None)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def workspace_assets(package):
    return {
        "media": copy.deepcopy(package.get("media", {})),
        "scenes": [{"id": scene["id"], "image": scene["image"]} for scene in package["scenes"]],
        "cue_audio": [{"id": cue["id"], "audio": cue["audio"]} for cue in package["cues"] if cue.get("audio")],
    }


def subtitle_document(package, timing_profile, result=None):
    if timing_profile not in TIMING_PROFILES:
        raise FrameCueError("timing_profile is invalid")
    result_cues = {row["id"]: row for row in result["cues"]} if result else {}
    result_blocks = {row["id"]: row for row in result["blocks"]} if result else {}
    block_by_cue = {}
    for block in package["blocks"]:
        for cue_id in block["cue_ids"]:
            block_by_cue.setdefault(cue_id, block["id"])
    assets = workspace_assets(package)
    document = {
        "schema": SUBTITLE_DOCUMENT_SCHEMA,
        "workspace_id": package["review_id"],
        "revision": package["revision"],
        "revision_kind": "content" if result else "source",
        "source_checksum": package["content_checksum"],
        "timing_profile": timing_profile,
        "cues": [{
            "id": cue["id"],
            "source_start_ms": cue["start_ms"],
            "source_end_ms": cue["end_ms"],
            "output_start_ms": None,
            "output_end_ms": None,
            "source_text": cue.get("original_text", ""),
            "display_text": result_cues.get(cue["id"], cue)["text"],
            "speech_text": result_cues.get(cue["id"], cue)["speech_text"],
            "block_id": block_by_cue.get(cue["id"], ""),
        } for cue in package["cues"]],
        "blocks": [{
            **block,
            "target_text": result_blocks.get(block["id"], block)["target_text"],
            "speech_text": result_blocks.get(block["id"], block)["speech_text"],
        } for block in package["blocks"]],
        "assets": assets,
        "source_package": copy.deepcopy(package),
        "approval_snapshot": copy.deepcopy(result) if result else None,
    }
    document["checksum"] = document_checksum(document)
    return document


def opaque_id(prefix):
    return f"{prefix}-{secrets.token_hex(12)}"


def workspace_draft_document(package, timing_profile):
    if timing_profile not in TIMING_PROFILES:
        raise FrameCueError("timing_profile is invalid")
    block_by_cue = {}
    for block in package["blocks"]:
        for cue_id in block["cue_ids"]:
            block_by_cue[cue_id] = block["id"]
    document = {
        "schema": SUBTITLE_DOCUMENT_V2_SCHEMA,
        "workspace_id": package["review_id"],
        "revision": package["revision"],
        "revision_kind": "draft",
        "source_checksum": package["content_checksum"],
        "timing_profile": timing_profile,
        "cues": [{
            "id": cue["id"],
            "source_start_ms": cue["start_ms"],
            "source_end_ms": cue["end_ms"],
            "output_start_ms": None,
            "output_end_ms": None,
            "timing_state": "unrealized",
            "source_text": cue.get("original_text", ""),
            "display_text": cue["text"],
            "speech_text": cue["speech_text"],
            "speech_linked": content_key(cue["speech_text"]) == content_key(cue["text"]),
            "block_id": block_by_cue.get(cue["id"], ""),
            "origin_cue_ids": [cue["id"]],
            "lineage": {"operation": "import", "parent_cue_ids": []},
        } for cue in package["cues"]],
        "blocks": copy.deepcopy(package["blocks"]),
        "assets": workspace_assets(package),
        "source_package": copy.deepcopy(package),
    }
    document["checksum"] = document_checksum(document)
    return document


def refresh_document_checksum(document):
    document["checksum"] = document_checksum(document)


def recompute_draft_blocks(document):
    cues = document.get("cues")
    blocks = document.get("blocks")
    if not isinstance(cues, list) or not isinstance(blocks, list):
        raise FrameCueError("workspace draft document is incomplete")
    cue_by_id = {cue.get("id"): cue for cue in cues if isinstance(cue, dict)}
    if len(cue_by_id) != len(cues):
        raise FrameCueError("workspace draft cues are invalid")
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("cue_ids"), list):
            raise FrameCueError("workspace draft blocks are invalid")
        try:
            block_cues = [cue_by_id[cue_id] for cue_id in block["cue_ids"]]
        except KeyError as error:
            raise FrameCueError(f"workspace draft block references an unknown cue: {error.args[0]}") from error
        block["target_text"] = " ".join(cue["display_text"] for cue in block_cues)
        block["speech_text"] = " ".join(cue["speech_text"] for cue in block_cues)


def draft_row(connection, workspace):
    row = connection.execute(
        """SELECT draft_version, document_json, issues_json, direct_changes_json
           FROM workspace_drafts WHERE review_id = ?""",
        (workspace["review_id"],),
    ).fetchone()
    if row is None:
        source_document, package = workspace_source_package(connection, workspace)
        document = workspace_draft_document(package, source_document.get("timing_profile"))
        connection.execute(
            """INSERT INTO workspace_drafts
               (review_id, draft_version, document_json, issues_json, direct_changes_json, frozen_revision_id)
               VALUES (?, 0, ?, '[]', '[]', NULL)""",
            (workspace["review_id"], canonical_json(document)),
        )
        return {
            "draft_version": 0,
            "document": document,
            "issues": [],
            "direct_changes": [],
        }
    try:
        document = json.loads(row["document_json"])
        issues = json.loads(row["issues_json"])
        direct_changes = json.loads(row["direct_changes_json"])
    except json.JSONDecodeError as error:
        raise FrameCueError(f"workspace draft is invalid JSON: {workspace['review_id']}") from error
    if (
        type(row["draft_version"]) is not int
        or not isinstance(document, dict)
        or document.get("schema") != SUBTITLE_DOCUMENT_V2_SCHEMA
        or not isinstance(issues, list)
        or not isinstance(direct_changes, list)
    ):
        raise FrameCueError(f"workspace draft is invalid: {workspace['review_id']}")
    return {
        "draft_version": row["draft_version"],
        "document": document,
        "issues": issues,
        "direct_changes": direct_changes,
    }


def apply_draft_edit(document, operation):
    cue_id = ensure_id(operation.get("cue_id", ""), "draft edit cue_id")
    display_text = as_text(operation.get("display_text"), "draft edit display_text")
    cue = next((row for row in document["cues"] if row.get("id") == cue_id), None)
    if cue is None:
        raise FrameCueError(f"workspace draft cue not found: {cue_id}")
    cue["display_text"] = display_text
    if cue.get("speech_linked"):
        cue["speech_text"] = display_text
    recompute_draft_blocks(document)
    refresh_document_checksum(document)
    return {"kind": "edit", "cue_ids": [cue_id]}


def split_draft_text(text, cursor, label):
    if type(cursor) is not int or cursor <= 0 or cursor >= len(text):
        raise FrameCueError(f"draft split cursor is invalid for {label}")
    left = text[:cursor].strip()
    right = text[cursor:].strip()
    if not left or not right:
        raise FrameCueError(f"draft split cursor creates an empty Cue: {label}")
    return left, right


def apply_draft_split(document, operation):
    cue_id = ensure_id(operation.get("cue_id", ""), "draft split cue_id")
    cue_index = next((index for index, row in enumerate(document["cues"]) if row.get("id") == cue_id), None)
    if cue_index is None:
        raise FrameCueError(f"workspace draft cue not found: {cue_id}")
    cue = document["cues"][cue_index]
    display_text = as_text(cue.get("display_text"), f"workspace draft cue display_text: {cue_id}")
    cursor = operation.get("cursor")
    if operation.get("word_timestamps") is not None:
        raise FrameCueError("draft split trusted word timestamps are not implemented")
    left_display, right_display = split_draft_text(display_text, cursor, cue_id)
    if cue.get("speech_linked"):
        left_speech, right_speech = left_display, right_display
    else:
        speech_text = as_text(cue.get("speech_text"), f"workspace draft cue speech_text: {cue_id}")
        speech_cursor = min(len(speech_text) - 1, max(1, round(len(speech_text) * cursor / len(display_text))))
        left_speech, right_speech = split_draft_text(speech_text, speech_cursor, cue_id)
    start = as_ms(cue.get("source_start_ms"), f"workspace draft cue source_start_ms: {cue_id}")
    end = as_ms(cue.get("source_end_ms"), f"workspace draft cue source_end_ms: {cue_id}")
    if end - start < 2:
        raise FrameCueError(f"workspace draft cue is too short to split: {cue_id}")
    split_at = start + round((end - start) * cursor / len(display_text))
    split_at = min(end - 1, max(start + 1, split_at))
    origins = cue.get("origin_cue_ids")
    if not isinstance(origins, list) or not all(isinstance(value, str) for value in origins):
        origins = [cue_id]

    def child(cue_id, source_start, source_end, display, speech):
        value = copy.deepcopy(cue)
        value.update({
            "id": cue_id,
            "source_start_ms": source_start,
            "source_end_ms": source_end,
            "output_start_ms": None,
            "output_end_ms": None,
            "timing_state": "provisional",
            "display_text": display,
            "speech_text": speech,
            "origin_cue_ids": origins,
            "lineage": {"operation": "split", "parent_cue_ids": [cue["id"]]},
        })
        return value

    left = child(opaque_id("cue"), start, split_at, left_display, left_speech)
    right = child(opaque_id("cue"), split_at, end, right_display, right_speech)
    block = next((row for row in document["blocks"] if row.get("id") == cue.get("block_id")), None)
    if block is None or cue_id not in block.get("cue_ids", []):
        raise FrameCueError(f"workspace draft Cue Block is invalid: {cue_id}")
    block_index = block["cue_ids"].index(cue_id)
    document["cues"][cue_index:cue_index + 1] = [left, right]
    block["cue_ids"][block_index:block_index + 1] = [left["id"], right["id"]]
    recompute_draft_blocks(document)
    refresh_document_checksum(document)
    return {"kind": "split", "cue_ids": [left["id"], right["id"]]}


def apply_draft_merge(document, operation):
    cue_id = ensure_id(operation.get("cue_id", ""), "draft merge cue_id")
    adjacent_cue_id = ensure_id(operation.get("adjacent_cue_id", ""), "draft merge adjacent_cue_id")
    if cue_id == adjacent_cue_id:
        raise FrameCueError("draft merge requires two different Cues")
    cue_by_id = {cue.get("id"): cue for cue in document["cues"] if isinstance(cue, dict)}
    if cue_id not in cue_by_id or adjacent_cue_id not in cue_by_id:
        raise FrameCueError("workspace draft merge Cue was not found")
    first = cue_by_id[cue_id]
    second = cue_by_id[adjacent_cue_id]
    if first.get("block_id") != second.get("block_id"):
        raise FrameCueError("draft merge Cues must share one Semantic Block")
    block = next((row for row in document["blocks"] if row.get("id") == first.get("block_id")), None)
    if block is None or not isinstance(block.get("cue_ids"), list):
        raise FrameCueError(f"workspace draft Cue Block is invalid: {cue_id}")
    first_index = block["cue_ids"].index(cue_id) if cue_id in block["cue_ids"] else -1
    second_index = block["cue_ids"].index(adjacent_cue_id) if adjacent_cue_id in block["cue_ids"] else -1
    if first_index < 0 or second_index < 0 or abs(first_index - second_index) != 1:
        raise FrameCueError("draft merge Cues must be adjacent in their Semantic Block")
    left_id, right_id = (cue_id, adjacent_cue_id) if first_index < second_index else (adjacent_cue_id, cue_id)
    left = cue_by_id[left_id]
    right = cue_by_id[right_id]
    origins = []
    for cue in (left, right):
        for origin in cue.get("origin_cue_ids", [cue["id"]]):
            if origin not in origins:
                origins.append(origin)
    output_start = left.get("output_start_ms")
    output_end = right.get("output_end_ms")
    if type(output_start) is not int or type(output_end) is not int:
        output_start = None
        output_end = None
    merged = {
        "id": opaque_id("cue"),
        "source_start_ms": min(left["source_start_ms"], right["source_start_ms"]),
        "source_end_ms": max(left["source_end_ms"], right["source_end_ms"]),
        "output_start_ms": output_start,
        "output_end_ms": output_end,
        "timing_state": "provisional" if "provisional" in {left.get("timing_state"), right.get("timing_state")} else "unrealized",
        "source_text": " ".join(value for value in (left.get("source_text", ""), right.get("source_text", "")) if value),
        "display_text": " ".join((left["display_text"], right["display_text"])),
        "speech_text": " ".join((left["speech_text"], right["speech_text"])),
        "speech_linked": bool(left.get("speech_linked")) and bool(right.get("speech_linked")),
        "block_id": block["id"],
        "origin_cue_ids": origins,
        "lineage": {"operation": "merge", "parent_cue_ids": [left_id, right_id]},
    }
    cue_indexes = [index for index, cue in enumerate(document["cues"]) if cue.get("id") in {left_id, right_id}]
    insert_at = min(cue_indexes)
    document["cues"] = [cue for cue in document["cues"] if cue.get("id") not in {left_id, right_id}]
    document["cues"].insert(insert_at, merged)
    block_index = min(first_index, second_index)
    block["cue_ids"][block_index:block_index + 2] = [merged["id"]]
    recompute_draft_blocks(document)
    refresh_document_checksum(document)
    return {"kind": "merge", "cue_ids": [merged["id"]]}


def apply_draft_flag(document, issues, operation):
    requested_cues = operation.get("cue_ids")
    if requested_cues is None:
        requested_cues = [operation.get("cue_id")]
    if not isinstance(requested_cues, list) or not requested_cues:
        raise FrameCueError("draft flag cue_ids must be a non-empty array")
    if not all(isinstance(cue_id, str) for cue_id in requested_cues) or len(set(requested_cues)) != len(requested_cues):
        raise FrameCueError("draft flag cue_ids are invalid")
    document_cue_ids = [cue.get("id") for cue in document["cues"] if isinstance(cue, dict)]
    positions = [document_cue_ids.index(cue_id) if cue_id in document_cue_ids else -1 for cue_id in requested_cues]
    if -1 in positions:
        raise FrameCueError("workspace draft flag Cue was not found")
    cue_ids = document_cue_ids[min(positions):max(positions) + 1]
    if set(cue_ids) != set(requested_cues):
        raise FrameCueError("draft flag Cues must be contiguous")
    categories = operation.get("categories")
    if not isinstance(categories, list) or not categories or not all(isinstance(category, str) and category for category in categories):
        raise FrameCueError("draft flag categories must be a non-empty string array")
    author = as_text(operation.get("author", "local"), "draft flag author")
    note = as_text(operation.get("note", ""), "draft flag note")
    if not author:
        raise FrameCueError("draft flag author must not be empty")
    if not isinstance(issues, list):
        raise FrameCueError("workspace draft issues are invalid")
    for category in sorted(set(categories)):
        issue = next((row for row in issues if isinstance(row, dict) and row.get("cue_ids") == cue_ids and row.get("category") == category), None)
        if issue is None:
            issue = {
                "flag_id": opaque_id("flag"),
                "range_id": opaque_id("range"),
                "cue_ids": cue_ids,
                "category": category,
                "authors": [],
                "notes": [],
            }
            issues.append(issue)
        if not isinstance(issue.get("authors"), list) or not isinstance(issue.get("notes"), list):
            raise FrameCueError("workspace draft issue is invalid")
        if author not in issue["authors"]:
            issue["authors"].append(author)
        if note and note not in issue["notes"]:
            issue["notes"].append(note)


def apply_draft_operation(database, review_id, operation):
    if not isinstance(operation, dict):
        raise FrameCueError("draft operation must be an object")
    expected_version = operation.get("draft_version")
    if type(expected_version) is not int or expected_version < 0:
        raise FrameCueError("draft operation draft_version is invalid")
    kind = operation.get("kind")
    if kind not in {"edit", "split", "merge", "flag"}:
        raise FrameCueError("draft operation kind is invalid")
    connection = open_workspace_database(database)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            workspace = workspace_row(connection, review_id)
            if workspace["stage"] != "content_review":
                raise FrameCueError(f"workspace is not accepting draft operations: {review_id}")
            draft = draft_row(connection, workspace)
            if draft["draft_version"] != expected_version:
                raise FrameCueError(
                    f"workspace draft version is stale: expected {draft['draft_version']}, got {expected_version}"
                )
            if kind == "edit":
                change = apply_draft_edit(draft["document"], operation)
            elif kind == "split":
                change = apply_draft_split(draft["document"], operation)
            elif kind == "merge":
                change = apply_draft_merge(draft["document"], operation)
            else:
                apply_draft_flag(draft["document"], draft["issues"], operation)
                change = None
            if change is not None:
                draft["direct_changes"].append(change)
            next_version = expected_version + 1
            if connection.execute(
                """UPDATE workspace_drafts
                   SET draft_version = ?, document_json = ?, issues_json = ?, direct_changes_json = ?
                   WHERE review_id = ? AND draft_version = ?""",
                (
                    next_version,
                    canonical_json(draft["document"]),
                    canonical_json(draft["issues"]),
                    canonical_json(draft["direct_changes"]),
                    review_id,
                    expected_version,
                ),
            ).rowcount != 1:
                raise FrameCueError(f"workspace draft version is stale: {review_id}")
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise FrameCueError(f"workspace database error: {error}") from error
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()
    return {
        "workspace_id": review_id,
        "stage": "content_review",
        "draft_version": next_version,
        "document": draft["document"],
        "issues": draft["issues"],
        "direct_edit_count": len(draft["direct_changes"]),
    }


def open_workspace_database(database):
    connection = None
    try:
        database_path = Path(database).expanduser().resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS workspaces (
                review_id TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                current_revision_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
                revision_id INTEGER PRIMARY KEY,
                review_id TEXT NOT NULL REFERENCES workspaces(review_id),
                revision TEXT NOT NULL,
                kind TEXT NOT NULL,
                parent_revision_id INTEGER REFERENCES revisions(revision_id),
                checksum TEXT NOT NULL,
                document_json TEXT NOT NULL,
                assets_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(review_id, kind, checksum)
            );
            CREATE TABLE IF NOT EXISTS work_orders (
                work_order_id INTEGER PRIMARY KEY,
                request_id TEXT UNIQUE NOT NULL,
                review_id TEXT NOT NULL REFERENCES workspaces(review_id),
                revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
                base_revision TEXT NOT NULL,
                base_checksum TEXT NOT NULL,
                operation TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                candidate_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(review_id, base_revision, base_checksum, operation)
            );
            CREATE TABLE IF NOT EXISTS workspace_drafts (
                review_id TEXT PRIMARY KEY REFERENCES workspaces(review_id),
                draft_version INTEGER NOT NULL,
                document_json TEXT NOT NULL,
                issues_json TEXT NOT NULL,
                direct_changes_json TEXT NOT NULL,
                frozen_revision_id INTEGER REFERENCES revisions(revision_id)
            );
        """)
        return connection
    except (OSError, sqlite3.Error) as error:
        if connection is not None:
            connection.close()
        raise FrameCueError(f"workspace database error: {error}") from error


def workspace_row(connection, review_id):
    row = connection.execute(
        "SELECT review_id, stage, current_revision_id FROM workspaces WHERE review_id = ?",
        (review_id,),
    ).fetchone()
    if row is None:
        raise FrameCueError(f"workspace not found: {review_id}")
    return row


def workspace_source_package(connection, workspace):
    row = connection.execute(
        "SELECT document_json FROM revisions WHERE revision_id = ?",
        (workspace["current_revision_id"],),
    ).fetchone()
    if row is None:
        raise FrameCueError(f"workspace has no current revision: {workspace['review_id']}")
    try:
        document = json.loads(row["document_json"])
    except json.JSONDecodeError as error:
        raise FrameCueError(f"workspace revision is invalid JSON: {workspace['review_id']}") from error
    package = document.get("source_package")
    if not isinstance(package, dict):
        raise FrameCueError(f"workspace source package is missing: {workspace['review_id']}")
    return document, package


def completed_draft_summary(connection, workspace, draft_version):
    draft = connection.execute(
        "SELECT draft_version, frozen_revision_id FROM workspace_drafts WHERE review_id = ?",
        (workspace["review_id"],),
    ).fetchone()
    if draft is None or draft["draft_version"] != draft_version or draft["frozen_revision_id"] is None:
        raise FrameCueError(f"workspace is not awaiting content review: {workspace['review_id']}")
    order = connection.execute(
        """SELECT request_id, operation, base_revision, base_checksum
           FROM work_orders WHERE review_id = ? AND revision_id = ?
           ORDER BY work_order_id DESC LIMIT 1""",
        (workspace["review_id"], draft["frozen_revision_id"]),
    ).fetchone()
    if order is None:
        raise FrameCueError(f"workspace completed round is incomplete: {workspace['review_id']}")
    return {
        "workspace_id": workspace["review_id"],
        "stage": workspace["stage"],
        "operation": order["operation"],
        "request_id": order["request_id"],
        "draft_version": draft_version,
        "revision": order["base_revision"],
        "checksum": order["base_checksum"],
    }


def workspace_work_order_target(document, cue_ids, range_id, allowed_operations, allowed_fields, categories=(), notes=(), direct_edit=False):
    cue_by_id = {cue["id"]: cue for cue in document["cues"]}
    try:
        cues = [cue_by_id[cue_id] for cue_id in cue_ids]
    except KeyError as error:
        raise FrameCueError(f"workspace draft range references an unknown Cue: {error.args[0]}") from error
    block_ids = []
    for cue in cues:
        if cue["block_id"] not in block_ids:
            block_ids.append(cue["block_id"])
    blocks = [block for block in document["blocks"] if block["id"] in block_ids]
    projection = {"cues": cues, "blocks": blocks}
    return {
        "range_id": range_id,
        "cue_ids": cue_ids,
        "block_ids": block_ids,
        "source_start_ms": min(cue["source_start_ms"] for cue in cues),
        "source_end_ms": max(cue["source_end_ms"] for cue in cues),
        "lineage_anchors": {cue["id"]: cue.get("origin_cue_ids", []) for cue in cues},
        "allowed_operations": allowed_operations,
        "allowed_fields": allowed_fields,
        "before_checksum": hashlib.sha256(canonical_json(projection).encode("utf-8")).hexdigest(),
        "context": {"direct_edit": direct_edit, "categories": list(categories), "notes": list(notes)},
    }


def correction_work_order_targets(document, direct_changes, issues):
    cue_order = [cue["id"] for cue in document["cues"]]
    ranges = []
    for change in direct_changes:
        ranges.append({"cue_ids": change["cue_ids"], "range_id": "", "direct_edit": True, "categories": [], "notes": []})
    for issue in issues:
        ranges.append({
            "cue_ids": issue["cue_ids"], "range_id": issue["range_id"], "direct_edit": False,
            "categories": [issue["category"]], "notes": issue["notes"],
        })
    merged = []
    for value in ranges:
        overlap = [row for row in merged if set(row["cue_ids"]) & set(value["cue_ids"])]
        for row in overlap:
            merged.remove(row)
            value["cue_ids"] = [cue_id for cue_id in cue_order if cue_id in set(value["cue_ids"]) | set(row["cue_ids"])]
            value["range_id"] = value["range_id"] or row["range_id"]
            value["direct_edit"] = value["direct_edit"] or row["direct_edit"]
            for key in ("categories", "notes"):
                value[key] = list(dict.fromkeys(row[key] + value[key]))
        merged.append(value)
    return [
        workspace_work_order_target(
            document, value["cue_ids"], value["range_id"] or opaque_id("range"),
            ["edit", "split", "merge"],
            ["display_text", "speech_text", "block.target_text", "block.speech_text"],
            value["categories"], value["notes"], value["direct_edit"],
        )
        for value in merged
    ]


def complete_workspace_round(database, review_id, draft_version):
    if type(draft_version) is not int or draft_version < 0:
        raise FrameCueError("workspace completion draft_version is invalid")
    connection = open_workspace_database(database)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            workspace = workspace_row(connection, review_id)
            if workspace["stage"] != "content_review":
                summary = completed_draft_summary(connection, workspace, draft_version)
                connection.rollback()
                return summary
            draft = draft_row(connection, workspace)
            if draft["draft_version"] != draft_version:
                raise FrameCueError(
                    f"workspace draft version is stale: expected {draft['draft_version']}, got {draft_version}"
                )
            needs_correction = bool(draft["direct_changes"] or draft["issues"])
            document = copy.deepcopy(draft["document"])
            document["revision_kind"] = "draft_snapshot" if needs_correction else "content"
            refresh_document_checksum(document)
            created_at = utc_now()
            revision = connection.execute(
                """INSERT INTO revisions
                   (review_id, revision, kind, parent_revision_id, checksum, document_json, assets_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    review_id,
                    document["revision"],
                    document["revision_kind"],
                    workspace["current_revision_id"],
                    document["checksum"],
                    canonical_json(document),
                    canonical_json(document["assets"]),
                    created_at,
                ),
            )
            next_order = connection.execute(
                "SELECT COALESCE(MAX(work_order_id), 0) + 1 AS value FROM work_orders"
            ).fetchone()["value"]
            request_id = f"req-{next_order:04d}"
            if needs_correction:
                targets = correction_work_order_targets(document, draft["direct_changes"], draft["issues"])
                operation = "content_correction_review"
                stage = "content_agent_review_pending"
                required_outputs = ["document", "change_proposals"]
            else:
                targets = [workspace_work_order_target(
                    document,
                    [cue["id"] for cue in document["cues"]],
                    opaque_id("range"),
                    ["realize_voice_timeline"],
                    ["output_start_ms", "output_end_ms", "assets"],
                )]
                operation = "realize_voice_timeline"
                stage = "voice_realization_pending"
                required_outputs = ["document", "block_audio", "word_alignment", "timing_audit"]
            work_order = {
                "schema": WORK_ORDER_V2_SCHEMA,
                "request_id": request_id,
                "workspace_id": review_id,
                "operation": operation,
                "base_revision": document["revision"],
                "base_draft_version": draft_version,
                "base_checksum": document["checksum"],
                "timing_profile": document["timing_profile"],
                "targets": targets,
                "required_outputs": required_outputs,
                "document": document,
            }
            connection.execute(
                """INSERT INTO work_orders
                   (request_id, review_id, revision_id, base_revision, base_checksum, operation, status, request_json, candidate_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    request_id,
                    review_id,
                    revision.lastrowid,
                    document["revision"],
                    document["checksum"],
                    operation,
                    "pending",
                    canonical_json(work_order),
                    created_at,
                ),
            )
            if connection.execute(
                """UPDATE workspace_drafts SET frozen_revision_id = ?
                   WHERE review_id = ? AND draft_version = ? AND frozen_revision_id IS NULL""",
                (revision.lastrowid, review_id, draft_version),
            ).rowcount != 1:
                raise FrameCueError(f"workspace draft version is stale: {review_id}")
            if connection.execute(
                """UPDATE workspaces SET stage = ?, current_revision_id = ?
                   WHERE review_id = ? AND stage = 'content_review'""",
                (stage, workspace["current_revision_id"] if needs_correction else revision.lastrowid, review_id),
            ).rowcount != 1:
                raise FrameCueError(f"workspace is not awaiting content review: {review_id}")
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise FrameCueError(f"workspace database error: {error}") from error
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()
    return {
        "workspace_id": review_id,
        "stage": stage,
        "operation": operation,
        "request_id": request_id,
        "draft_version": draft_version,
        "revision": document["revision"],
        "checksum": document["checksum"],
    }


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


def command_workspace_import(args):
    package, _ = bundle_summary(args.package)
    document = subtitle_document(package, args.timing_profile)
    connection = open_workspace_database(args.database)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM workspaces WHERE review_id = ?",
                (package["review_id"],),
            ).fetchone():
                raise FrameCueError(f"workspace already exists: {package['review_id']}")
            created_at = utc_now()
            connection.execute(
                "INSERT INTO workspaces (review_id, stage, current_revision_id, created_at) VALUES (?, ?, NULL, ?)",
                (package["review_id"], "content_review", created_at),
            )
            cursor = connection.execute(
                """INSERT INTO revisions
                   (review_id, revision, kind, parent_revision_id, checksum, document_json, assets_json, created_at)
                   VALUES (?, ?, ?, NULL, ?, ?, ?, ?)""",
                (
                    package["review_id"],
                    package["revision"],
                    "source",
                    document["checksum"],
                    canonical_json(document),
                    canonical_json(document["assets"]),
                    created_at,
                ),
            )
            connection.execute(
                "UPDATE workspaces SET current_revision_id = ? WHERE review_id = ?",
                (cursor.lastrowid, package["review_id"]),
            )
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise FrameCueError(f"workspace database error: {error}") from error
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()
    print(json.dumps({
        "workspace_id": package["review_id"],
        "stage": "content_review",
        "revision": package["revision"],
        "checksum": document["checksum"],
    }, ensure_ascii=False))


def command_workspace_apply(args):
    operation = read_json(args.operation)
    print(json.dumps(
        apply_draft_operation(args.database, args.review_id, operation),
        ensure_ascii=False,
    ))


def command_workspace_complete(args):
    print(json.dumps(
        complete_workspace_round(args.database, args.review_id, args.draft_version),
        ensure_ascii=False,
    ))


def complete_content_revision(database, review_id, result):
    connection = open_workspace_database(database)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            workspace = workspace_row(connection, review_id)
            source_document, package = workspace_source_package(connection, workspace)
            if workspace["stage"] != "content_review":
                validate_result(result, package, require_approved=True)
                existing_result = copy.deepcopy(source_document.get("approval_snapshot"))
                repeated_result = copy.deepcopy(result)
                if isinstance(existing_result, dict):
                    existing_result.pop("generated_at", None)
                if isinstance(repeated_result, dict):
                    repeated_result.pop("generated_at", None)
                order = connection.execute(
                    """SELECT request_id FROM work_orders
                       WHERE review_id = ? AND revision_id = ? AND base_checksum = ?
                       ORDER BY work_order_id DESC LIMIT 1""",
                    (review_id, workspace["current_revision_id"], source_document.get("checksum")),
                ).fetchone()
                if (
                    source_document.get("revision_kind") == "content"
                    and canonical_json(existing_result) == canonical_json(repeated_result)
                    and order is not None
                ):
                    connection.rollback()
                    return {
                        "workspace_id": review_id,
                        "stage": workspace["stage"],
                        "request_id": order["request_id"],
                        "revision": package["revision"],
                        "checksum": source_document["checksum"],
                    }
                raise FrameCueError(f"workspace is not awaiting content review: {review_id}")
            validate_result(result, package, require_approved=True)
            document = subtitle_document(package, source_document.get("timing_profile"), result)
            created_at = utc_now()
            cursor = connection.execute(
                """INSERT INTO revisions
                   (review_id, revision, kind, parent_revision_id, checksum, document_json, assets_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    review_id,
                    package["revision"],
                    "content",
                    workspace["current_revision_id"],
                    document["checksum"],
                    canonical_json(document),
                    canonical_json(document["assets"]),
                    created_at,
                ),
            )
            next_order = connection.execute(
                "SELECT COALESCE(MAX(work_order_id), 0) + 1 AS value FROM work_orders"
            ).fetchone()["value"]
            request_id = f"req-{next_order:04d}"
            work_order = {
                "schema": WORK_ORDER_SCHEMA,
                "request_id": request_id,
                "workspace_id": review_id,
                "operation": "realize_voice_timeline",
                "base_revision": package["revision"],
                "base_checksum": document["checksum"],
                "timing_profile": document["timing_profile"],
                "target_block_ids": "all",
                "instructions": [],
                "required_outputs": ["document", "block_audio", "word_alignment", "timing_audit"],
                "document": document,
            }
            connection.execute(
                """INSERT INTO work_orders
                   (request_id, review_id, revision_id, base_revision, base_checksum, operation, status, request_json, candidate_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    request_id,
                    review_id,
                    cursor.lastrowid,
                    package["revision"],
                    document["checksum"],
                    "realize_voice_timeline",
                    "pending",
                    canonical_json(work_order),
                    created_at,
                ),
            )
            connection.execute(
                "UPDATE workspaces SET stage = ?, current_revision_id = ? WHERE review_id = ?",
                ("voice_realization_pending", cursor.lastrowid, review_id),
            )
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise FrameCueError(f"workspace database error: {error}") from error
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()
    return {
        "workspace_id": review_id,
        "stage": "voice_realization_pending",
        "request_id": request_id,
        "revision": package["revision"],
        "checksum": document["checksum"],
    }


def command_content_complete(args):
    result = read_json(args.result)
    print(json.dumps(complete_content_revision(args.database, args.review_id, result), ensure_ascii=False))


def make_workspace_server(database, bundle_dir, port=0):
    directory = Path(bundle_dir).expanduser().resolve()
    package, _ = bundle_summary(directory / "review_package.json")
    review_id = package["review_id"]
    connection = open_workspace_database(database)
    try:
        workspace_row(connection, review_id)
    finally:
        connection.close()
    csrf_token = secrets.token_urlsafe(32)

    class WorkspaceHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            self._range_length = None
            super().__init__(*args, directory=str(directory), **kwargs)

        def translate_path(self, path):
            request_path = unquote(urlsplit(path).path)
            if request_path in {"/", "/index.html"}:
                return str(DIST_DIR / "index.html")
            if request_path.startswith("/assets/"):
                viewer_assets = (DIST_DIR / "assets").resolve()
                candidate = (DIST_DIR / request_path.lstrip("/")).resolve()
                if candidate.is_file() and candidate.is_relative_to(viewer_assets):
                    return str(candidate)
            return super().translate_path(path)

        def send_json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_unsatisfiable_range(self, size):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def send_head(self):
            self._range_length = None
            range_header = self.headers.get("Range")
            path = self.translate_path(self.path)
            if not range_header or not Path(path).is_file():
                return super().send_head()
            file = open(path, "rb")
            file.seek(0, 2)
            size = file.tell()
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or not size:
                file.close()
                return self.send_unsatisfiable_range(size)
            start_text, end_text = match.groups()
            if not start_text and not end_text:
                file.close()
                return self.send_unsatisfiable_range(size)
            if start_text:
                start = int(start_text)
                end = min(int(end_text), size - 1) if end_text else size - 1
            else:
                suffix = int(end_text)
                start = max(0, size - suffix)
                end = size - 1
            if start >= size or start > end or (not start_text and not int(end_text)):
                file.close()
                return self.send_unsatisfiable_range(size)
            self._range_length = end - start + 1
            file.seek(start)
            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(self._range_length))
            self.end_headers()
            return file

        def copyfile(self, source, outputfile):
            if self._range_length is None:
                return super().copyfile(source, outputfile)
            remaining = self._range_length
            while remaining:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)

        def do_GET(self):
            if self.path != "/api/workspace":
                return super().do_GET()
            connection = open_workspace_database(database)
            try:
                workspace = workspace_row(connection, review_id)
            finally:
                connection.close()
            self.send_json(200, {
                "mode": "server",
                "workspace_id": review_id,
                "stage": workspace["stage"],
                "content_complete_endpoint": "/api/content-complete",
                "csrf_token": csrf_token,
                "endpoint": "/api/content-complete",
                "csrf": csrf_token,
            })

        def do_POST(self):
            if self.path != "/api/content-complete":
                self.send_error(404)
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self.send_error(415)
                return
            try:
                content_length = int(self.headers["Content-Length"])
            except (KeyError, TypeError, ValueError):
                self.send_error(411)
                return
            if content_length < 0:
                self.send_error(400)
                return
            if content_length > 10 * 1024 * 1024:
                self.send_error(413)
                return
            origin = self.headers.get("Origin", "")
            parsed_origin = urlsplit(origin)
            same_host = (
                parsed_origin.scheme in {"http", "https"}
                and parsed_origin.netloc == self.headers.get("Host", "")
                and not parsed_origin.path
                and not parsed_origin.query
                and not parsed_origin.fragment
            )
            token = self.headers.get("X-FrameCue-CSRF", "")
            if not same_host or not hmac.compare_digest(token, csrf_token):
                self.send_error(403)
                return
            try:
                result = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400)
                return
            try:
                summary = complete_content_revision(database, review_id, result)
            except FrameCueError as error:
                self.send_json(409, {"error": str(error)})
                return
            self.send_json(200, summary)

    server = ThreadingHTTPServer(("127.0.0.1", port), WorkspaceHandler)
    return server


def command_workspace_serve(args):
    server = make_workspace_server(args.database, args.dir, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def command_work_pull(args):
    connection = open_workspace_database(args.database)
    try:
        try:
            workspace_row(connection, args.review_id)
            orders = connection.execute(
                """SELECT request_id, request_json FROM work_orders
                   WHERE review_id = ? AND status = 'pending'
                   ORDER BY work_order_id DESC LIMIT 2""",
                (args.review_id,),
            ).fetchall()
        except sqlite3.Error as error:
            raise FrameCueError(f"workspace database error: {error}") from error
    finally:
        connection.close()
    if not orders:
        raise FrameCueError(f"workspace has no pending work order: {args.review_id}")
    if len(orders) != 1:
        raise FrameCueError(f"workspace has multiple pending work orders: {args.review_id}")
    try:
        work_order = json.loads(orders[0]["request_json"])
    except json.JSONDecodeError as error:
        raise FrameCueError(f"work order is invalid JSON: {orders[0]['request_id']}") from error
    if work_order.get("request_id") != orders[0]["request_id"]:
        raise FrameCueError(f"work order request ID does not match storage: {orders[0]['request_id']}")
    try:
        out_path = Path(args.out).expanduser().resolve()
        if out_path.exists():
            raise FrameCueError(f"refusing to overwrite work order: {out_path}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(out_path, work_order)
    except OSError as error:
        raise FrameCueError(f"unable to write work order: {out_path}: {error}") from error
    print(json.dumps({
        "workspace_id": args.review_id,
        "request_id": orders[0]["request_id"],
        "out": str(out_path),
    }, ensure_ascii=False))


def verify_candidate_asset(candidate_root, asset, label):
    if not isinstance(asset, dict):
        raise FrameCueError(f"{label} evidence must be an object")
    path_value = asset.get("path")
    checksum = asset.get("sha256")
    if not isinstance(path_value, str) or not path_value:
        raise FrameCueError(f"{label} evidence path is invalid")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise FrameCueError(f"{label} evidence sha256 is invalid")
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = candidate_root / path
    path = path.resolve()
    if not path.is_file():
        raise FrameCueError(f"{label} evidence file is missing: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != checksum:
        raise FrameCueError(f"{label} evidence sha256 does not match: {path}")
    return path


def validate_candidate_wav(path, label):
    try:
        with wave.open(str(path), "rb") as audio:
            frame = audio.readframes(1)
            if audio.getnframes() < 1 or len(frame) != audio.getnchannels() * audio.getsampwidth():
                raise FrameCueError(f"{label} evidence WAV is empty or truncated: {path}")
    except (EOFError, wave.Error) as error:
        raise FrameCueError(f"{label} evidence is not a decodable WAV: {path}") from error


def validate_word_alignment_evidence(path, order, document):
    alignment = read_json(path)
    if not isinstance(alignment, dict):
        raise FrameCueError("candidate word alignment evidence must be an object")
    if alignment.get("schema") != "agenticdub_word_alignment_v1":
        raise FrameCueError("candidate word alignment evidence schema is invalid")
    if alignment.get("request_id") != order["request_id"]:
        raise FrameCueError("candidate word alignment evidence request_id does not match work order")
    if alignment.get("base_checksum") != order["base_checksum"]:
        raise FrameCueError("candidate word alignment evidence base_checksum does not match work order")
    if alignment.get("document_checksum") != document["checksum"]:
        raise FrameCueError("candidate word alignment evidence document_checksum does not match candidate")
    expected_cues = [{
        "id": cue["id"],
        "start_ms": cue["output_start_ms"],
        "end_ms": cue["output_end_ms"],
    } for cue in document["cues"]]
    if canonical_json(alignment.get("cues")) != canonical_json(expected_cues):
        raise FrameCueError("candidate word alignment evidence cue ranges do not match candidate")


def validate_timing_audit_evidence(path, order, document):
    audit = read_json(path)
    if not isinstance(audit, dict):
        raise FrameCueError("candidate timing audit evidence must be an object")
    if audit.get("schema") != "agenticdub_timing_audit_v1":
        raise FrameCueError("candidate timing audit evidence schema is invalid")
    if audit.get("request_id") != order["request_id"]:
        raise FrameCueError("candidate timing audit evidence request_id does not match work order")
    if audit.get("base_checksum") != order["base_checksum"]:
        raise FrameCueError("candidate timing audit evidence base_checksum does not match work order")
    if audit.get("document_checksum") != document["checksum"]:
        raise FrameCueError("candidate timing audit evidence document_checksum does not match candidate")
    if audit.get("timing_profile") != document["timing_profile"]:
        raise FrameCueError("candidate timing audit evidence timing_profile does not match candidate")
    if audit.get("status") != "passed":
        raise FrameCueError("candidate timing audit evidence status is not passed")
    if type(audit.get("overlap_count")) is not int or audit["overlap_count"] != 0:
        raise FrameCueError("candidate timing audit evidence overlap_count is not zero")


def validate_candidate_evidence(candidate, order, document, base_blocks, candidate_root):
    validation = candidate.get("validation")
    if not isinstance(validation, dict):
        raise FrameCueError("candidate validation must be an object")
    if validation.get("word_alignment_status") != "passed":
        raise FrameCueError("candidate word_alignment_status is not passed")
    if validation.get("timing_audit_status") != "passed":
        raise FrameCueError("candidate timing_audit_status is not passed")

    assets = candidate.get("assets")
    if not isinstance(assets, dict):
        raise FrameCueError("candidate assets must be an object")
    block_audio = assets.get("block_audio")
    if not isinstance(block_audio, list) or not all(isinstance(asset, dict) for asset in block_audio):
        raise FrameCueError("candidate block_audio evidence must be an array of objects")
    block_ids = [block.get("id") for block in base_blocks]
    if [asset.get("block_id") for asset in block_audio] != block_ids:
        raise FrameCueError("candidate block_audio evidence IDs do not match base blocks")
    for asset in block_audio:
        label = f"candidate block audio {asset['block_id']}"
        validate_candidate_wav(verify_candidate_asset(candidate_root, asset, label), label)
    word_alignment = verify_candidate_asset(candidate_root, assets.get("word_alignment"), "candidate word alignment")
    validate_word_alignment_evidence(word_alignment, order, document)
    timing_audit = verify_candidate_asset(candidate_root, assets.get("timing_audit"), "candidate timing audit")
    validate_timing_audit_evidence(timing_audit, order, document)


def validate_candidate_document_immutable(base_document, document):
    expected = copy.deepcopy(base_document)
    submitted = copy.deepcopy(document)
    expected.pop("checksum", None)
    submitted.pop("checksum", None)
    submitted["revision_kind"] = expected.get("revision_kind")
    for base_cue, candidate_cue in zip(expected["cues"], submitted["cues"]):
        candidate_cue["output_start_ms"] = base_cue.get("output_start_ms")
        candidate_cue["output_end_ms"] = base_cue.get("output_end_ms")
    if canonical_json(submitted) != canonical_json(expected):
        raise FrameCueError("candidate document changes immutable base fields")


def command_work_submit(args):
    candidate = read_json(args.candidate)
    if not isinstance(candidate, dict):
        raise FrameCueError("candidate revision must be an object")
    request_id = candidate.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise FrameCueError("candidate request_id is required")

    connection = open_workspace_database(args.database)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            orders = connection.execute(
                """SELECT work_orders.work_order_id, work_orders.request_id, work_orders.review_id,
                          work_orders.base_revision, work_orders.base_checksum, revisions.document_json
                   FROM work_orders
                   JOIN revisions ON revisions.revision_id = work_orders.revision_id
                   WHERE work_orders.request_id = ? AND work_orders.status = 'pending'""",
                (request_id,),
            ).fetchall()
            if not orders:
                raise FrameCueError(f"work order is not pending: {request_id}")
            if len(orders) != 1:
                raise FrameCueError(f"work order is not unique: {request_id}")
            order = orders[0]

            if candidate.get("schema") != CANDIDATE_REVISION_SCHEMA:
                raise FrameCueError("candidate schema is invalid")
            if candidate.get("status") != "ready_for_review":
                raise FrameCueError("candidate status is not ready_for_review")
            if candidate.get("base_revision") != order["base_revision"]:
                raise FrameCueError("candidate base_revision does not match work order")
            if candidate.get("base_checksum") != order["base_checksum"]:
                raise FrameCueError("candidate base_checksum does not match work order")
            try:
                base_document = json.loads(order["document_json"])
            except json.JSONDecodeError as error:
                raise FrameCueError(f"work order base document is invalid JSON: {request_id}") from error
            if not isinstance(base_document, dict):
                raise FrameCueError(f"work order base document is invalid: {request_id}")
            if (
                base_document.get("checksum") != order["base_checksum"]
                or document_checksum(base_document) != order["base_checksum"]
            ):
                raise FrameCueError(f"work order base checksum does not match storage: {request_id}")

            document = candidate.get("document")
            if not isinstance(document, dict):
                raise FrameCueError("candidate document must be an object")
            if document.get("schema") != SUBTITLE_DOCUMENT_SCHEMA:
                raise FrameCueError("candidate document schema is invalid")
            if document.get("revision_kind") != "voice_aligned":
                raise FrameCueError("candidate document revision_kind is invalid")
            if document.get("workspace_id") != order["review_id"]:
                raise FrameCueError("candidate document workspace_id does not match work order")
            if document.get("checksum") != document_checksum(document):
                raise FrameCueError("candidate document checksum does not match content")

            base_cues = base_document.get("cues")
            candidate_cues = document.get("cues")
            base_blocks = base_document.get("blocks")
            candidate_blocks = document.get("blocks")
            if not all(isinstance(rows, list) for rows in (base_cues, candidate_cues, base_blocks, candidate_blocks)):
                raise FrameCueError("candidate document cues and blocks must be arrays")
            if not all(isinstance(row, dict) for row in base_cues + base_blocks):
                raise FrameCueError(f"work order base document is invalid: {request_id}")
            if not all(isinstance(row, dict) for row in candidate_cues + candidate_blocks):
                raise FrameCueError("candidate document cues and blocks must contain objects")
            if [cue.get("id") for cue in candidate_cues] != [cue.get("id") for cue in base_cues]:
                raise FrameCueError("candidate cue IDs do not match base document")
            if [block.get("id") for block in candidate_blocks] != [block.get("id") for block in base_blocks]:
                raise FrameCueError("candidate block IDs do not match base document")
            previous_output_end = None
            for cue in candidate_cues:
                output_start = cue.get("output_start_ms")
                output_end = cue.get("output_end_ms")
                if type(output_start) is not int or output_start < 0:
                    raise FrameCueError(f"candidate cue output_start_ms is invalid: {cue.get('id')}")
                if type(output_end) is not int or output_end < 0:
                    raise FrameCueError(f"candidate cue output_end_ms is invalid: {cue.get('id')}")
                if output_start >= output_end:
                    raise FrameCueError(f"candidate cue output range is invalid: {cue.get('id')}")
                if previous_output_end is not None and output_start < previous_output_end:
                    raise FrameCueError(f"candidate cue output ranges overlap: {cue.get('id')}")
                previous_output_end = output_end

            validate_candidate_document_immutable(base_document, document)
            candidate_root = Path(args.candidate).expanduser().resolve().parent
            validate_candidate_evidence(candidate, order, document, base_blocks, candidate_root)

            if connection.execute(
                """UPDATE work_orders SET status = ?, candidate_json = ?
                   WHERE work_order_id = ? AND status = 'pending'""",
                ("candidate_ready", canonical_json(candidate), order["work_order_id"]),
            ).rowcount != 1:
                raise FrameCueError(f"work order is not pending: {request_id}")
            if connection.execute(
                "UPDATE workspaces SET stage = ? WHERE review_id = ?",
                ("audiovisual_review", order["review_id"]),
            ).rowcount != 1:
                raise FrameCueError(f"workspace not found: {order['review_id']}")
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise FrameCueError(f"workspace database error: {error}") from error
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()
    print(json.dumps({
        "stage": "audiovisual_review",
        "status": "candidate_ready",
        "request_id": request_id,
    }, ensure_ascii=False))


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
        subprocess.run([miniserve, "--interfaces", "127.0.0.1", "--port", str(args.port), "--index", "index.html", str(directory)], check=True)
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

    workspace_import = commands.add_parser("workspace-import", help="import an immutable v2 package into a local subtitle workspace")
    workspace_import.add_argument("--database", required=True)
    workspace_import.add_argument("--package", required=True)
    workspace_import.add_argument("--timing-profile", choices=sorted(TIMING_PROFILES), required=True)
    workspace_import.set_defaults(func=command_workspace_import)

    workspace_apply = commands.add_parser("workspace-apply", help="apply one versioned Workspace v2 draft operation")
    workspace_apply.add_argument("--database", required=True)
    workspace_apply.add_argument("--review-id", required=True)
    workspace_apply.add_argument("--operation", required=True)
    workspace_apply.set_defaults(func=command_workspace_apply)

    workspace_complete = commands.add_parser("workspace-complete", help="complete one versioned Workspace v2 content round")
    workspace_complete.add_argument("--database", required=True)
    workspace_complete.add_argument("--review-id", required=True)
    workspace_complete.add_argument("--draft-version", type=int, required=True)
    workspace_complete.set_defaults(func=command_workspace_complete)

    content_complete = commands.add_parser("content-complete", help="save an approved content revision and create its pending work order")
    content_complete.add_argument("--database", required=True)
    content_complete.add_argument("--review-id", required=True)
    content_complete.add_argument("--result", required=True)
    content_complete.set_defaults(func=command_content_complete)

    workspace_serve = commands.add_parser("workspace-serve", help="serve one local subtitle workspace")
    workspace_serve.add_argument("--database", required=True)
    workspace_serve.add_argument("--dir", required=True)
    workspace_serve.add_argument("--port", type=int, default=3069)
    workspace_serve.set_defaults(func=command_workspace_serve)

    work_pull = commands.add_parser("work-pull", help="write the workspace's pending work order")
    work_pull.add_argument("--database", required=True)
    work_pull.add_argument("--review-id", required=True)
    work_pull.add_argument("--out", required=True)
    work_pull.set_defaults(func=command_work_pull)

    work_submit = commands.add_parser("work-submit", help="save a voice-aligned candidate revision for audiovisual review")
    work_submit.add_argument("--database", required=True)
    work_submit.add_argument("--candidate", required=True)
    work_submit.set_defaults(func=command_work_submit)

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
