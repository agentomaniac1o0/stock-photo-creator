#!/usr/bin/env python3
"""
stock_photo_pipeline.py — Automated Stock Photo Pipeline for CR2/CR3 Photos

Processes vacation photos from RAW to stock-ready:
1. Bracket Grouping (EXIF-based, 3-shot AEB detection)
2. Best Exposure Selection (histogram/SNR analysis)
3. Quality Gate (sharpness, noise, overexposure)
4. File Naming (<Location>_<Date>_<NNN>)
5. Scene Classification + Contrast/Saturation + Title/Keywords (Gemini Vision)
6. RAW Development (rawtherapee-cli + scene-specific .pp3 profiles)
7. Post-Processing (Pillow: EXIF rotation, sRGB, upscale)
8. Metadata Writing (exiftool: IPTC/XMP/EXIF)
9. Upload (WebDAV → Nextcloud)

Based on metabatch.py keyword/title logic by the user.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import imagehash
import numpy as np
from PIL import Image, ImageOps
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

try:
    import rawpy
    HAS_RAWPY = True
except ImportError:
    HAS_RAWPY = False

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# ── Configuration ──────────────────────────────────────────────────────────────

INPUT_FOLDER = "./input"
REJECTED_FOLDER = "./rejected"
OUTPUT_FOLDER = "./output"
PROFILES_FOLDER = "./profiles"
LOGFILE = "batch_log.txt"

NEXTCLOUD_HOST = os.getenv("NEXTCLOUD_HOST", "https://192.168.0.82").rstrip("/")
NEXTCLOUD_USER = os.getenv("NEXTCLOUD_USER", "nerdclaudeadm")
NEXTCLOUD_PASS = os.getenv("NEXTCLOUD_APP_PASSWORD", "")
NEXTCLOUD_WEBDAV = f"{NEXTCLOUD_HOST}/remote.php/dav/files/{NEXTCLOUD_USER}/"
NEXTCLOUD_REMOTE_FOLDER = "Photos/StockFotoCreator/output/"

ARTIST_NAME = "dynamixx"
COPYRIGHT_NAME = os.getenv("COPYRIGHT_NAME", "")
SOURCE_TAG = "Photo"

BRACKET_EXPOSURE_TOLERANCE = 2.0
BRACKET_TIME_TOLERANCE_SEC = 2.0
HASH_THRESHOLD = 8

SHARPNESS_THRESHOLD = 50.0
SNR_THRESHOLD = 10.0
OVEREXPOSURE_THRESHOLD = 0.02

TITLE_MAX = 85
DESC_MAX = 130
KEYWORD_COUNT = 40
MIN_SINGLE_WORD = int(KEYWORD_COUNT * 0.60)
MIN_PHRASE = KEYWORD_COUNT - MIN_SINGLE_WORD

PLACEHOLDER_CITY_AREA = "city/area"
PLACEHOLDER_COUNTRY = "country"

VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4.1-mini")
MAX_WORKERS = 2

SCENE_PROFILES = {
    "landscape": "landscape.pp3",
    "architecture": "architecture.pp3",
    "portrait": "portrait.pp3",
    "street": "street.pp3",
    "food": "food.pp3",
    "macro": "macro.pp3",
    "night": "night.pp3",
    "underwater": "underwater.pp3",
    "default": "default.pp3",
}

CONTRAST_SATURATION_DEFAULTS = {
    "landscape": {"contrast": 0, "saturation": 10},
    "architecture": {"contrast": 10, "saturation": 0},
    "portrait": {"contrast": 0, "saturation": -5},
    "street": {"contrast": 5, "saturation": 0},
    "food": {"contrast": 5, "saturation": 15},
    "macro": {"contrast": 10, "saturation": 5},
    "night": {"contrast": 15, "saturation": 0},
    "underwater": {"contrast": 10, "saturation": 10},
    "fog": {"contrast": -20, "saturation": -10},
    "default": {"contrast": 0, "saturation": 0},
}

# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ── EXIF Reading ──────────────────────────────────────────────────────────────

def read_exif_fields(filepath: Path) -> dict:
    try:
        result = subprocess.run(
            ["exiftool", "-json",
             "-EXIF:ExposureCompensation",
             "-EXIF:DateTimeOriginal",
             "-EXIF:ExposureTime",
             "-EXIF:FNumber",
             "-EXIF:ISO",
             "-EXIF:Model",
             "-EXIF:ImageWidth",
             "-EXIF:ImageHeight",
             "-EXIF:Orientation",
             "-GPS:GPSLatitude",
             "-GPS:GPSLatitudeRef",
             "-GPS:GPSLongitude",
             "-GPS:GPSLongitudeRef",
             str(filepath)],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        if data and isinstance(data, list):
            return data[0]
        return {}
    except Exception as e:
        log(f"EXIF read error for {filepath.name}: {e}")
        return {}


def read_exif_tag(filepath: Path, tag: str) -> str:
    try:
        result = subprocess.run(
            ["exiftool", "-s", "-s", "-s", f"-{tag}", str(filepath)],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip()
    except Exception:
        return ""

# ── Step 1: Bracket Grouping ──────────────────────────────────────────────────

def group_brackets(files: list[Path]) -> list[list[Path]]:
    bracket_sets = []
    sorted_files = sorted(files, key=lambda f: f.name)
    
    file_data = {}
    for f in sorted_files:
        exif = read_exif_fields(f)
        ev = exif.get("EXIF:ExposureCompensation", "")
        dt = exif.get("EXIF:DateTimeOriginal", "")
        try:
            ev_val = float(ev) if ev else 0.0
        except (ValueError, TypeError):
            ev_val = 0.0
        
        try:
            ts = datetime.strptime(str(dt), "%Y:%m:%d %H:%M:%S") if dt else None
        except ValueError:
            ts = None
        
        file_data[f] = {"ev": ev_val, "timestamp": ts}
    
    used = set()
    
    for i, f in enumerate(sorted_files):
        if f in used:
            continue
        
        fd = file_data[f]
        if fd["timestamp"] is None:
            bracket_sets.append([f])
            used.add(f)
            continue
        
        group = [f]
        used.add(f)
        
        for j in range(i + 1, min(i + 5, len(sorted_files))):
            fj = sorted_files[j]
            if fj in used:
                continue
            
            fdj = file_data[fj]
            if fdj["timestamp"] is None:
                continue
            
            time_diff = abs((fdj["timestamp"] - fd["timestamp"]).total_seconds())
            if time_diff > BRACKET_TIME_TOLERANCE_SEC:
                break
            
            ev_diff = abs(fdj["ev"] - fd["ev"])
            if ev_diff > 0.2:
                group.append(fj)
                used.add(fj)
        
        if len(group) >= 2:
            bracket_sets.append(group)
        else:
            bracket_sets.append([f])
    
    return bracket_sets

# ── Step 2: Best Exposure Selection ───────────────────────────────────────────

def analyze_raw_image(raw_path: Path) -> dict:
    if not HAS_RAWPY:
        return {"laplacian_var": 0, "snr": 0, "overexposed_ratio": 1.0}
    
    try:
        with rawpy.imread(str(raw_path)) as raw:
            rgb = raw.postprocess(
                params=rawpy.Params(use_camera_wb=True, output_bps=8)
            )
            gray = np.mean(rgb.astype(float), axis=2)
            
            laplacian = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]])
            from scipy.ndimage import convolve
            lap = convolve(gray, laplacian)
            laplacian_var = float(np.var(lap))
            
            signal = np.mean(gray)
            noise = np.std(gray)
            snr = float(signal / noise) if noise > 0 else 0
            
            overexposed = float(np.sum(rgb >= 250) / rgb.size)
            
            return {
                "laplacian_var": laplacian_var,
                "snr": snr,
                "overexposed_ratio": overexposed,
            }
    except Exception as e:
        log(f"RAW analysis error for {raw_path.name}: {e}")
        return {"laplacian_var": 0, "snr": 0, "overexposed_ratio": 1.0}


def select_best_exposure(bracket_set: list[Path]) -> tuple[Path, list[Path]]:
    if len(bracket_set) == 1:
        return bracket_set[0], []
    
    ev_values = {}
    for f in bracket_set:
        exif = read_exif_fields(f)
        try:
            ev = float(exif.get("EXIF:ExposureCompensation", 0))
        except (ValueError, TypeError):
            ev = 0.0
        ev_values[f] = ev
    
    best = min(bracket_set, key=lambda f: abs(ev_values[f]))
    rejected = [f for f in bracket_set if f != best]
    
    return best, rejected

# ── Step 3: Quality Gate ──────────────────────────────────────────────────────

def quality_check(raw_path: Path) -> tuple[bool, dict]:
    metrics = analyze_raw_image(raw_path)
    
    sharp = metrics["laplacian_var"] >= SHARPNESS_THRESHOLD
    low_noise = metrics["snr"] >= SNR_THRESHOLD
    not_overexposed = metrics["overexposed_ratio"] <= OVEREXPOSURE_THRESHOLD
    
    passed = sharp and low_noise and not_overexposed
    
    details = {
        "sharpness": metrics["laplacian_var"],
        "snr": metrics["snr"],
        "overexposed_ratio": metrics["overexposed_ratio"],
        "sharpness_pass": sharp,
        "noise_pass": low_noise,
        "exposure_pass": not_overexposed,
    }
    
    return passed, details

# ── Step 4: File Naming + Location ────────────────────────────────────────────

def get_location_from_gps(exif: dict) -> str:
    lat = exif.get("EXIF:GPSLatitude", "")
    lat_ref = exif.get("EXIF:GPSLatitudeRef", "N")
    lon = exif.get("EXIF:GPSLongitude", "")
    lon_ref = exif.get("EXIF:GPSLongitudeRef", "E")
    
    if not lat or not lon:
        return ""
    
    try:
        def parse_dms(dms_str):
            parts = str(dms_str).replace("deg", "").replace("'", "").replace('"', "").split()
            if len(parts) >= 3:
                return float(parts[0]) + float(parts[1]) / 60 + float(parts[2]) / 3600
            return float(parts[0])
        
        lat_val = parse_dms(lat) * (1 if lat_ref == "N" else -1)
        lon_val = parse_dms(lon) * (1 if lon_ref == "E" else -1)
        
        try:
            import requests
            resp = requests.get(
                f"https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat_val, "lon": lon_val, "format": "json", "accept-language": "en"},
                headers={"User-Agent": "StockPhotoCreator/1.0"},
                timeout=10
            )
            if resp.status_code == 200:
                addr = resp.json().get("address", {})
                city = addr.get("city") or addr.get("town") or addr.get("village") or ""
                return city.replace(" ", "").replace("-", "_")
        except Exception:
            pass
        
        return ""
    except Exception:
        return ""


def generate_filename(image_path: Path, location: str, counter: int) -> str:
    exif = read_exif_fields(image_path)
    dt_str = exif.get("EXIF:DateTimeOriginal", "")
    
    try:
        dt = datetime.strptime(str(dt_str), "%Y:%m:%d %H:%M:%S")
        date_str = dt.strftime("%Y%m%d")
    except (ValueError, TypeError):
        dt = datetime.fromtimestamp(image_path.stat().st_mtime)
        date_str = dt.strftime("%Y%m%d")
    
    if not location:
        location = get_location_from_gps(exif)
    
    loc_safe = re.sub(r'[^\w]', '', location) if location else "Unknown"
    return f"{loc_safe}_{date_str}_{counter:03d}"

# ── Step 5: Scene Classification + Metadata (GPT Vision) ─────────────────────

def get_openai_client():
    try:
        key = subprocess.check_output(
            ["secret-tool", "lookup", "service", "openai", "purpose", "stockfoto"],
            text=True
        ).strip()
        return OpenAI(api_key=key)
    except Exception:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if api_key:
            return OpenAI(api_key=api_key)
        return None


def resize_and_encode(filepath: Path) -> str:
    img = Image.open(filepath)
    img = img.convert("RGB")
    max_size = 800
    img.thumbnail((max_size, max_size))
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=70)
    import base64
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def classify_and_describe(filepath: Path) -> dict:
    client = get_openai_client()
    if client is None:
        log("WARNING: No OpenAI API key available. Using defaults.")
        return {
            "scene": "default",
            "contrast_adjustment": 0,
            "saturation_adjustment": 0,
            "short_desc": "Untitled photo",
            "photo_desc": "Photo description",
            "city_area": "",
            "country": "",
            "keywords": []
        }
    
    image_b64 = resize_and_encode(filepath)
    
    prompt_text = f"""You generate stock photography metadata in ENGLISH.

Analyze this photo and provide:

1. Scene classification: one of: landscape, architecture, portrait, street, food, macro, night, underwater, fog
2. Contrast recommendation (-30 to +30): 
   - Fog/atmosphere → negative (-15 to -25)
   - Motion dynamics → slightly negative (-5 to -10)
   - Gray weather → positive (+15 to +25)
   - Normal/sunny → 0
3. Saturation recommendation (-30 to +30):
   - Autumn/landscape → slightly more (+10)
   - Fog → less (-10)
   - Portrait → slightly less (-5)
   - Flowers/nature → more (+15)
   - Normal → 0
4. Short description for title (max 50 chars)
5. Photo description (detailed, max 200 chars)
6. City/area if recognizable (empty if unsure)
7. Country if recognizable (empty if unsure)
8. 55-75 keywords, relevance-sorted, no duplicates, no punctuation
   IMPORTANT: at least 60% single words and at least 40% multi-word phrases (2-4 words)

Return JSON ONLY:
{{
  "scene": "landscape",
  "contrast_adjustment": 0,
  "saturation_adjustment": 10,
  "short_desc": "short description",
  "photo_desc": "detailed photo description",
  "city_area": "city or area or empty string",
  "country": "country or empty string",
  "keywords": ["keyword1", "keyword phrase", ...]
}}"""

    retries = 4
    delay = 4
    
    for attempt in range(retries):
        try:
            log(f"Vision request: {filepath.name} attempt {attempt+1}/{retries}")
            response = client.chat.completions.create(
                model=VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                        {"type": "text", "text": prompt_text},
                    ]
                }],
            )
            text = response.choices[0].message.content
            start = text.find("{")
            end = text.rfind("}") + 1
            result = json.loads(text[start:end])
            
            if "scene" not in result:
                result["scene"] = "default"
            if "contrast_adjustment" not in result:
                result["contrast_adjustment"] = 0
            if "saturation_adjustment" not in result:
                result["saturation_adjustment"] = 0
            
            return result
            
        except Exception as e:
            log(f"Vision failed: {type(e).__name__}: {str(e)[:180]}")
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay += 2
    
    raise RuntimeError(f"Vision API failed after {retries} attempts")

# ── Step 5b: Keyword Logic (from metabatch.py) ────────────────────────────────

def normalize_location(city_area, country):
    def clean(x):
        if not x:
            return ""
        x = str(x).strip()
        if x.lower() in {"unknown", "n/a", "na", "none", "null", "unsure", "uncertain", "not sure"}:
            return ""
        return x
    ca = clean(city_area) or PLACEHOLDER_CITY_AREA
    co = clean(country) or PLACEHOLDER_COUNTRY
    return ca, co


def clamp_text(s, max_len):
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return s[:max_len]
    return s[: max_len - 1].rstrip() + "…"


def make_title(short_desc, city_area, country):
    loc_part = f"{city_area} - {country}".strip()
    sd = (short_desc or "").strip()
    base = f"{sd} - {loc_part}".strip()
    if len(base) <= TITLE_MAX:
        return base
    overhead = len(" - ") + len(loc_part)
    available = max(1, TITLE_MAX - overhead)
    sd2 = clamp_text(sd, available)
    return clamp_text(f"{sd2} - {loc_part}", TITLE_MAX)


def make_description(photo_desc, city_area, country):
    prefix = f"{city_area} - {country}: "
    max_photo = max(0, DESC_MAX - len(prefix))
    pd = clamp_text(photo_desc or "", max_photo)
    return clamp_text(prefix + pd, DESC_MAX)


FILLER = {
    "photo", "photography", "image", "picture", "background",
    "travel", "tourism", "vacation", "holiday",
    "outdoor", "nature", "landscape",
    "architecture", "city", "town", "street",
    "summer", "winter", "spring", "autumn",
    "day", "night", "sunset", "sunrise",
    "sky", "clouds", "building", "buildings",
    "destination", "copy space", "no people", "nobody",
}

STOPWORDS = {
    "a", "an", "the", "and", "or", "with", "without", "in", "on", "at", "of", "for", "to", "from",
    "is", "are", "was", "were", "be", "being", "been",
    "this", "that", "these", "those",
    "nice", "beautiful", "pretty", "amazing", "stunning", "great",
    "photo", "image", "picture", "snapshot", "shot", "view", "scene",
    "background", "foreground", "copy", "space", "template",
}

MODIFIERS = {
    "blue", "golden", "old", "ancient", "modern", "historic", "urban", "rural",
    "sunny", "cloudy", "rainy", "snowy", "foggy", "misty",
    "night", "evening", "morning", "dawn", "dusk",
    "calm", "peaceful", "dramatic",
}

SUBJECTS = {
    "sky", "clouds", "beach", "sea", "ocean", "waves", "sand", "coast", "shore",
    "city", "street", "building", "architecture", "bridge",
    "mountain", "forest", "lake", "river", "waterfall",
    "sunset", "sunrise", "horizon", "landscape", "nature",
}


def is_phrase(k):
    return " " in str(k).strip()


def is_good_single(word):
    w = (word or "").strip().lower()
    if not w or w in STOPWORDS or w.isdigit() or len(w) <= 2:
        return False
    return True


def dedupe_preserve_order(keywords):
    seen = set()
    out = []
    for k in keywords:
        k = str(k).strip()
        if not k:
            continue
        kl = k.lower()
        if kl in seen:
            continue
        seen.add(kl)
        out.append(k)
    return out


def dedupe_phrases_unordered(phrases):
    out = []
    seen = set()
    for p in phrases:
        p = str(p).strip()
        parts = p.lower().split()
        if not (2 <= len(parts) <= 4):
            continue
        key = " ".join(sorted(parts))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def build_phrases_from_singles(singles, max_phrases=10, max_per_modifier=2):
    good = []
    seen_single = set()
    for s in singles:
        w = str(s).strip()
        k = w.lower()
        if k in seen_single:
            continue
        if not is_good_single(w):
            continue
        seen_single.add(k)
        good.append(w)
    
    if len(good) < 2:
        return []
    
    mods = [w for w in good if w.lower() in MODIFIERS]
    subs = [w for w in good if w.lower() in SUBJECTS]
    others = [w for w in good if w.lower() not in MODIFIERS and w.lower() not in SUBJECTS]
    
    phrases = []
    seen_phrase = set()
    modifier_count = {}
    
    def add_phrase(a, b, modifier=None):
        key = " ".join(sorted([a.lower(), b.lower()]))
        if key in seen_phrase:
            return False
        if modifier is not None:
            m = modifier.lower()
            if modifier_count.get(m, 0) >= max_per_modifier:
                return False
            modifier_count[m] = modifier_count.get(m, 0) + 1
        seen_phrase.add(key)
        phrases.append(f"{a} {b}")
        return True
    
    for b in subs:
        for a in mods:
            if len(phrases) >= max_phrases:
                break
            add_phrase(a, b, modifier=a)
        if len(phrases) >= max_phrases:
            break
    
    if len(phrases) < max_phrases:
        for i in range(len(subs) - 1):
            if len(phrases) >= max_phrases:
                break
            add_phrase(subs[i], subs[i + 1])
    
    if len(phrases) < max_phrases:
        pool = subs + others
        for i in range(len(pool)):
            if len(phrases) >= max_phrases:
                break
            for j in range(i + 1, len(pool)):
                if len(phrases) >= max_phrases:
                    break
                add_phrase(pool[i], pool[j])
    
    return phrases[:max_phrases]


def extract_singles_from_phrases(phrases, existing_singles):
    existing = {str(s).strip().lower() for s in existing_singles}
    out = []
    for p in phrases:
        parts = str(p).strip().split()
        if not (2 <= len(parts) <= 4):
            continue
        for w in parts:
            wl = w.strip().lower()
            if wl in existing:
                continue
            if not is_good_single(w):
                continue
            existing.add(wl)
            out.append(w.strip())
    return out


def trim_to_n_with_mix(keywords, n=KEYWORD_COUNT):
    target_singles = MIN_SINGLE_WORD
    target_phrases = MIN_PHRASE
    max_phrases_cap = 22
    
    kws = dedupe_preserve_order(keywords)
    
    if len(kws) > n:
        keep = kws[:]
        i = len(keep) - 1
        while len(keep) > n and i >= 0:
            if keep[i].strip().lower() in FILLER:
                keep.pop(i)
            i -= 1
        kws = keep[:n] if len(keep) > n else keep
    
    singles = dedupe_preserve_order([k for k in kws if not is_phrase(k)])
    phrases = dedupe_phrases_unordered(dedupe_preserve_order([k for k in kws if is_phrase(k)]))
    
    used_phrase_fallback = False
    if len(phrases) < target_phrases:
        need = target_phrases - len(phrases)
        gen = build_phrases_from_singles(singles, max_phrases=need * 4, max_per_modifier=2)
        gen = dedupe_phrases_unordered(gen)
        phrases = dedupe_phrases_unordered(phrases + gen[:need])
        used_phrase_fallback = True
    
    extracted = 0
    if len(singles) < target_singles:
        extra = extract_singles_from_phrases(phrases, singles)
        extracted = len(extra)
        singles = dedupe_preserve_order(singles + extra)
    
    core_singles = singles[:target_singles]
    core_phrases = phrases[:target_phrases]
    
    result = []
    si = pi = 0
    while len(result) < (len(core_singles) + len(core_phrases)):
        if si < len(core_singles):
            result.append(core_singles[si])
            si += 1
        if pi < len(core_phrases):
            result.append(core_phrases[pi])
            pi += 1
    
    for s in singles[target_singles:]:
        if len(result) >= n:
            break
        if s not in result:
            result.append(s)
    
    for p in phrases[target_phrases:]:
        if len(result) >= n:
            break
        if p not in result and sum(1 for x in result if is_phrase(x)) < max_phrases_cap:
            result.append(p)
    
    emergency_used = False
    if len(result) < n:
        need_more = n - len(result)
        gen_more = build_phrases_from_singles(singles, max_phrases=need_more * 10, max_per_modifier=2)
        gen_more = dedupe_phrases_unordered(gen_more)
        for p in gen_more:
            if len(result) >= n:
                break
            if p not in result and sum(1 for x in result if is_phrase(x)) < max_phrases_cap:
                result.append(p)
        
        if len(result) < n:
            emergency_used = True
            good_tokens = []
            seen = set()
            for s in singles:
                w = str(s).strip()
                wl = w.lower()
                if wl in seen:
                    continue
                if not is_good_single(w):
                    continue
                seen.add(wl)
                good_tokens.append(w)
            
            existing_phrase_keys = {
                " ".join(sorted(x.lower().split()))
                for x in result if is_phrase(x)
            }
            
            for i in range(len(good_tokens)):
                if len(result) >= n:
                    break
                for j in range(i + 1, len(good_tokens)):
                    if len(result) >= n:
                        break
                    p = f"{good_tokens[i]} {good_tokens[j]}"
                    key = " ".join(sorted(p.lower().split()))
                    if key in existing_phrase_keys:
                        continue
                    result.append(p)
                    existing_phrase_keys.add(key)
    
    result = result[:n]
    
    sg = sum(1 for x in result if not is_phrase(x))
    ph = len(result) - sg
    
    if used_phrase_fallback or extracted or emergency_used or sg < target_singles or ph < target_phrases:
        if sg < target_singles or ph < target_phrases:
            log(f"WARNING: Keyword mix not fully met: singles={sg} phrases={ph} "
                f"(target >= {target_singles}/{target_phrases}). "
                f"Fallbacks: phrase_fallback={used_phrase_fallback} extracted_singles={extracted} "
                f"emergency_fill={emergency_used}.")
        else:
            log(f"INFO: Keyword mix OK after fallbacks: singles={sg} phrases={ph}. "
                f"Fallbacks: phrase_fallback={used_phrase_fallback} extracted_singles={extracted} "
                f"emergency_fill={emergency_used}.")
    
    return result

# ── Step 6: RAW Development ───────────────────────────────────────────────────

def get_pp3_profile(scene: str) -> Path:
    profile_name = SCENE_PROFILES.get(scene, SCENE_PROFILES["default"])
    profile_path = Path(PROFILES_FOLDER) / profile_name
    if profile_path.exists():
        return profile_path
    default_path = Path(PROFILES_FOLDER) / SCENE_PROFILES["default"]
    if default_path.exists():
        return default_path
    return None


def modify_pp3_contrast_saturation(pp3_content: str, contrast: int, saturation: int) -> str:
    lines = pp3_content.split("\n")
    in_sharpening = False
    new_lines = []
    
    for line in lines:
        stripped = line.strip()
        
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            in_sharpening = section == "Sharpening"
        
        if "[Contrast]" in stripped:
            in_sharpening = False
        
        if "Contrast=" in stripped and not in_sharpening:
            line = re.sub(r'Contrast=\d+', f'Contrast={contrast}', line)
        elif "Saturation=" in stripped:
            line = re.sub(r'Saturation=\d+', f'Saturation={saturation}', line)
        
        new_lines.append(line)
    
    return "\n".join(new_lines)


def develop_raw(raw_path: Path, output_path: Path, pp3_path: Optional[Path] = None,
                 contrast: int = 0, saturation: int = 0) -> bool:
    try:
        rt_cli = subprocess.run(
            ["which", "rawtherapee-cli"],
            capture_output=True, text=True
        )
        if rt_cli.returncode != 0:
            log("rawtherapee-cli not found, falling back to rawpy")
            return develop_raw_with_rawpy(raw_path, output_path)
        
        effective_pp3 = None
        if pp3_path and pp3_path.exists():
            with open(pp3_path, "r") as f:
                pp3_content = f.read()
            if contrast != 0 or saturation != 0:
                pp3_content = modify_pp3_contrast_saturation(pp3_content, contrast, saturation)
                modified_pp3 = output_path.with_suffix(".pp3")
                with open(modified_pp3, "w") as f:
                    f.write(pp3_content)
                effective_pp3 = modified_pp3
            else:
                effective_pp3 = pp3_path
        
        cmd = ["rawtherapee-cli"]
        if effective_pp3:
            cmd.extend(["-p", str(effective_pp3)])
        cmd.extend(["-o", str(output_path), "-j", "95", "-c", str(raw_path)])
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        if effective_pp3 and effective_pp3 != pp3_path:
            effective_pp3.unlink(missing_ok=True)
        
        return result.returncode == 0
        
    except FileNotFoundError:
        log("rawtherapee-cli not found, falling back to rawpy")
        return develop_raw_with_rawpy(raw_path, output_path)
    except Exception as e:
        log(f"RAW development error: {e}")
        return develop_raw_with_rawpy(raw_path, output_path)


def develop_raw_with_rawpy(raw_path: Path, output_path: Path) -> bool:
    if not HAS_RAWPY:
        log("ERROR: Neither rawtherapee-cli nor rawpy available")
        return False
    
    try:
        with rawpy.imread(str(raw_path)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                output_bps=8,
                no_auto_bright=False,
            )
        img = Image.fromarray(rgb)
        img.save(str(output_path), "JPEG", quality=95)
        return True
    except Exception as e:
        log(f"rawpy development error: {e}")
        return False

# ── Step 7: Post-Processing ───────────────────────────────────────────────────

MIN_STOCK_WIDTH = 2560
MIN_STOCK_HEIGHT = 1704
MIN_STOCK_MP = 5

def postprocess_image(img_path: Path) -> bool:
    try:
        img = Image.open(img_path)
        
        orientation = ImageOps.exif_transpose(img)
        if orientation is not None:
            img = orientation
        
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        
        w, h = img.size
        current_mp = (w * h) / 1_000_000
        
        if current_mp < MIN_STOCK_MP or w < MIN_STOCK_WIDTH or h < MIN_STOCK_HEIGHT:
            target_mp = max(MIN_STOCK_MP, current_mp * 2)
            factor = (target_mp / current_mp) ** 0.5
            new_w = max(int(w * factor), MIN_STOCK_WIDTH)
            new_h = max(int(h * factor), MIN_STOCK_HEIGHT)
            
            if new_w / new_h > w / h:
                new_w = int(new_h * (w / h))
            else:
                new_h = int(new_w * (h / w))
            
            img = img.resize((new_w, new_h), Image.LANCZOS)
        
        from PIL import ImageFilter
        if current_mp < MIN_STOCK_MP:
            img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=100, threshold=3))
        
        img.save(str(img_path), "JPEG", quality=95, icc_profile=img.info.get("icc_profile"))
        return True
        
    except Exception as e:
        log(f"Post-processing error: {e}")
        return False

# ── Step 8: Metadata Writing ──────────────────────────────────────────────────

def write_metadata_exiftool(jpg_path: Path, title: str, description: str,
                            keywords: list[str], artist: str = ARTIST_NAME,
                            copyright_name: str = "") -> bool:
    try:
        args = [
            "exiftool",
            "-overwrite_original",
            "-charset", "UTF8",
            "-codedcharacterset=UTF8",
            f"-IPTC:ObjectName={title}",
            f"-IPTC:Caption-Abstract={description}",
            f"-XMP:Title={title}",
            f"-XMP:Description={description}",
            f"-XMP:Source={SOURCE_TAG}",
            f"-XMP:Creator={artist}",
            f"-XMP:Rights={copyright_name or artist}",
            f"-EXIF:Artist={artist}",
            f"-EXIF:Copyright={copyright_name or artist}",
            f"-EXIF:Software=Stock Photo Creator Pipeline",
            f"-EXIF:ImageDescription={description}",
            f"-EXIF:UserComment=Stock photography by {artist}",
        ]
        
        for kw in keywords[:40]:
            args.append(f"-IPTC:Keywords={kw}")
            args.append(f"-XMP:Subject={kw}")
        
        xp_keywords = ", ".join(keywords[:40])
        args.append(f"-XMP:Keywords={xp_keywords}")
        
        args.append(str(jpg_path))
        
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log(f"      exiftool warning: {result.stderr.strip()[:100]}")
            return False
        return True
        
    except FileNotFoundError:
        log("exiftool not found")
        return False
    except Exception as e:
        log(f"exiftool error: {e}")
        return False


def create_sidecar(filename: str, title: str, description: str,
                   keywords: list[str], scene: str, location: str,
                   contrast: int, saturation: int) -> dict:
    sidecar = {
        "filename": filename,
        "title": title,
        "description": description,
        "keywords": keywords[:40],
        "keyword_count": len(keywords[:40]),
        "scene": scene,
        "location": location,
        "contrast_adjustment": contrast,
        "saturation_adjustment": saturation,
        "category": "Photography",
        "ai_generated": False,
        "artist": ARTIST_NAME,
        "copyright": COPYRIGHT_NAME or ARTIST_NAME,
        "created_at": datetime.now().isoformat(),
        "language": "en",
    }
    return sidecar

# ── Step 9: Upload ────────────────────────────────────────────────────────────

def upload_to_nextcloud(local_path: Path, remote_folder: str = NEXTCLOUD_REMOTE_FOLDER) -> bool:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    if not NEXTCLOUD_PASS:
        log("NEXTCLOUD_APP_PASSWORD not set. Skipping upload.")
        return False
    
    remote_url = f"{NEXTCLOUD_WEBDAV.rstrip('/')}/{remote_folder.strip('/')}/{local_path.name}"
    
    try:
        with open(local_path, "rb") as f:
            response = requests.put(
                remote_url, data=f,
                auth=(NEXTCLOUD_USER, NEXTCLOUD_PASS),
                timeout=120, verify=False,
            )
        
        if response.status_code in (201, 204):
            log(f"  uploaded to Nextcloud: {remote_url}")
            return True
        else:
            log(f"  Nextcloud upload failed: HTTP {response.status_code}")
            return False
            
    except Exception as e:
        log(f"  Nextcloud upload error: {e}")
        return False

# ── Main Pipeline ─────────────────────────────────────────────────────────────

def process_bracket_set(bracket_set: list[Path], output_dir: Path, rejected_dir: Path,
                        counter: int) -> Optional[Path]:
    log(f"\n--- Processing bracket set ({len(bracket_set)} images) ---")
    
    for f in bracket_set:
        log(f"  {f.name}")
    
    best, rejected = select_best_exposure(bracket_set)
    log(f"  Selected best: {best.name}")
    
    for r in rejected:
        dest = rejected_dir / r.name
        r.rename(dest)
        log(f"  Rejected → {dest.name}")
    
    passed, details = quality_check(best)
    if not passed:
        log(f"  Quality check FAILED: sharpness={details['sharpness']:.1f} "
            f"snr={details['snr']:.1f} overexposed={details['overexposed_ratio']:.3f}")
        dest = rejected_dir / best.name
        best.rename(dest)
        return None
    
    log(f"  Quality check PASSED")
    
    meta = classify_and_describe(best)
    scene = meta.get("scene", "default")
    contrast = int(meta.get("contrast_adjustment", 0))
    saturation = int(meta.get("saturation_adjustment", 0))
    
    city_area, country = normalize_location(
        meta.get("city_area", ""),
        meta.get("country", "")
    )
    
    location = city_area
    if location == PLACEHOLDER_CITY_AREA:
        gps_location = get_location_from_gps(read_exif_fields(best))
        if gps_location:
            location = gps_location
    
    base_name = generate_filename(best, location, counter)
    
    short_desc = (meta.get("short_desc") or "").strip()
    photo_desc = (meta.get("photo_desc") or "").strip()
    title = make_title(short_desc, city_area, country)
    desc = make_description(photo_desc, city_area, country)
    
    keywords = [str(k).strip() for k in (meta.get("keywords") or []) if str(k).strip()]
    keywords = trim_to_n_with_mix(keywords, KEYWORD_COUNT)
    
    sg = sum(1 for x in keywords if not is_phrase(x))
    ph = len(keywords) - sg
    log(f"  Scene: {scene} | Contrast: {contrast:+d} | Saturation: {saturation:+d}")
    log(f"  Title: {title} ({len(title)} chars)")
    log(f"  Description: {desc} ({len(desc)} chars)")
    log(f"  Keywords: {len(keywords)} (singles={sg} phrases={ph})")
    log(f"  Location: {location}")
    log(f"  Filename: {base_name}")
    
    output_jpg = output_dir / f"{base_name}.jpg"
    pp3_profile = get_pp3_profile(scene)
    
    if pp3_profile:
        log(f"  Using pp3 profile: {pp3_profile.name}")
    
    success = develop_raw(best, output_jpg, pp3_profile, contrast, saturation)
    if not success:
        log(f"  ERROR: RAW development failed for {best.name}")
        return None
    
    log(f"  Developed: {output_jpg.name}")
    
    success = postprocess_image(output_jpg)
    if not success:
        log(f"  ERROR: Post-processing failed for {output_jpg.name}")
        return None
    
    log(f"  Post-processed: {output_jpg.name}")
    
    write_metadata_exiftool(output_jpg, title, desc, keywords,
                            artist=ARTIST_NAME, copyright_name=COPYRIGHT_NAME)
    
    sidecar = create_sidecar(output_jpg.name, title, desc, keywords,
                             scene, location, contrast, saturation)
    sidecar_path = output_dir / f"{base_name}_metadata.json"
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f, ensure_ascii=False, indent=2)
    
    log(f"  Metadata written")
    
    upload_to_nextcloud(output_jpg)
    upload_to_nextcloud(sidecar_path, "Photos/StockFotoCreator/output/")
    
    return output_jpg


def main():
    parser = argparse.ArgumentParser(description="Stock Photo Creator Pipeline")
    parser.add_argument("input", nargs="?", default=INPUT_FOLDER,
                        help="Input folder containing CR2/CR3 files")
    parser.add_argument("--output", default=OUTPUT_FOLDER, help="Output folder")
    parser.add_argument("--rejected", default=REJECTED_FOLDER, help="Rejected folder")
    parser.add_argument("--profiles", default=PROFILES_FOLDER, help="pp3 profiles folder")
    parser.add_argument("--dry-run", action="store_true", help="Don't process, just show plan")
    parser.add_argument("--max-workers", type=int, default=1, help="Parallel workers")
    parser.add_argument("--start-counter", type=int, default=1, help="Starting counter for filenames")
    args = parser.parse_args()
    
    global INPUT_FOLDER, OUTPUT_FOLDER, REJECTED_FOLDER, PROFILES_FOLDER
    
    input_dir = Path(args.input)
    output_dir = Path(args.output)
    rejected_dir = Path(args.rejected)
    profiles_dir = Path(args.profiles)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    rejected_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    
    INPUT_FOLDER = str(input_dir)
    OUTPUT_FOLDER = str(output_dir)
    REJECTED_FOLDER = str(rejected_dir)
    PROFILES_FOLDER = str(profiles_dir)
    
    log("=" * 60)
    log("  STOCK PHOTO CREATOR PIPELINE")
    log("=" * 60)
    log(f"  Input:    {input_dir}")
    log(f"  Output:   {output_dir}")
    log(f"  Rejected: {rejected_dir}")
    log(f"  Profiles: {profiles_dir}")
    
    raw_files = list(input_dir.glob("*.CR2")) + list(input_dir.glob("*.cr2")) + \
                list(input_dir.glob("*.CR3")) + list(input_dir.glob("*.cr3"))
    
    if not raw_files:
        jpg_files = list(input_dir.glob("*.jpg")) + list(input_dir.glob("*.JPG")) + \
                    list(input_dir.glob("*.jpeg")) + list(input_dir.glob("*.JPEG"))
        if jpg_files:
            log(f"  No RAW files found. Processing {len(jpg_files)} JPEG files directly.")
            raw_files = jpg_files
        else:
            log("  No image files found. Exiting.")
            sys.exit(0)
    else:
        log(f"  Found {len(raw_files)} RAW files.")
    
    bracket_sets = group_brackets(raw_files)
    log(f"  Detected {len(bracket_sets)} bracket sets/groups")
    
    if args.dry_run:
        log("\n  DRY RUN - would process:")
        for i, bs in enumerate(bracket_sets):
            log(f"    Set {i+1}: {[f.name for f in bs]}")
        return
    
    counter = args.start_counter
    results = []
    
    for bracket_set in bracket_sets:
        result = process_bracket_set(bracket_set, output_dir, rejected_dir, counter)
        if result:
            results.append(result)
            counter += 1
    
    log("\n" + "=" * 60)
    log(f"  PIPELINE COMPLETE")
    log(f"  Processed: {len(results)} images")
    log(f"  Output:    {output_dir}")
    log(f"  Rejected:  {rejected_dir}")
    log("=" * 60)


if __name__ == "__main__":
    main()