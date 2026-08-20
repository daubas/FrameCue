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
import threading
import time
import unicodedata
import wave
from http.cookies import CookieError, SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


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


def agent_utc_now():
    return utc_now()


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
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_drafts (
                review_id TEXT PRIMARY KEY REFERENCES workspaces(review_id),
                draft_version INTEGER NOT NULL,
                document_json TEXT NOT NULL,
                issues_json TEXT NOT NULL,
                direct_changes_json TEXT NOT NULL,
                frozen_revision_id INTEGER REFERENCES revisions(revision_id)
            );
            CREATE TABLE IF NOT EXISTS agent_tokens (
                token_id TEXT PRIMARY KEY,
                token_hash TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL,
                workspace_ids_json TEXT NOT NULL,
                permissions_json TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL
            );
        """)
        work_orders_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'work_orders'"
        ).fetchone()["sql"]
        if "unique(review_id,base_revision,base_checksum,operation)" in re.sub(r"\s+", "", work_orders_sql.lower()):
            connection.executescript("""
                BEGIN;
                DROP TABLE IF EXISTS work_orders_without_base_unique;
                CREATE TABLE work_orders_without_base_unique (
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
                    created_at TEXT NOT NULL
                );
                INSERT INTO work_orders_without_base_unique
                    (work_order_id, request_id, review_id, revision_id, base_revision, base_checksum,
                     operation, status, request_json, candidate_json, created_at)
                SELECT work_order_id, request_id, review_id, revision_id, base_revision, base_checksum,
                       operation, status, request_json, candidate_json, created_at
                FROM work_orders;
                DROP TABLE work_orders;
                ALTER TABLE work_orders_without_base_unique RENAME TO work_orders;
                COMMIT;
            """)
        work_order_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(work_orders)")
        }
        for name, declaration in (
            ("lease_owner_token_id", "TEXT REFERENCES agent_tokens(token_id)"),
            ("lease_expires_at", "TEXT"),
            ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_error_json", "TEXT"),
        ):
            if name not in work_order_columns:
                connection.execute(f"ALTER TABLE work_orders ADD COLUMN {name} {declaration}")
        connection.commit()
        return connection
    except (OSError, sqlite3.Error) as error:
        if connection is not None:
            connection.close()
        raise FrameCueError(f"workspace database error: {error}") from error


def insert_workspace_revision(connection, review_id, revision, kind, parent_revision_id, document, created_at):
    document_json = canonical_json(document)
    connection.execute(
        """INSERT OR IGNORE INTO revisions
           (review_id, revision, kind, parent_revision_id, checksum, document_json, assets_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            review_id, revision, kind, parent_revision_id, document["checksum"], document_json,
            canonical_json(document["assets"]), created_at,
        ),
    )
    row = connection.execute(
        "SELECT revision_id, document_json FROM revisions WHERE review_id = ? AND kind = ? AND checksum = ?",
        (review_id, kind, document["checksum"]),
    ).fetchone()
    if row is None or row["document_json"] != document_json:
        raise FrameCueError("workspace revision checksum collision")
    return row["revision_id"]


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
            revision_id = insert_workspace_revision(
                connection, review_id, document["revision"], document["revision_kind"],
                workspace["current_revision_id"], document, created_at,
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
                    revision_id,
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
                (revision_id, review_id, draft_version),
            ).rowcount != 1:
                raise FrameCueError(f"workspace draft version is stale: {review_id}")
            if connection.execute(
                """UPDATE workspaces SET stage = ?, current_revision_id = ?
                   WHERE review_id = ? AND stage = 'content_review'""",
                (stage, workspace["current_revision_id"] if needs_correction else revision_id, review_id),
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


def workspace_snapshot(database, review_id, csrf_token):
    connection = open_workspace_database(database)
    try:
        workspace = workspace_row(connection, review_id)
        draft = draft_row(connection, workspace)
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise FrameCueError(f"workspace database error: {error}") from error
    finally:
        connection.close()
    return {
        "schema": "framecue_workspace_snapshot_v2",
        "workspace_id": review_id,
        "stage": workspace["stage"],
        "draft_version": draft["draft_version"],
        "csrf_token": csrf_token,
        "document": draft["document"],
        "issues": draft["issues"],
        "direct_edit_count": len(draft["direct_changes"]),
    }


class WorkspaceCollaboration:
    SESSION_TTL_SECONDS = 20
    LOCK_TTL_SECONDS = 15

    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.sessions = {}
        self.locks = {}
        self.lead_session_id = None
        self.version = 0

    def _expire(self):
        now = time.monotonic()
        expired = [
            session_id for session_id, session in self.sessions.items()
            if session["last_seen"] + self.SESSION_TTL_SECONDS <= now
        ]
        for session_id in expired:
            del self.sessions[session_id]
        locks = {
            cue_id: lock for cue_id, lock in self.locks.items()
            if lock["session_id"] in self.sessions and lock["expires_at"] > now
        }
        changed = bool(expired) or locks != self.locks
        self.locks = locks
        return changed

    def expire(self):
        if self._expire():
            self.changed()

    def _display_name(self, requested, exclude_session_id=None):
        name = requested.strip() if isinstance(requested, str) else ""
        if not name:
            name = "Reviewer"
        if len(name) > 80 or any(char in name for char in "\r\n"):
            raise FrameCueError("workspace display name is invalid")
        taken = {
            session["display_name"] for session_id, session in self.sessions.items()
            if session_id != exclude_session_id
        }
        if name not in taken:
            return name
        suffix = 2
        while f"{name} {suffix}" in taken:
            suffix += 1
        return f"{name} {suffix}"

    def register_or_refresh(self, session_id, display_name):
        self.expire()
        session = self.sessions.get(session_id)
        if session is not None:
            session["last_seen"] = time.monotonic()
            return session
        session_id = secrets.token_urlsafe(24)
        session = {
            "session_id": session_id,
            "display_name": self._display_name(display_name),
            "dirty": False,
            "selected_cue_id": "",
            "last_seen": time.monotonic(),
        }
        self.sessions[session_id] = session
        if self.lead_session_id not in self.sessions:
            self.lead_session_id = session_id
        self.changed()
        return session

    def session(self, session_id):
        self.expire()
        session = self.sessions.get(session_id)
        if session is not None:
            session["last_seen"] = time.monotonic()
        return session

    def changed(self):
        self.version += 1
        self.condition.notify_all()

    def snapshot_fields(self, session_id):
        self.expire()
        session = self.sessions[session_id]
        return {
            "session_id": session_id,
            "display_name": session["display_name"],
            "lead_session_id": self.lead_session_id,
            "lead_active": self.lead_session_id in self.sessions,
            "participants": [{
                "session_id": session["session_id"],
                "display_name": session["display_name"],
                "dirty": session["dirty"],
                "selected_cue_id": session["selected_cue_id"],
            } for session in self.sessions.values()],
            "locks": [{
                "cue_id": cue_id,
                "session_id": lock["session_id"],
            } for cue_id, lock in self.locks.items()],
            "snapshot_version": self.version,
        }

    def set_presence(self, session, operation, cue_ids):
        selected_cue_id = operation.get("selected_cue_id", session["selected_cue_id"])
        if not isinstance(selected_cue_id, str) or (selected_cue_id and selected_cue_id not in cue_ids):
            raise FrameCueError("workspace selected Cue is invalid")
        display_name = operation.get("display_name", session["display_name"])
        if not isinstance(display_name, str):
            raise FrameCueError("workspace display name is invalid")
        next_name = self._display_name(display_name, session["session_id"])
        if session["selected_cue_id"] != selected_cue_id or session["display_name"] != next_name:
            session["selected_cue_id"] = selected_cue_id
            session["display_name"] = next_name
            self.changed()

    def set_dirty(self, session, dirty):
        if type(dirty) is not bool:
            raise FrameCueError("workspace dirty state is invalid")
        if session["dirty"] != dirty:
            session["dirty"] = dirty
            self.changed()

    def lock(self, session, cue_ids):
        self.expire()
        now = time.monotonic()
        for cue_id in cue_ids:
            lock = self.locks.get(cue_id)
            if lock is not None and lock["session_id"] != session["session_id"]:
                raise FrameCueError(f"workspace Cue is locked: {cue_id}")
        for cue_id in cue_ids:
            self.locks[cue_id] = {
                "session_id": session["session_id"],
                "expires_at": now + self.LOCK_TTL_SECONDS,
            }
        self.changed()

    def unlock(self, session, cue_ids):
        changed = False
        for cue_id in cue_ids:
            lock = self.locks.get(cue_id)
            if lock is not None and lock["session_id"] != session["session_id"]:
                raise FrameCueError(f"workspace Cue lock belongs to another session: {cue_id}")
            if lock is not None and lock["session_id"] == session["session_id"]:
                del self.locks[cue_id]
                changed = True
        if changed:
            self.changed()

    def assert_unlocked(self, session, cue_ids):
        self.expire()
        for cue_id in cue_ids:
            lock = self.locks.get(cue_id)
            if lock is not None and lock["session_id"] != session["session_id"]:
                raise FrameCueError(f"workspace Cue is locked: {cue_id}")

    def transfer_lead(self, session, expected_lead_session_id, new_lead_session_id):
        self.expire()
        if expected_lead_session_id != self.lead_session_id:
            raise FrameCueError("workspace lead changed; reload the latest snapshot")
        target_session_id = new_lead_session_id or session["session_id"]
        if target_session_id not in self.sessions:
            raise FrameCueError("workspace lead target is not connected")
        current_lead = self.sessions.get(self.lead_session_id)
        if current_lead is not None and session["session_id"] != self.lead_session_id:
            raise FrameCueError("only the workspace lead can transfer this role")
        if current_lead is None and target_session_id != session["session_id"]:
            raise FrameCueError("a replacement lead must claim the role directly")
        if self.lead_session_id != target_session_id:
            self.lead_session_id = target_session_id
            self.changed()

    def completion_error(self):
        self.expire()
        if any(session["dirty"] for session in self.sessions.values()):
            return "workspace completion is blocked by unsynchronized edits"
        if self.locks:
            return "workspace completion is blocked by active Cue locks"
        return ""


def expire_agent_leases(connection, now):
    connection.execute(
        """UPDATE work_orders
           SET status = 'pending', lease_owner_token_id = NULL, lease_expires_at = NULL
           WHERE status = 'processing' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?""",
        (now,),
    )


def agent_work_order_value(row):
    try:
        value = json.loads(row["request_json"])
    except json.JSONDecodeError as error:
        raise FrameCueError(f"work order is invalid JSON: {row['request_id']}") from error
    value.update({
        "status": row["status"],
        "lease_owner_token_id": row["lease_owner_token_id"],
        "lease_expires_at": row["lease_expires_at"],
        "attempt_count": row["attempt_count"],
    })
    return value


def agent_work_order_metadata(row):
    return {
        "request_id": row["request_id"],
        "workspace_id": row["review_id"],
        "operation": row["operation"],
        "base_revision": row["base_revision"],
        "base_checksum": row["base_checksum"],
        "status": row["status"],
        "lease_owner_token_id": row["lease_owner_token_id"],
        "lease_expires_at": row["lease_expires_at"],
        "attempt_count": row["attempt_count"],
    }


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
    collaboration = WorkspaceCollaboration()

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

        def send_json(self, status, value, headers=()):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, header_value in headers:
                self.send_header(name, header_value)
            self.end_headers()
            self.wfile.write(body)

        def request_session_id(self):
            header = self.headers.get("X-FrameCue-Session", "").strip()
            if header:
                return header
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                morsel = cookies.get("framecue_session")
                return morsel.value if morsel is not None else ""
            except CookieError:
                return ""

        def read_workspace_json(self):
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self.send_error(415)
                return None
            try:
                content_length = int(self.headers["Content-Length"])
            except (KeyError, TypeError, ValueError):
                self.send_error(411)
                return None
            if content_length < 0:
                self.send_error(400)
                return None
            if content_length > 10 * 1024 * 1024:
                self.send_error(413)
                return None
            try:
                value = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400)
                return None
            if not isinstance(value, dict):
                self.send_error(400)
                return None
            return value

        def has_valid_csrf(self):
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
            return same_host and hmac.compare_digest(token, csrf_token)

        def require_agent(self, permission):
            match = re.fullmatch(r"Bearer\s+(\S+)", self.headers.get("Authorization", ""))
            if match is None:
                self.send_json(401, {"error": "agent bearer token is required"})
                return None
            token_hash = hashlib.sha256(match.group(1).encode("utf-8")).hexdigest()
            connection = open_workspace_database(database)
            try:
                row = connection.execute(
                    """SELECT token_id, label, workspace_ids_json, permissions_json, revoked_at
                       FROM agent_tokens WHERE token_hash = ?""",
                    (token_hash,),
                ).fetchone()
            finally:
                connection.close()
            if row is None or row["revoked_at"] is not None:
                self.send_json(401, {"error": "agent bearer token is invalid"})
                return None
            try:
                workspace_ids = json.loads(row["workspace_ids_json"])
                permissions = json.loads(row["permissions_json"])
            except json.JSONDecodeError:
                self.send_json(401, {"error": "agent bearer token is invalid"})
                return None
            if (
                not isinstance(workspace_ids, list)
                or not all(isinstance(value, str) for value in workspace_ids)
                or not isinstance(permissions, list)
                or not all(isinstance(value, str) for value in permissions)
            ):
                self.send_json(401, {"error": "agent bearer token is invalid"})
                return None
            if permission not in permissions:
                self.send_json(403, {"error": f"agent token lacks {permission} permission"})
                return None
            return {"token_id": row["token_id"], "label": row["label"], "workspace_ids": workspace_ids}

        def agent_is_active(self, connection, identity):
            row = connection.execute(
                "SELECT revoked_at FROM agent_tokens WHERE token_id = ?", (identity["token_id"],)
            ).fetchone()
            return row is not None and row["revoked_at"] is None

        def send_agent_list(self, identity, workspace_id):
            if workspace_id not in identity["workspace_ids"]:
                self.send_json(403, {"error": "agent token is not allowed for this workspace"})
                return
            connection = open_workspace_database(database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not self.agent_is_active(connection, identity):
                    connection.rollback()
                    self.send_json(401, {"error": "agent bearer token is invalid"})
                    return
                expire_agent_leases(connection, agent_utc_now())
                rows = connection.execute(
                    """SELECT request_id, review_id, operation, base_revision, base_checksum,
                              status, lease_owner_token_id, lease_expires_at, attempt_count
                       FROM work_orders WHERE review_id = ? ORDER BY work_order_id""",
                    (workspace_id,),
                ).fetchall()
                connection.commit()
                values = [agent_work_order_metadata(row) for row in rows]
            except (sqlite3.Error, FrameCueError) as error:
                connection.rollback()
                self.send_json(409, {"error": str(error)})
                return
            finally:
                connection.close()
            self.send_json(200, {"work_orders": values})

        def send_agent_read(self, identity, request_id):
            connection = open_workspace_database(database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not self.agent_is_active(connection, identity):
                    connection.rollback()
                    self.send_json(401, {"error": "agent bearer token is invalid"})
                    return
                expire_agent_leases(connection, agent_utc_now())
                row = connection.execute(
                    """SELECT request_id, review_id, request_json, status, lease_owner_token_id,
                              lease_expires_at, attempt_count
                       FROM work_orders WHERE request_id = ?""",
                    (request_id,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    self.send_json(404, {"error": "work order was not found"})
                    return
                if row["review_id"] not in identity["workspace_ids"]:
                    connection.rollback()
                    self.send_json(403, {"error": "agent token is not allowed for this workspace"})
                    return
                connection.commit()
                value = agent_work_order_value(row)
            except (sqlite3.Error, FrameCueError) as error:
                connection.rollback()
                self.send_json(409, {"error": str(error)})
                return
            finally:
                connection.close()
            self.send_json(200, value)

        def send_agent_claim(self, identity, request_id):
            now = agent_utc_now()
            try:
                lease_expires_at = (
                    dt.datetime.fromisoformat(now) + dt.timedelta(seconds=300)
                ).replace(microsecond=0).isoformat()
            except ValueError:
                self.send_json(500, {"error": "agent clock is invalid"})
                return
            connection = open_workspace_database(database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not self.agent_is_active(connection, identity):
                    connection.rollback()
                    self.send_json(401, {"error": "agent bearer token is invalid"})
                    return
                expire_agent_leases(connection, now)
                row = connection.execute(
                    "SELECT request_id, review_id, status, lease_owner_token_id FROM work_orders WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    self.send_json(404, {"error": "work order was not found"})
                    return
                if row["review_id"] not in identity["workspace_ids"]:
                    connection.rollback()
                    self.send_json(403, {"error": "agent token is not allowed for this workspace"})
                    return
                if row["status"] == "pending":
                    connection.execute(
                        """UPDATE work_orders
                           SET status = 'processing', lease_owner_token_id = ?, lease_expires_at = ?,
                               attempt_count = attempt_count + 1
                           WHERE request_id = ? AND status = 'pending'""",
                        (identity["token_id"], lease_expires_at, request_id),
                    )
                elif row["status"] == "processing" and row["lease_owner_token_id"] == identity["token_id"]:
                    connection.execute(
                        "UPDATE work_orders SET lease_expires_at = ? WHERE request_id = ?",
                        (lease_expires_at, request_id),
                    )
                else:
                    connection.rollback()
                    self.send_json(409, {"error": "work order is already claimed or unavailable"})
                    return
                claimed = connection.execute(
                    """SELECT request_id, review_id, operation, base_revision, base_checksum,
                              status, lease_owner_token_id, lease_expires_at, attempt_count
                       FROM work_orders WHERE request_id = ?""",
                    (request_id,),
                ).fetchone()
                connection.commit()
                value = agent_work_order_metadata(claimed)
            except (sqlite3.Error, FrameCueError) as error:
                connection.rollback()
                self.send_json(409, {"error": str(error)})
                return
            finally:
                connection.close()
            self.send_json(200, value)

        def send_agent_submit(self, identity, request_id, candidate):
            now = agent_utc_now()
            connection = open_workspace_database(database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not self.agent_is_active(connection, identity):
                    connection.rollback()
                    self.send_json(401, {"error": "agent bearer token is invalid"})
                    return
                expire_agent_leases(connection, now)
                order = connection.execute(
                    """SELECT work_orders.*, revisions.document_json
                       FROM work_orders
                       JOIN revisions ON revisions.revision_id = work_orders.revision_id
                       WHERE work_orders.request_id = ?""",
                    (request_id,),
                ).fetchone()
                if order is None:
                    connection.commit()
                    self.send_json(404, {"error": "work order was not found"})
                    return
                if order["review_id"] not in identity["workspace_ids"]:
                    connection.commit()
                    self.send_json(403, {"error": "agent token is not allowed for this workspace"})
                    return
                if (
                    order["status"] != "processing"
                    or order["lease_owner_token_id"] != identity["token_id"]
                    or order["lease_expires_at"] is None
                    or order["lease_expires_at"] <= now
                ):
                    connection.commit()
                    self.send_json(409, {"error": "work order lease is not owned by this agent"})
                    return
                store_content_candidate_v2(connection, order, candidate, "processing", identity["token_id"])
                connection.commit()
            except (sqlite3.Error, FrameCueError) as error:
                connection.rollback()
                self.send_json(409, {"error": str(error)})
                return
            finally:
                connection.close()
            self.send_json(200, {
                "stage": "content_candidate_review",
                "status": "candidate_ready",
                "request_id": request_id,
            })

        def send_agent_fail(self, identity, request_id, value):
            category = value.get("category")
            message = value.get("message")
            retryable = value.get("retryable")
            if (
                not isinstance(category, str) or not 1 <= len(category.strip()) <= 64
                or not isinstance(message, str) or not 1 <= len(message.strip()) <= 2000
                or type(retryable) is not bool
            ):
                self.send_json(409, {"error": "agent failure must include a valid category, message, and retryable flag"})
                return
            last_error = {"category": category.strip(), "message": message.strip(), "retryable": retryable}
            now = agent_utc_now()
            connection = open_workspace_database(database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not self.agent_is_active(connection, identity):
                    connection.rollback()
                    self.send_json(401, {"error": "agent bearer token is invalid"})
                    return
                expire_agent_leases(connection, now)
                row = connection.execute(
                    """SELECT review_id, status, lease_owner_token_id, lease_expires_at
                       FROM work_orders WHERE request_id = ?""",
                    (request_id,),
                ).fetchone()
                if row is None:
                    connection.commit()
                    self.send_json(404, {"error": "work order was not found"})
                    return
                if row["review_id"] not in identity["workspace_ids"]:
                    connection.commit()
                    self.send_json(403, {"error": "agent token is not allowed for this workspace"})
                    return
                if (
                    row["status"] != "processing"
                    or row["lease_owner_token_id"] != identity["token_id"]
                    or row["lease_expires_at"] is None
                    or row["lease_expires_at"] <= now
                ):
                    connection.commit()
                    self.send_json(409, {"error": "work order lease is not owned by this agent"})
                    return
                if connection.execute(
                    """UPDATE work_orders
                       SET status = 'failed', last_error_json = ?,
                           lease_owner_token_id = NULL, lease_expires_at = NULL
                       WHERE request_id = ? AND status = 'processing' AND lease_owner_token_id = ?""",
                    (canonical_json(last_error), request_id, identity["token_id"]),
                ).rowcount != 1:
                    raise FrameCueError("work order lease changed")
                connection.commit()
            except (sqlite3.Error, FrameCueError) as error:
                connection.rollback()
                self.send_json(409, {"error": str(error)})
                return
            finally:
                connection.close()
            self.send_json(200, {"request_id": request_id, "status": "failed", "last_error": last_error})

        def send_agent_retry(self, identity, request_id):
            connection = open_workspace_database(database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not self.agent_is_active(connection, identity):
                    connection.rollback()
                    self.send_json(401, {"error": "agent bearer token is invalid"})
                    return
                expire_agent_leases(connection, agent_utc_now())
                row = connection.execute(
                    """SELECT request_id, review_id, request_json, status, last_error_json,
                              operation, base_revision, base_checksum, attempt_count
                       FROM work_orders WHERE request_id = ?""",
                    (request_id,),
                ).fetchone()
                if row is None:
                    connection.commit()
                    self.send_json(404, {"error": "work order was not found"})
                    return
                if row["review_id"] not in identity["workspace_ids"]:
                    connection.commit()
                    self.send_json(403, {"error": "agent token is not allowed for this workspace"})
                    return
                try:
                    last_error = json.loads(row["last_error_json"] or "null")
                    request = json.loads(row["request_json"])
                except json.JSONDecodeError:
                    last_error = request = None
                if row["status"] != "failed" or not isinstance(last_error, dict) or last_error.get("retryable") is not True:
                    connection.commit()
                    self.send_json(409, {"error": "work order is not retryable"})
                    return
                if not isinstance(request, dict):
                    raise FrameCueError("work order request is invalid")
                next_request_id = opaque_id("req")
                request["request_id"] = next_request_id
                if connection.execute(
                    """UPDATE work_orders
                       SET request_id = ?, request_json = ?, status = 'pending', candidate_json = NULL,
                           last_error_json = NULL, lease_owner_token_id = NULL, lease_expires_at = NULL
                       WHERE request_id = ? AND status = 'failed'""",
                    (next_request_id, canonical_json(request), request_id),
                ).rowcount != 1:
                    raise FrameCueError("work order status changed")
                connection.commit()
            except (sqlite3.Error, FrameCueError) as error:
                connection.rollback()
                self.send_json(409, {"error": str(error)})
                return
            finally:
                connection.close()
            self.send_json(200, {
                "request_id": next_request_id,
                "workspace_id": row["review_id"],
                "operation": row["operation"],
                "base_revision": row["base_revision"],
                "base_checksum": row["base_checksum"],
                "status": "pending",
                "lease_owner_token_id": None,
                "lease_expires_at": None,
                "attempt_count": row["attempt_count"],
            })

        def require_session(self):
            session = collaboration.session(self.request_session_id())
            if session is None:
                self.send_json(403, {"error": "workspace session is not registered"})
            return session

        def current_snapshot(self, session):
            snapshot = workspace_snapshot(database, review_id, csrf_token)
            snapshot.update(collaboration.snapshot_fields(session["session_id"]))
            return snapshot

        def has_workspace_draft(self):
            connection = open_workspace_database(database)
            try:
                return connection.execute(
                    "SELECT 1 FROM workspace_drafts WHERE review_id = ?",
                    (review_id,),
                ).fetchone() is not None
            finally:
                connection.close()

        def required_draft_version(self, operation, snapshot):
            draft_version = operation.get("draft_version")
            if type(draft_version) is not int or draft_version < 0:
                raise FrameCueError("workspace draft version is invalid")
            if draft_version != snapshot["draft_version"]:
                raise FrameCueError(
                    f"workspace draft version is stale: expected {snapshot['draft_version']}, got {draft_version}"
                )

        def operation_cue_ids(self, operation):
            kind = operation.get("kind")
            if kind in {"edit", "split"}:
                values = [operation.get("cue_id")]
            elif kind == "merge":
                values = [operation.get("cue_id"), operation.get("adjacent_cue_id")]
            elif kind == "flag":
                values = operation.get("cue_ids")
                if values is None:
                    values = [operation.get("cue_id")]
            else:
                values = []
            return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []

        def requested_cue_ids(self, operation, document):
            cue_ids = operation.get("cue_ids")
            if (
                not isinstance(cue_ids, list)
                or not cue_ids
                or not all(isinstance(cue_id, str) and cue_id for cue_id in cue_ids)
                or len(set(cue_ids)) != len(cue_ids)
            ):
                raise FrameCueError("workspace Cue IDs are invalid")
            known_cue_ids = {cue["id"] for cue in document["cues"]}
            if not set(cue_ids).issubset(known_cue_ids):
                raise FrameCueError("workspace Cue was not found")
            return cue_ids

        def apply_workspace_operation(self, session, operation):
            snapshot = self.current_snapshot(session)
            kind = operation.get("kind")
            if kind == "presence":
                self.set_presence(session, operation, snapshot)
                return self.current_snapshot(session)
            if kind == "dirty":
                collaboration.set_dirty(session, operation.get("dirty"))
                return self.current_snapshot(session)
            if kind in {"lock", "unlock"}:
                self.required_draft_version(operation, snapshot)
                cue_ids = self.requested_cue_ids(operation, snapshot["document"])
                if kind == "lock":
                    collaboration.lock(session, cue_ids)
                else:
                    collaboration.unlock(session, cue_ids)
                return self.current_snapshot(session)
            if kind == "lead":
                self.required_draft_version(operation, snapshot)
                expected_lead_session_id = operation.get("expected_lead_session_id")
                new_lead_session_id = operation.get("new_lead_session_id", "")
                if not isinstance(expected_lead_session_id, str) or not isinstance(new_lead_session_id, str):
                    raise FrameCueError("workspace lead operation is invalid")
                collaboration.transfer_lead(session, expected_lead_session_id, new_lead_session_id)
                return self.current_snapshot(session)
            affected_cue_ids = self.operation_cue_ids(operation)
            collaboration.assert_unlocked(session, affected_cue_ids)
            apply_draft_operation(database, review_id, operation)
            collaboration.unlock(session, affected_cue_ids)
            collaboration.changed()
            return self.current_snapshot(session)

        def set_presence(self, session, operation, snapshot):
            cue_ids = {cue["id"] for cue in snapshot["document"]["cues"]}
            collaboration.set_presence(session, operation, cue_ids)

        def send_workspace_event(self):
            with collaboration.condition:
                session = self.require_session()
                if session is None:
                    return
                try:
                    last_version = int(self.headers.get("Last-Event-ID", "-1"))
                except ValueError:
                    last_version = -1
                collaboration.expire()
                if collaboration.version <= last_version:
                    collaboration.condition.wait(timeout=15)
                    collaboration.expire()
                version = collaboration.version
            if version <= last_version:
                body = b": keepalive\n\n"
            else:
                body = (
                    f"retry: 1000\nid: {version}\nevent: snapshot\n"
                    f"data: {json.dumps({'version': version})}\n\n"
                ).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

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
            parsed_request = urlsplit(self.path)
            path = parsed_request.path
            if path == "/api/agent/work-orders":
                identity = self.require_agent("list")
                if identity is None:
                    return
                workspace_ids = parse_qs(parsed_request.query).get("workspace_id", [])
                if len(workspace_ids) != 1 or not workspace_ids[0]:
                    self.send_json(400, {"error": "workspace_id is required"})
                    return
                self.send_agent_list(identity, workspace_ids[0])
                return
            agent_read = re.fullmatch(r"/api/agent/work-orders/([^/]+)", path)
            if agent_read:
                identity = self.require_agent("read")
                if identity is not None:
                    self.send_agent_read(identity, unquote(agent_read.group(1)))
                return
            if path == "/api/workspace/events":
                self.send_workspace_event()
                return
            if path == "/api/workspace/snapshot":
                with collaboration.condition:
                    try:
                        session = collaboration.register_or_refresh(
                            self.request_session_id(),
                            self.headers.get("X-FrameCue-Display-Name", ""),
                        )
                        snapshot = self.current_snapshot(session)
                    except FrameCueError as error:
                        self.send_json(409, {"error": str(error)})
                        return
                self.send_json(
                    200,
                    snapshot,
                    (("Set-Cookie", f"framecue_session={session['session_id']}; Path=/; HttpOnly; SameSite=Strict"),),
                )
                return
            if path != "/api/workspace":
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
            path = urlsplit(self.path).path
            agent_claim = re.fullmatch(r"/api/agent/work-orders/([^/]+)/claim", path)
            if agent_claim:
                identity = self.require_agent("claim")
                if identity is not None:
                    self.send_agent_claim(identity, unquote(agent_claim.group(1)))
                return
            agent_submit = re.fullmatch(r"/api/agent/work-orders/([^/]+)/submit", path)
            if agent_submit:
                identity = self.require_agent("submit")
                if identity is None:
                    return
                value = self.read_workspace_json()
                if value is not None:
                    self.send_agent_submit(identity, unquote(agent_submit.group(1)), value)
                return
            agent_fail = re.fullmatch(r"/api/agent/work-orders/([^/]+)/fail", path)
            if agent_fail:
                identity = self.require_agent("fail")
                if identity is None:
                    return
                value = self.read_workspace_json()
                if value is not None:
                    self.send_agent_fail(identity, unquote(agent_fail.group(1)), value)
                return
            agent_retry = re.fullmatch(r"/api/agent/work-orders/([^/]+)/retry", path)
            if agent_retry:
                identity = self.require_agent("retry")
                if identity is None:
                    return
                value = self.read_workspace_json()
                if value is not None:
                    self.send_agent_retry(identity, unquote(agent_retry.group(1)))
                return
            if path not in {"/api/content-complete", "/api/workspace/operation", "/api/workspace/complete"}:
                self.send_error(404)
                return
            value = self.read_workspace_json()
            if value is None:
                return
            if not self.has_valid_csrf():
                self.send_error(403)
                return
            if path == "/api/content-complete":
                if self.has_workspace_draft():
                    self.send_json(409, {"error": "active Subtitle Workspace must use /api/workspace/complete"})
                    return
                try:
                    summary = complete_content_revision(database, review_id, value)
                except FrameCueError as error:
                    self.send_json(409, {"error": str(error)})
                    return
                self.send_json(200, summary)
                return
            with collaboration.condition:
                session = self.require_session()
                if session is None:
                    return
                try:
                    if path == "/api/workspace/operation":
                        self.send_json(200, self.apply_workspace_operation(session, value))
                        return
                    if collaboration.lead_session_id != session["session_id"]:
                        self.send_json(403, {"error": "only the workspace lead can complete this round"})
                        return
                    completion_error = collaboration.completion_error()
                    if completion_error:
                        self.send_json(409, {"error": completion_error})
                        return
                    summary = complete_workspace_round(database, review_id, value.get("draft_version"))
                    collaboration.changed()
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


def command_agent_token_create(args):
    label = args.label.strip()
    workspace_ids = list(dict.fromkeys(value.strip() for value in args.workspace if value.strip()))
    permissions = list(dict.fromkeys(args.permission))
    if not label or any(not value.strip() for value in args.workspace):
        raise FrameCueError("agent token label and Workspace IDs must be non-empty")
    token_id = opaque_id("token")
    token = secrets.token_urlsafe(32)
    connection = open_workspace_database(args.database)
    try:
        connection.execute(
            """INSERT INTO agent_tokens
               (token_id, token_hash, label, workspace_ids_json, permissions_json, revoked_at, created_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?)""",
            (
                token_id,
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                label,
                canonical_json(workspace_ids),
                canonical_json(permissions),
                utc_now(),
            ),
        )
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise FrameCueError(f"workspace database error: {error}") from error
    finally:
        connection.close()
    print(json.dumps({
        "token_id": token_id,
        "token": token,
        "label": label,
        "workspace_ids": workspace_ids,
        "permissions": permissions,
    }, ensure_ascii=False))


def command_agent_token_revoke(args):
    connection = open_workspace_database(args.database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT revoked_at FROM agent_tokens WHERE token_id = ?", (args.token_id,)
        ).fetchone()
        if row is None:
            raise FrameCueError(f"agent token not found: {args.token_id}")
        revoked_at = row["revoked_at"] or utc_now()
        connection.execute(
            "UPDATE agent_tokens SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL",
            (revoked_at, args.token_id),
        )
        connection.execute(
            """UPDATE work_orders
               SET status = 'pending', lease_owner_token_id = NULL, lease_expires_at = NULL
               WHERE status = 'processing' AND lease_owner_token_id = ?""",
            (args.token_id,),
        )
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise FrameCueError(f"workspace database error: {error}") from error
    finally:
        connection.close()
    print(json.dumps({"token_id": args.token_id, "status": "revoked", "revoked_at": revoked_at}))


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


def validate_content_candidate_v2(candidate, order, base_document):
    try:
        request = json.loads(order["request_json"])
    except json.JSONDecodeError as error:
        raise FrameCueError(f"work order request is invalid JSON: {order['request_id']}") from error
    bindings = {
        "request_id": order["request_id"],
        "workspace_id": order["review_id"],
        "operation": order["operation"],
        "base_revision": order["base_revision"],
        "base_draft_version": request.get("base_draft_version"),
        "base_checksum": order["base_checksum"],
    }
    if request.get("schema") != WORK_ORDER_V2_SCHEMA or order["operation"] != "content_correction_review":
        raise FrameCueError("candidate operation is not supported")
    for key, value in bindings.items():
        if candidate.get(key) != value:
            raise FrameCueError(f"candidate {key} does not match work order")
    if candidate.get("status") != "ready_for_review":
        raise FrameCueError("candidate status is not ready_for_review")

    document = candidate.get("document")
    if not isinstance(document, dict) or document.get("schema") != SUBTITLE_DOCUMENT_V2_SCHEMA:
        raise FrameCueError("candidate document schema is invalid")
    if document.get("workspace_id") != order["review_id"]:
        raise FrameCueError("candidate document workspace_id does not match work order")
    if document.get("checksum") != document_checksum(document):
        raise FrameCueError("candidate document checksum does not match content")
    base_cues = base_document.get("cues")
    base_blocks = base_document.get("blocks")
    cues = document.get("cues")
    blocks = document.get("blocks")
    if not all(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
               for rows in (base_cues, base_blocks, cues, blocks)):
        raise FrameCueError("candidate document cues and blocks must be arrays of objects")
    base_cue_by_id = {row.get("id"): row for row in base_cues}
    cue_by_id = {row.get("id"): row for row in cues}
    base_block_by_id = {row.get("id"): row for row in base_blocks}
    block_by_id = {row.get("id"): row for row in blocks}
    if None in base_cue_by_id or None in cue_by_id or len(cue_by_id) != len(cues):
        raise FrameCueError("candidate cue IDs are invalid")
    if list(block_by_id) != list(base_block_by_id) or len(block_by_id) != len(blocks):
        raise FrameCueError("candidate block IDs do not match base document")

    targets = request.get("targets")
    proposals = candidate.get("change_proposals")
    if not isinstance(targets, list) or not isinstance(proposals, list) or not proposals:
        raise FrameCueError("candidate change_proposals are required")
    target_by_range = {row.get("range_id"): row for row in targets if isinstance(row, dict)}
    if None in target_by_range or len(target_by_range) != len(targets):
        raise FrameCueError("work order targets are invalid")
    proposal_ids = [row.get("proposal_id") for row in proposals if isinstance(row, dict)]
    range_ids = [row.get("range_id") for row in proposals if isinstance(row, dict)]
    if len(proposal_ids) != len(proposals) or any(not isinstance(value, str) or not value for value in proposal_ids):
        raise FrameCueError("candidate proposal_id is invalid")
    if len(set(proposal_ids)) != len(proposal_ids) or len(set(range_ids)) != len(range_ids):
        raise FrameCueError("candidate proposals must have unique IDs and ranges")

    authorized_cues = set()
    authorized_blocks = set()
    cue_owner = {}
    structural_operations = set()
    replacement_cues = {}
    replacement_blocks = {}
    for proposal in proposals:
        target = target_by_range.get(proposal.get("range_id"))
        if target is None:
            raise FrameCueError("candidate proposal range is not authorized")
        if proposal.get("before_checksum") != target.get("before_checksum"):
            raise FrameCueError("candidate proposal before_checksum is stale")
        base_slice = {
            "cues": [base_cue_by_id.get(cue_id) for cue_id in target.get("cue_ids", [])],
            "blocks": [base_block_by_id.get(block_id) for block_id in target.get("block_ids", [])],
        }
        if None in base_slice["cues"] or None in base_slice["blocks"]:
            raise FrameCueError("work order target references missing document rows")
        if hashlib.sha256(canonical_json(base_slice).encode("utf-8")).hexdigest() != target.get("before_checksum"):
            raise FrameCueError("work order target before_checksum does not match base document")
        replacement = proposal.get("replacement")
        if not isinstance(replacement, dict) or not all(
            isinstance(replacement.get(key), list) and all(isinstance(row, dict) for row in replacement[key])
            for key in ("cues", "blocks")
        ):
            raise FrameCueError("candidate proposal replacement is invalid")
        target_cue_ids = set(target["cue_ids"])
        replacement_slice = [
            cue for cue in cues
            if cue.get("id") in target_cue_ids
            or (
                cue.get("id") not in base_cue_by_id
                and isinstance(cue.get("origin_cue_ids"), list)
                and bool(set(cue["origin_cue_ids"]) & target_cue_ids)
            )
        ]
        if replacement["cues"] != replacement_slice:
            raise FrameCueError("candidate replacement Cues do not match their target range")
        if replacement["blocks"] != [block_by_id[block_id] for block_id in target["block_ids"]]:
            raise FrameCueError("candidate replacement Blocks do not match their target range")
        structural_slice = [cue for cue in replacement_slice if cue.get("id") not in base_cue_by_id]
        for cue in structural_slice:
            lineage = cue.get("lineage")
            if (
                not isinstance(lineage, dict)
                or lineage.get("operation") not in target.get("allowed_operations", [])
                or not set(cue.get("origin_cue_ids", [])).issubset(target_cue_ids)
                or not set(lineage.get("parent_cue_ids", [])).issubset(target_cue_ids)
            ):
                raise FrameCueError(f"candidate Cue lineage is not authorized for range: {cue.get('id')}")
            parents = [base_cue_by_id[parent_id] for parent_id in lineage["parent_cue_ids"]]
            if lineage["operation"] == "split":
                if len(parents) != 1:
                    raise FrameCueError(f"candidate split lineage is invalid: {cue.get('id')}")
                parent = parents[0]
                derived = {
                    "id", "source_start_ms", "source_end_ms", "output_start_ms", "output_end_ms",
                    "timing_state", "source_text", "display_text", "speech_text", "origin_cue_ids", "lineage",
                }
                if (
                    any(cue.get(key) != parent.get(key) for key in set(cue) | set(parent) if key not in derived)
                    or cue.get("output_start_ms") is not None
                    or cue.get("output_end_ms") is not None
                    or cue.get("timing_state") != "provisional"
                    or cue.get("origin_cue_ids") != parent.get("origin_cue_ids", [parent["id"]])
                    or cue.get("block_id") != parent.get("block_id")
                ):
                    raise FrameCueError(f"candidate split changes immutable Cue fields: {cue.get('id')}")
            else:
                if len(parents) != 2:
                    raise FrameCueError(f"candidate merge lineage is invalid: {cue.get('id')}")
                positions = [base_cues.index(parent) for parent in parents]
                if (
                    abs(positions[0] - positions[1]) != 1
                    or parents[0].get("block_id") != parents[1].get("block_id")
                    or any(parent["id"] in cue_by_id for parent in parents)
                ):
                    raise FrameCueError(f"candidate merge parents are not adjacent in one Block: {cue.get('id')}")
                left, right = sorted(parents, key=base_cues.index)
                origins = list(dict.fromkeys(left.get("origin_cue_ids", [left["id"]]) + right.get("origin_cue_ids", [right["id"]])))
                output_start, output_end = left.get("output_start_ms"), right.get("output_end_ms")
                if type(output_start) is not int or type(output_end) is not int:
                    output_start = output_end = None
                expected = {
                    "source_start_ms": min(left["source_start_ms"], right["source_start_ms"]),
                    "source_end_ms": max(left["source_end_ms"], right["source_end_ms"]),
                    "output_start_ms": output_start,
                    "output_end_ms": output_end,
                    "timing_state": "provisional" if "provisional" in {left.get("timing_state"), right.get("timing_state")} else "unrealized",
                    "source_text": " ".join(value for value in (left.get("source_text", ""), right.get("source_text", "")) if value),
                    "speech_linked": bool(left.get("speech_linked")) and bool(right.get("speech_linked")),
                    "block_id": left.get("block_id"),
                    "origin_cue_ids": origins,
                    "lineage": {"operation": "merge", "parent_cue_ids": [left["id"], right["id"]]},
                }
                if set(cue) != set(expected) | {"id", "display_text", "speech_text"} or any(cue.get(key) != value for key, value in expected.items()):
                    raise FrameCueError(f"candidate merge changes immutable Cue fields: {cue.get('id')}")
        split_parents = {cue["lineage"]["parent_cue_ids"][0] for cue in structural_slice if cue["lineage"]["operation"] == "split"}
        for parent_id in split_parents:
            children = [cue for cue in replacement_slice if cue.get("lineage") == {"operation": "split", "parent_cue_ids": [parent_id]}]
            parent = base_cue_by_id[parent_id]
            if (
                len(children) != 2
                or parent_id in cue_by_id
                or children[0].get("source_start_ms") != parent.get("source_start_ms")
                or children[0].get("source_end_ms") != children[1].get("source_start_ms")
                or children[1].get("source_end_ms") != parent.get("source_end_ms")
                or "".join("".join(child.get("source_text", "").split()) for child in children)
                != "".join(parent.get("source_text", "").split())
            ):
                raise FrameCueError(f"candidate split does not exactly partition its parent: {parent_id}")
        if structural_slice:
            base_text = "".join("".join(cue.get("source_text", "").split()) for cue in base_slice["cues"])
            replacement_text = "".join("".join(cue.get("source_text", "").split()) for cue in replacement_slice)
            if (
                replacement_text != base_text
                or replacement_slice[0].get("source_start_ms") != base_slice["cues"][0].get("source_start_ms")
                or replacement_slice[-1].get("source_end_ms") != base_slice["cues"][-1].get("source_end_ms")
            ):
                raise FrameCueError("candidate structural replacement changes immutable source content")
        authorized_cues.update(target["cue_ids"])
        authorized_blocks.update(target["block_ids"])
        for cue_id in target["cue_ids"]:
            if cue_id in cue_owner:
                raise FrameCueError("work order target Cue ranges overlap")
            cue_owner[cue_id] = target["range_id"]
        structural_operations.update(set(target.get("allowed_operations", [])) & {"split", "merge"})
        for row in replacement["cues"]:
            if row.get("id") in replacement_cues:
                raise FrameCueError("candidate replacement Cue appears in multiple proposals")
            replacement_cues[row.get("id")] = row
        for row in replacement["blocks"]:
            if row.get("id") in replacement_blocks:
                raise FrameCueError("candidate replacement Block appears in multiple proposals")
            replacement_blocks[row.get("id")] = row

    for key in set(base_document) | set(document):
        if key not in {"checksum", "cues", "blocks"} and document.get(key) != base_document.get(key):
            raise FrameCueError(f"candidate document changes immutable field: {key}")
    for cue_id, base_cue in base_cue_by_id.items():
        if cue_id not in authorized_cues and cue_by_id.get(cue_id) != base_cue:
            raise FrameCueError(f"candidate changes Cue outside an authorized range: {cue_id}")

    candidate_slice_cues = []
    covered_origins = set()
    for cue in cues:
        cue_id = cue["id"]
        if cue_id in authorized_cues:
            expected = copy.deepcopy(base_cue_by_id[cue_id])
            expected["display_text"] = cue.get("display_text")
            expected["speech_text"] = cue.get("speech_text")
            if cue != expected:
                raise FrameCueError(f"candidate changes unauthorized Cue fields: {cue_id}")
            covered_origins.add(cue_id)
            candidate_slice_cues.append(cue)
        elif cue_id not in base_cue_by_id:
            origins = cue.get("origin_cue_ids")
            lineage = cue.get("lineage")
            operation = lineage.get("operation") if isinstance(lineage, dict) else None
            parents = lineage.get("parent_cue_ids") if isinstance(lineage, dict) else None
            if (
                operation not in structural_operations
                or not isinstance(origins, list) or not origins
                or not isinstance(parents, list) or not parents
                or not set(origins).issubset(authorized_cues)
                or not set(parents).issubset(authorized_cues)
                or cue.get("block_id") not in authorized_blocks
            ):
                raise FrameCueError(f"candidate Cue lineage is not authorized: {cue_id}")
            covered_origins.update(set(origins) & authorized_cues)
            candidate_slice_cues.append(cue)
        elif cue_id not in authorized_cues and cue != base_cue_by_id[cue_id]:
            raise FrameCueError(f"candidate changes Cue outside an authorized range: {cue_id}")
    if covered_origins != authorized_cues:
        raise FrameCueError("candidate structural replacement does not preserve target lineage")

    structural = any(cue["id"] not in base_cue_by_id for cue in candidate_slice_cues)
    if not structural and [cue["id"] for cue in cues] != [cue["id"] for cue in base_cues]:
        raise FrameCueError("candidate Cue order does not match base document")
    if structural:
        for range_id in range_ids:
            indexes = [index for index, cue in enumerate(base_cues) if cue["id"] in target_by_range[range_id]["cue_ids"]]
            if indexes != list(range(min(indexes), max(indexes) + 1)):
                raise FrameCueError(f"work order target Cue range is not contiguous: {range_id}")
        base_order = [cue_owner.get(cue["id"], cue["id"]) for cue in base_cues]
        candidate_order = []
        for cue in cues:
            owners = {cue_owner[value] for value in cue.get("origin_cue_ids", []) if value in cue_owner}
            owner = cue_owner.get(cue["id"])
            if owner:
                owners.add(owner)
            if len(owners) > 1:
                raise FrameCueError(f"candidate Cue crosses authorized ranges: {cue['id']}")
            candidate_order.append(next(iter(owners), cue["id"]))
        compact = lambda values: [value for index, value in enumerate(values) if index == 0 or value != values[index - 1]]
        if compact(candidate_order) != compact(base_order):
            raise FrameCueError("candidate structural replacement moves an authorized Cue range")
    for block_id, base_block in base_block_by_id.items():
        block = block_by_id[block_id]
        if block_id not in authorized_blocks:
            if block != base_block:
                raise FrameCueError(f"candidate changes Block outside an authorized range: {block_id}")
            continue
        expected = copy.deepcopy(base_block)
        expected["target_text"] = block.get("target_text")
        expected["speech_text"] = block.get("speech_text")
        if structural:
            expected["cue_ids"] = block.get("cue_ids")
        if block != expected:
            raise FrameCueError(f"candidate changes unauthorized Block fields: {block_id}")
        block_cues = [cue_by_id.get(cue_id) for cue_id in block.get("cue_ids", [])]
        if None in block_cues:
            raise FrameCueError(f"candidate Block references an unknown Cue: {block_id}")
        if block.get("target_text") != " ".join(cue.get("display_text", "") for cue in block_cues):
            raise FrameCueError(f"candidate Block target_text is inconsistent: {block_id}")
        if block.get("speech_text") != " ".join(cue.get("speech_text", "") for cue in block_cues):
            raise FrameCueError(f"candidate Block speech_text is inconsistent: {block_id}")

    candidate_slice_blocks = [block for block in blocks if block["id"] in authorized_blocks]
    if replacement_cues != {row["id"]: row for row in candidate_slice_cues}:
        raise FrameCueError("candidate replacement Cues do not match the full document")
    if replacement_blocks != {row["id"]: row for row in candidate_slice_blocks}:
        raise FrameCueError("candidate replacement Blocks do not match the full document")


def store_content_candidate_v2(connection, order, candidate, expected_status, lease_owner_token_id=None):
    if candidate.get("schema") != CANDIDATE_REVISION_V2_SCHEMA:
        raise FrameCueError("candidate schema is invalid")
    try:
        base_document = json.loads(order["document_json"])
    except json.JSONDecodeError as error:
        raise FrameCueError(f"work order base document is invalid JSON: {order['request_id']}") from error
    if not isinstance(base_document, dict):
        raise FrameCueError(f"work order base document is invalid: {order['request_id']}")
    if (
        base_document.get("checksum") != order["base_checksum"]
        or document_checksum(base_document) != order["base_checksum"]
    ):
        raise FrameCueError(f"work order base checksum does not match storage: {order['request_id']}")
    validate_content_candidate_v2(candidate, order, base_document)
    conditions = "work_order_id = ? AND status = ?"
    values = [order["work_order_id"], expected_status]
    if lease_owner_token_id is not None:
        conditions += " AND lease_owner_token_id = ?"
        values.append(lease_owner_token_id)
    if connection.execute(
        f"""UPDATE work_orders
               SET status = 'candidate_ready', candidate_json = ?,
                   lease_owner_token_id = NULL, lease_expires_at = NULL
               WHERE {conditions}""",
        [canonical_json(candidate), *values],
    ).rowcount != 1:
        raise FrameCueError(f"work order is not {expected_status}: {order['request_id']}")
    if connection.execute(
        "UPDATE workspaces SET stage = 'content_candidate_review' WHERE review_id = ?",
        (order["review_id"],),
    ).rowcount != 1:
        raise FrameCueError(f"workspace not found: {order['review_id']}")


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
                          work_orders.base_revision, work_orders.base_checksum, work_orders.operation,
                          work_orders.request_json, revisions.document_json
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

            if candidate.get("schema") not in {CANDIDATE_REVISION_SCHEMA, CANDIDATE_REVISION_V2_SCHEMA}:
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

            if candidate.get("schema") == CANDIDATE_REVISION_V2_SCHEMA:
                store_content_candidate_v2(connection, order, candidate, "pending")
                connection.commit()
                print(json.dumps({
                    "stage": "content_candidate_review",
                    "status": "candidate_ready",
                    "request_id": request_id,
                }, ensure_ascii=False))
                return

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


def candidate_checksum(candidate):
    return hashlib.sha256(canonical_json(candidate).encode("utf-8")).hexdigest()


def apply_candidate_proposals(base_document, proposals):
    document = copy.deepcopy(base_document)
    for proposal in proposals:
        replacement = proposal["replacement"]
        origins = {
            cue_id
            for cue in replacement["cues"]
            for cue_id in cue.get("origin_cue_ids", [cue.get("id")])
        }
        indexes = [index for index, cue in enumerate(document["cues"]) if cue.get("id") in origins]
        if not indexes or indexes != list(range(min(indexes), max(indexes) + 1)):
            raise FrameCueError(f"candidate proposal base range is invalid: {proposal['proposal_id']}")
        document["cues"][min(indexes):max(indexes) + 1] = copy.deepcopy(replacement["cues"])
        blocks = {block["id"]: copy.deepcopy(block) for block in replacement["blocks"]}
        document["blocks"] = [blocks.get(block["id"], block) for block in document["blocks"]]
    recompute_draft_blocks(document)
    return document


def decide_content_candidate(database, review_id, request_id, supplied_checksum, decisions):
    if not isinstance(decisions, list) or not decisions:
        raise FrameCueError("candidate decisions must be a non-empty array")
    connection = open_workspace_database(database)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            workspace = workspace_row(connection, review_id)
            if workspace["stage"] != "content_candidate_review":
                raise FrameCueError(f"workspace is not awaiting candidate decisions: {review_id}")
            order = connection.execute(
                """SELECT work_orders.*, revisions.document_json
                   FROM work_orders JOIN revisions ON revisions.revision_id = work_orders.revision_id
                   WHERE work_orders.review_id = ? AND work_orders.request_id = ?
                     AND work_orders.status = 'candidate_ready'""",
                (review_id, request_id),
            ).fetchone()
            if order is None:
                raise FrameCueError(f"content candidate is not ready: {request_id}")
            candidate = json.loads(order["candidate_json"])
            if candidate_checksum(candidate) != supplied_checksum:
                raise FrameCueError("content candidate checksum is stale")
            proposals = candidate.get("change_proposals")
            proposal_ids = [proposal.get("proposal_id") for proposal in proposals]
            decision_ids = [decision.get("proposal_id") for decision in decisions if isinstance(decision, dict)]
            if (
                len(decision_ids) != len(decisions)
                or len(set(decision_ids)) != len(decision_ids)
                or set(decision_ids) != set(proposal_ids)
                or any(decision.get("decision") not in {"accept", "reject"} for decision in decisions)
            ):
                raise FrameCueError("candidate decisions must cover every proposal exactly once")
            decision_by_id = {decision["proposal_id"]: decision["decision"] for decision in decisions}
            dependency_decisions = {}
            for proposal in proposals:
                dependencies = proposal.get("dependencies", [])
                if (
                    not isinstance(dependencies, list)
                    or any(not isinstance(value, str) or not value for value in dependencies)
                    or len(set(dependencies)) != len(dependencies)
                ):
                    raise FrameCueError("candidate proposal dependencies must be unique non-empty strings")
                decision = decision_by_id[proposal["proposal_id"]]
                for dependency in dependencies:
                    prior = dependency_decisions.setdefault(dependency, decision)
                    if prior != decision:
                        raise FrameCueError(f"candidate dependency group has conflicting decisions: {dependency}")
            base_document = json.loads(order["document_json"])
            accepted = [proposal for proposal in proposals if decision_by_id[proposal["proposal_id"]] == "accept"]
            rejected = [proposal for proposal in proposals if decision_by_id[proposal["proposal_id"]] == "reject"]
            document = apply_candidate_proposals(base_document, accepted)
            request = json.loads(order["request_json"])
            target_by_range = {target["range_id"]: target for target in request["targets"]}
            if rejected:
                document["revision_kind"] = "draft"
                refresh_document_checksum(document)
                issues = []
                for proposal in rejected:
                    target = target_by_range[proposal["range_id"]]
                    context = target.get("context", {})
                    categories = context.get("categories") or ["candidate_rejected"]
                    for category in categories:
                        issues.append({
                            "flag_id": opaque_id("flag"),
                            "range_id": target["range_id"],
                            "cue_ids": target["cue_ids"],
                            "category": category,
                            "authors": ["candidate-review"],
                            "notes": list(context.get("notes", [])),
                        })
                draft = connection.execute(
                    "SELECT draft_version FROM workspace_drafts WHERE review_id = ?",
                    (review_id,),
                ).fetchone()
                if draft is None:
                    raise FrameCueError(f"workspace draft not found: {review_id}")
                next_version = draft["draft_version"] + 1
                created_at = utc_now()
                revision_id = insert_workspace_revision(
                    connection, review_id, document["revision"], "draft_snapshot",
                    order["revision_id"], document, created_at,
                )
                candidate["status"] = "changes_requested"
                candidate["decisions"] = decisions
                connection.execute(
                    "UPDATE work_orders SET status = 'changes_requested', candidate_json = ? WHERE work_order_id = ?",
                    (canonical_json(candidate), order["work_order_id"]),
                )
                if connection.execute(
                    """UPDATE workspace_drafts
                       SET draft_version = ?, document_json = ?, issues_json = ?, direct_changes_json = '[]', frozen_revision_id = NULL
                       WHERE review_id = ? AND draft_version = ?""",
                    (next_version, canonical_json(document), canonical_json(issues), review_id, draft["draft_version"]),
                ).rowcount != 1:
                    raise FrameCueError(f"workspace draft version is stale: {review_id}")
                connection.execute(
                    "UPDATE workspaces SET stage = 'content_review', current_revision_id = ? WHERE review_id = ?",
                    (revision_id, review_id),
                )
                connection.commit()
                return {
                    "workspace_id": review_id,
                    "stage": "content_review",
                    "status": "changes_requested",
                    "request_id": request_id,
                    "draft_version": next_version,
                    "checksum": document["checksum"],
                }

            document["revision_kind"] = "content"
            refresh_document_checksum(document)
            created_at = utc_now()
            revision_id = insert_workspace_revision(
                connection, review_id, document["revision"], "content",
                order["revision_id"], document, created_at,
            )
            next_order = connection.execute(
                "SELECT COALESCE(MAX(work_order_id), 0) + 1 AS value FROM work_orders"
            ).fetchone()["value"]
            next_request_id = f"req-{next_order:04d}"
            target = workspace_work_order_target(
                document, [cue["id"] for cue in document["cues"]], opaque_id("range"),
                ["realize_voice_timeline"], ["output_start_ms", "output_end_ms", "assets"],
            )
            work_order = {
                "schema": WORK_ORDER_V2_SCHEMA,
                "request_id": next_request_id,
                "workspace_id": review_id,
                "operation": "realize_voice_timeline",
                "base_revision": document["revision"],
                "base_draft_version": request["base_draft_version"],
                "base_checksum": document["checksum"],
                "timing_profile": document["timing_profile"],
                "targets": [target],
                "required_outputs": ["document", "block_audio", "word_alignment", "timing_audit"],
                "document": document,
            }
            candidate["status"] = "accepted"
            candidate["decisions"] = decisions
            connection.execute(
                "UPDATE work_orders SET status = 'accepted', candidate_json = ? WHERE work_order_id = ?",
                (canonical_json(candidate), order["work_order_id"]),
            )
            connection.execute(
                """INSERT INTO work_orders
                   (request_id, review_id, revision_id, base_revision, base_checksum, operation, status, request_json, candidate_json, created_at)
                   VALUES (?, ?, ?, ?, ?, 'realize_voice_timeline', 'pending', ?, NULL, ?)""",
                (
                    next_request_id, review_id, revision_id, document["revision"], document["checksum"],
                    canonical_json(work_order), created_at,
                ),
            )
            connection.execute(
                """UPDATE workspace_drafts
                   SET document_json = ?, issues_json = '[]', direct_changes_json = '[]', frozen_revision_id = ?
                   WHERE review_id = ?""",
                (canonical_json(document), revision_id, review_id),
            )
            connection.execute(
                "UPDATE workspaces SET stage = 'voice_realization_pending', current_revision_id = ? WHERE review_id = ?",
                (revision_id, review_id),
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
        "status": "accepted",
        "request_id": request_id,
        "next_request_id": next_request_id,
        "checksum": document["checksum"],
    }


def command_candidate_decide(args):
    decisions = read_json(args.decisions)
    print(json.dumps(decide_content_candidate(
        args.database, args.review_id, args.request_id, args.candidate_checksum, decisions,
    ), ensure_ascii=False))


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

    agent_token_create = commands.add_parser("agent-token-create", help="create one scoped agent bearer token")
    agent_token_create.add_argument("--database", required=True)
    agent_token_create.add_argument("--label", required=True)
    agent_token_create.add_argument("--workspace", action="append", required=True)
    agent_token_create.add_argument(
        "--permission", action="append", required=True,
        choices=("list", "claim", "read", "submit", "fail", "retry"),
    )
    agent_token_create.set_defaults(func=command_agent_token_create)

    agent_token_revoke = commands.add_parser("agent-token-revoke", help="revoke one agent bearer token")
    agent_token_revoke.add_argument("--database", required=True)
    agent_token_revoke.add_argument("--token-id", required=True)
    agent_token_revoke.set_defaults(func=command_agent_token_revoke)

    work_pull = commands.add_parser("work-pull", help="write the workspace's pending work order")
    work_pull.add_argument("--database", required=True)
    work_pull.add_argument("--review-id", required=True)
    work_pull.add_argument("--out", required=True)
    work_pull.set_defaults(func=command_work_pull)

    work_submit = commands.add_parser("work-submit", help="save a voice-aligned candidate revision for audiovisual review")
    work_submit.add_argument("--database", required=True)
    work_submit.add_argument("--candidate", required=True)
    work_submit.set_defaults(func=command_work_submit)

    candidate_decide = commands.add_parser("candidate-decide", help="accept or reject every proposal in a content Candidate")
    candidate_decide.add_argument("--database", required=True)
    candidate_decide.add_argument("--review-id", required=True)
    candidate_decide.add_argument("--request-id", required=True)
    candidate_decide.add_argument("--candidate-checksum", required=True)
    candidate_decide.add_argument("--decisions", required=True)
    candidate_decide.set_defaults(func=command_candidate_decide)

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
