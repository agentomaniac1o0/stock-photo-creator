#!/usr/bin/env python3
"""
select_pipeline.py — Selection Phase for Stock Photo Creator

Phase 1 pipeline: groups RAWs by AEB, classifies each group via Gemini,
generates .pp3 profiles with scene-specific settings, and sorts into
selected/rejected on Nextcloud. NO JPEGs are created in this phase.

Usage:
  .venv/bin/python3 select_pipeline.py SW-England-May26-01
  .venv/bin/python3 select_pipeline.py SW-England-May26-01 --dry-run
  .venv/bin/python3 select_pipeline.py SW-England-May26-01 --keep-temp
"""

import argparse
import gc
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from io import BytesIO

import numpy as np
import requests
from PIL import Image

requests.packages.urllib3.disable_warnings()

HAS_RAWPY = False
try:
    import rawpy
    HAS_RAWPY = True
except ImportError:
    pass

HAS_CV2 = False
try:
    import cv2
    HAS_CV2 = True
except ImportError:
    pass

HAS_OPENAI = False
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from photo_pipeline import (
    NextcloudClient, init_nextcloud,
    group_brackets, select_best_exposure,
    analyze_raw_image, quality_check,
    classify_and_describe, get_pp3_profile,
    modify_pp3_contrast_saturation,
    read_exif_fields,
    SCENE_QUALITY_THRESHOLDS,
    SCENE_PROFILES,
    NC_BASE_PATH, LOCAL_TEMP_DIR,
    HAS_RAWPY as _,
)

NC_SELECT_PIPE = f"{NC_BASE_PATH}/select-pipe-proj"

SCENE_DEFAULTS = {
    "landscape":     {"contrast": 0,   "saturation": 10},
    "architecture":  {"contrast": 10,  "saturation": 0},
    "portrait":      {"contrast": 0,   "saturation": -5},
    "street":        {"contrast": 5,   "saturation": 0},
    "food":          {"contrast": 5,   "saturation": 15},
    "macro":         {"contrast": 10,  "saturation": 5},
    "night":         {"contrast": 15,  "saturation": 0},
    "underwater":    {"contrast": 10,  "saturation": 10},
    "fog":           {"contrast": -20, "saturation": -10},
    "default":       {"contrast": 0,   "saturation": 0},
}


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def extract_thumbnail(raw_path: Path) -> Optional[bytes]:
    try:
        with rawpy.imread(str(raw_path)) as raw:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                return thumb.data
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                img = Image.fromarray(thumb.data)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=85)
                return buf.getvalue()
    except Exception as e:
        log(f"  Thumbnail extraction failed for {raw_path.name}: {e}")
    return None


def classify_raw(raw_path: Path) -> dict:
    thumb_data = extract_thumbnail(raw_path)
    if thumb_data is None:
        log(f"  No thumbnail available, falling back to PIL open")
        try:
            img = Image.open(raw_path)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=70)
            thumb_data = buf.getvalue()
        except Exception as e:
            log(f"  Cannot open {raw_path.name}: {e}")
            return {
                "scene": "default",
                "contrast_adjustment": 0,
                "saturation_adjustment": 0,
                "short_desc": "",
                "photo_desc": "",
                "city_area": "",
                "country": "",
                "keywords": [],
            }

    import base64
    image_b64 = base64.b64encode(thumb_data).decode("utf-8")

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        try:
            import subprocess
            api_key = subprocess.check_output(
                ["secret-tool", "lookup", "service", "openai", "purpose", "stockfoto"],
                text=True
            ).strip()
        except Exception:
            pass

    if not api_key:
        log("  WARNING: No OpenAI API key. Using defaults.")
        return {
            "scene": "default",
            "contrast_adjustment": 0,
            "saturation_adjustment": 0,
            "short_desc": "",
            "photo_desc": "",
            "city_area": "",
            "country": "",
            "keywords": [],
        }

    client = OpenAI(api_key=api_key)
    vision_model = os.getenv("VISION_MODEL", "gpt-4.1-mini")

    prompt = """You generate stock photography metadata in ENGLISH.
Analyze this photo and provide:
1. Scene classification: one of: landscape, architecture, portrait, street, food, macro, night, underwater, fog
2. Contrast recommendation (-30 to +30)
3. Saturation recommendation (-30 to +30)
4. Short description for title (max 50 chars)
5. Photo description (detailed, max 200 chars)
6. City/area if recognizable (empty if unsure)
7. Country if recognizable (empty if unsure)
8. 55-75 keywords, relevance-sorted, no duplicates, no punctuation

Return JSON ONLY:
{"scene": "landscape", "contrast_adjustment": 0, "saturation_adjustment": 10, "short_desc": "...", "photo_desc": "...", "city_area": "...", "country": "...", "keywords": [...]}"""

    for attempt in range(4):
        try:
            resp = client.chat.completions.create(
                model=vision_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                        {"type": "text", "text": prompt},
                    ]
                }],
            )
            text = resp.choices[0].message.content
            start = text.find("{")
            end = text.rfind("}") + 1
            result = json.loads(text[start:end])
            result.setdefault("scene", "default")
            result.setdefault("contrast_adjustment", 0)
            result.setdefault("saturation_adjustment", 0)
            return result
        except Exception as e:
            log(f"  Vision attempt {attempt+1} failed: {e}")
            time.sleep(4 + attempt * 2)
    return {
        "scene": "default",
        "contrast_adjustment": 0,
        "saturation_adjustment": 0,
        "short_desc": "",
        "photo_desc": "",
        "city_area": "",
        "country": "",
        "keywords": [],
    }


def generate_pp3(scene: str, contrast: int, saturation: int, depth: str = "medium") -> str:
    profile_path = get_pp3_profile(scene, depth)
    if not profile_path or not profile_path.exists():
        log(f"  No pp3 profile for {scene}/{depth}, using neutral")
        profile_path = Path(__file__).parent / "profiles" / "neutral.pp3"
    try:
        content = profile_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    modified = modify_pp3_contrast_saturation(content, contrast, saturation,
                                               skip_override=(depth == "full"))
    return modified


def main():
    parser = argparse.ArgumentParser(description="Selection Pipeline (Phase 2)")
    parser.add_argument("batch", nargs="?", default="",
                        help="Batch name (e.g. SW-England-May26-01)")
parser.add_argument("--phase", default="1", choices=["1"],
                        help="Phase number (default: 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show plan without processing")
    parser.add_argument("--keep-temp", action="store_true",
                        help="Keep temp files after completion")
    parser.add_argument("--profile-depth", default="medium",
                        choices=["minimal", "medium", "full"],
                        help="Profile depth level (default: medium)")
    parser.add_argument("--max-images", type=int, default=0,
                        help="Limit number of images to process")
    parser.add_argument("--local", action="store_true",
                        help="Use local RAW directory instead of Nextcloud")
    args = parser.parse_args()

    batch = args.batch
    nc_client = init_nextcloud()
    use_nextcloud = not args.local

    if use_nextcloud and nc_client is None:
        log("Nextcloud credentials missing, falling back to --local")
        use_nextcloud = False

    temp_dir = LOCAL_TEMP_DIR / f"select_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    raw_files = []

    if use_nextcloud:
        nc_raw_path = f"{NC_SELECT_PIPE}/{batch}" if batch else NC_SELECT_PIPE
        log(f"Nextcloud mode: listing {nc_raw_path}")

        items = nc_client.list_dir(nc_raw_path)
        raw_extensions = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2"}
        nc_raw_names = []
        for item in items:
            ext = Path(item["name"]).suffix.lower()
            if ext in raw_extensions:
                nc_raw_names.append(item["name"])

        if not nc_raw_names:
            log("No RAW files found directly. Checking subfolders...")
            for item in items:
                sub_path = f"{nc_raw_path}/{item['name']}"
                sub_items = nc_client.list_dir(sub_path)
                for si in sub_items:
                    ext = Path(si["name"]).suffix.lower()
                    if ext in raw_extensions:
                        nc_raw_names.append(si["name"])

        if not nc_raw_names:
            log("No RAW files found on Nextcloud")
            sys.exit(0)

        if args.max_images > 0:
            nc_raw_names = sorted(nc_raw_names)[:args.max_images]

        log(f"Found {len(nc_raw_names)} RAW files on Nextcloud")
        raw_dir = temp_dir / "RAW"
        raw_dir.mkdir(parents=True, exist_ok=True)

        for name in nc_raw_names:
            remote = f"{nc_raw_path}/{name}"
            local = raw_dir / name
            if nc_client.download_file(remote, local):
                raw_files.append(local)
                log(f"  Downloaded: {name}")
            else:
                log(f"  FAILED: {name}")

    else:
        input_path = Path(args.batch) if args.batch else Path("./RAW")
        if not input_path.exists():
            log(f"Local path {input_path} not found")
            sys.exit(1)
        if input_path.is_dir():
            raw_extensions = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2"}
            raw_files = [f for f in sorted(input_path.iterdir()) if f.suffix.lower() in raw_extensions]
        else:
            raw_files = [input_path]
        log(f"Local mode: {len(raw_files)} RAW files from {input_path}")

    if not raw_files:
        log("No RAW files to process")
        if use_nextcloud and not args.keep_temp:
            shutil.rmtree(temp_dir, ignore_errors=True)
        sys.exit(0)

    log(f"\n{'='*60}")
    log(f"  SELECTION PIPELINE — Phase {args.phase}")
    log(f"  Batch:     {batch or '(all)'}")
    log(f"  Files:     {len(raw_files)}")
    log(f"  Depth:     {args.profile_depth}")
    log(f"  Profile:   {args.profile_depth}")
    log(f"  Temp:      {temp_dir}")
    log(f"  Mode:      {'Nextcloud' if use_nextcloud else 'Local'}")
    log(f"{'='*60}\n")

    bracket_sets = group_brackets(raw_files)
    log(f"Detected {len(bracket_sets)} bracket sets")

    if args.dry_run:
        log("\nDRY RUN — would process:")
        for i, bs in enumerate(bracket_sets):
            names = [f.name for f in bs]
            log(f"  Set {i+1}: {names}")
        if use_nextcloud and not args.keep_temp:
            log(f"  Temp dir would be cleaned up")
        return

    decisions = []
    kept_count = 0
    rejected_count = 0
    group_number = 0

    for bracket_set in bracket_sets:
        group_number += 1
        log(f"\n--- Group {group_number} ({len(bracket_set)} images) ---")
        for f in bracket_set:
            log(f"  {f.name}")

        best, rejected = select_best_exposure(bracket_set)
        log(f"  Best: {best.name}")

        metrics = analyze_raw_image(best)
        log(f"  Metrics: sharpness={metrics.get('laplacian_var', 0):.1f}, "
            f"noise={metrics.get('noise_energy', 0):.1f}, "
            f"overexposed={metrics.get('overexposed_ratio', 0):.4f}")

        meta = classify_raw(best)
        scene = meta.get("scene", "default")
        contrast = int(meta.get("contrast_adjustment", 0))
        saturation = int(meta.get("saturation_adjustment", 0))
        log(f"  Scene: {scene} | Contrast: {contrast:+d} | Saturation: {saturation:+d}")

        passed, qc_details = quality_check(best, scene=scene)
        t = qc_details.get("thresholds", {})
        log(f"  Quality: {'PASS' if passed else 'FAIL'} "
            f"(sharpness={qc_details['sharpness']:.1f}≥{t.get('sharpness','?')}, "
            f"noise={qc_details['noise_energy']:.1f}≤{t.get('max_noise_energy','?')}, "
            f"overexposed={qc_details['overexposed_ratio']:.3f}≤{t.get('overexposed','?')})")

        if passed:
            pp3_content = generate_pp3(scene, contrast, saturation, args.profile_depth)
            pp3_name = best.stem + ".pp3"

            pp3_local = temp_dir / "pp3" / pp3_name
            pp3_local.parent.mkdir(parents=True, exist_ok=True)
            pp3_local.write_text(pp3_content, encoding="utf-8")
            log(f"  Generated .pp3: {pp3_name}")

            kept_list = [best] + [r for r in rejected if r != best]
            kept_files = kept_list
            rejected_files = []
            decision = "keep"
            kept_count += 1
        else:
            kept_files = []
            rejected_files = [best] + [r for r in rejected if r != best]
            decision = "reject"
            rejected_count += 1

        for f in bracket_set:
            decisions.append({
                "filename": f.name,
                "decision": "keep" if f == best and passed else "reject",
                "reason": (f"Best quality in group #{group_number} ({scene})"
                           if f == best and passed
                           else f"Lower quality than best in group #{group_number}"
                           if f != best and passed
                           else f"Quality check failed: {scene}"
                           if f == best
                           else f"Lower quality than best in group #{group_number}"),
            })

        if passed:
            selected_dir = temp_dir / "selected"
            selected_dir.mkdir(parents=True, exist_ok=True)
            for f in [best] + [r for r in rejected if r != best]:
                dest = selected_dir / f.name
                if f.exists():
                    shutil.copy2(f, dest)
            pp3_local = temp_dir / "pp3" / pp3_name
            pp3_dest = selected_dir / pp3_name
            if pp3_local.exists():
                shutil.copy2(pp3_local, pp3_dest)
        else:
            rejected_out = temp_dir / "rejected"
            rejected_out.mkdir(parents=True, exist_ok=True)
            for f in [best] + [r for r in rejected if r != best]:
                dest = rejected_out / f.name
                if f.exists():
                    shutil.copy2(f, dest)

        gc.collect()

    log(f"\n{'='*60}")
    log(f"  PHASE 2 COMPLETE")
    log(f"  Kept:    {kept_count}")
    log(f"  Rejected: {rejected_count}")
    log(f"{'='*60}")

    report = {
        "batch_name": f"{batch}_phase_{args.phase}",
        "timestamp": datetime.now().isoformat(),
        "total_decisions": len(decisions),
        "kept": kept_count,
        "rejected": rejected_count,
        "ties": 0,
        "tie_details": [],
        "decisions": decisions,
    }

    report_local = temp_dir / f"phase_{args.phase}_report.json"
    with open(report_local, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log(f"Report written: {report_local}")

    if use_nextcloud:
        log(f"\nUploading to Nextcloud...")
        nc_batch = f"{NC_SELECT_PIPE}/{batch}" if batch else NC_SELECT_PIPE

        sel_src = temp_dir / "selected"
        if sel_src.exists():
            nc_sel = f"{nc_batch}/selected-phase_1"
            nc_client.mkdir(nc_sel)
            for f in sorted(sel_src.iterdir()):
                if f.is_file():
                    nc_remote = f"{nc_sel}/{f.name}"
                    if nc_client.upload_file(f, nc_remote):
                        log(f"  Selected: {f.name}")
                    else:
                        log(f"  FAILED: {f.name}")

        rej_src = temp_dir / "rejected"
        if rej_src.exists():
            nc_rej = f"{nc_batch}/rejected-phase_1"
            nc_client.mkdir(nc_rej)
            for f in sorted(rej_src.iterdir()):
                if f.is_file():
                    nc_remote = f"{nc_rej}/{f.name}"
                    if nc_client.upload_file(f, nc_remote):
                        log(f"  Rejected: {f.name}")
                    else:
                        log(f"  FAILED: {f.name}")

        nc_report_path = f"{nc_batch}/phase_2_report.json"
        nc_client.upload_file(report_local, nc_report_path)
        log(f"  Report: phase_{args.phase}_report.json")

        log(f"\n  Results on Nextcloud:")
        log(f"    Selected: Photos/StockFotoCreator/select-pipe-proj/{batch}/selected-phase_1/")
        log(f"    Rejected: Photos/StockFotoCreator/select-pipe-proj/{batch}/rejected-phase_1/")
        log(f"    Report:   Photos/StockFotoCreator/select-pipe-proj/{batch}/phase_{args.phase}_report.json")

        if not args.keep_temp:
            log(f"  Cleaning up temp: {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)
        else:
            log(f"  Temp kept: {temp_dir}")
    else:
        log(f"\nLocal mode output:")
        log(f"  Selected: {temp_dir}/selected/")
        log(f"  Rejected: {temp_dir}/rejected/")


if __name__ == "__main__":
    main()
