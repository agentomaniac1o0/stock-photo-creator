# Stock Photo Creator

Automated pipeline for processing vacation photos (CR2/CR3 RAW) into stock-ready JPEGs.

## Pipeline Steps

1. **Bracket Grouping** — Groups 3-shot AEB bracket sets by EXIF timestamp + exposure compensation
2. **Best Exposure Selection** — Selects the most balanced exposure from each bracket set
3. **Quality Gate** — Sharpness (Laplacian variance), noise (SNR), overexposure checks
4. **File Naming** — `<Location>_<YYYYMMDD>_<NNN>.jpg` (GPS reverse geocoding + Gemini Vision fallback)
5. **Scene Classification + Metadata** — GPT Vision: scene, contrast/saturation recommendations, title, description, 40 keywords
6. **RAW Development** — `rawtherapee-cli` with scene-specific `.pp3` profiles (fallback: rawpy)
7. **Post-Processing** — EXIF rotation, sRGB, upscale if needed, JPEG quality 95
8. **Metadata Writing** — exiftool: IPTC/XMP/EXIF (Artist=dynamixx, Source=Photo)
9. **Upload** — WebDAV → Nextcloud `/Photos/StockFotoCreator/output/`

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt
sudo apt install rawtherapee exiftool

# Process a folder of CR2/CR3 files
python3 photo_pipeline.py /path/to/photos --output ./output --rejected ./rejected

# Dry run (show plan without processing)
python3 photo_pipeline.py /path/to/photos --dry-run
```

## Nextcloud Directory Structure

```
Photos/StockFotoCreator/
  ├── ai-stockimages/     ← Existing AI stock images (moved from Home Lab)
  ├── input/              ← RAW files to process
  ├── rejected/           ← Rejected/failed images
  ├── output/             ← Finished stock-ready JPEGs
  ├── profiles/           ← .pp3 Raw Therapee profiles
  └── metabatch.py        ← Original GPT Vision keyword script
```

## File Naming Convention

`<Location>_<YYYYMMDD>_<NNN>.jpg`

Examples: `Paris_20260514_001.jpg`, `Barcelona_20260320_003.jpg`

Location sources (priority):
1. GPS coordinates → Reverse geocoding (OpenStreetMap Nominatim)
2. Gemini Vision location detection
3. Fallback: "Unknown"

## .pp3 Profiles

Scene-specific Raw Therapee profiles in `profiles/`:

| Scene | Profile | Contrast | Saturation |
|-------|---------|----------|------------|
| landscape | landscape.pp3 | 0 | +10 |
| architecture | architecture.pp3 | +10 | 0 |
| portrait | portrait.pp3 | 0 | -5 |
| street | street.pp3 | +5 | 0 |
| food | food.pp3 | +5 | +15 |
| macro | macro.pp3 | +10 | +5 |
| night | night.pp3 | +15 | 0 |
| fog | (dynamic) | -20 | -10 |
| default | default.pp3 | 0 | 0 |

Gemini Vision determines the scene and can override contrast/saturation per image.

## Keyword Rules

Inherited from `metabatch.py`:
- Exactly 40 keywords per image
- At least 24 single-word keywords (60%)
- At least 16 multi-word phrases (40%)
- Filler words removed from tail
- Title: short description - city/area - country (max 85 chars)
- Description: city/area - country: photo description (max 130 chars)

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXTCLOUD_HOST` | https://192.168.0.82 | Nextcloud host |
| `NEXTCLOUD_USER` | nerdclaudeadm | Nextcloud user |
| `NEXTCLOUD_APP_PASSWORD` | (from ~/.env) | WebDAV password |
| `VISION_MODEL` | gpt-4.1-mini | GPT model for scene classification |
| `COPYRIGHT_NAME` | (empty) | Copyright holder name |
| `ARTIST_NAME` | dynamixx | Artist/creator name |

## Dependencies

- Python 3.10+
- rawpy, Pillow, piexif, imagehash, numpy
- exiftool (system)
- rawtherapee-cli (system, optional — falls back to rawpy)
- OpenAI Python SDK