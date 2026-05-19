# Stock Photo Creator — AGENTS.md

## Project: stock-photo-creator

Automated pipeline for processing vacation photos (CR2/CR3) into stock-ready JPEGs.

### Key Decisions (2026-05-14, updated 2026-05-14)

| Decision | Choice |
|----------|--------|
| RAW format | Canon CR2/CR3, 3-shot AEB |
| Bracket selection | EXIF-based grouping (timestamp ±2s, different EV) |
| RAW development | rawtherapee-cli + scene-specific .pp3 profiles |
| Quality filter | Rejected images → Nextcloud rejected/ (not deleted) |
| GIMP step v1 | Omitted — direct RT → Stock |
| Metadata | Gemini/GPT Vision: title + description + 40 keywords + scene + contrast/saturation |
| Batch mode | Full folder at once, auto batch naming (date + location) |
| pp3 profiles | Scene-specific, 3 depth levels (minimal/medium/full) |
| Profile depth | minimal=Contrast+Sat, medium=+Sharpening/Shadows/Vibrance, full=complete profiles |
| GPT override | minimal+medium: GPT overrides contrast/saturation; full: override skipped |
| File naming | `<Location>_<YYYYMMDD>_<NNN>.jpg` |
| Location source | GPS reverse geocoding (Nominatim), fallback: Gemini Vision, fallback: folder name |
| **Storage** | **All file exchange via Nextcloud (not local VM)** |
| Nextcloud base | `/Photos/StockFotoCreator/` |
| Local temp | `~/stock-pipeline-temp/` (cleaned up after each batch) |
| Default mode | `--all-depths` (processes minimal, medium, full in one run) |
| Artist | dynamixx |
| Copyright | Real name (configurable) |
| Source tag | Photo (not "AI Generated") |
| Keyword logic | Inherited from metabatch.py: 40 keywords, 24 singles / 16 phrases |

### Nextcloud Directory Structure

```
Nextcloud: /Photos/StockFotoCreator/
  RAW/                          ← User uploads RAW batches here
    Barcelona_Trip/
      IMG_001.CR2
      IMG_002.CR2
  output/                       ← Pipeline output (batch subfolders)
    2026-05-14_Barcelona/
      Barcelona_20260514_001.jpg
      Barcelona_20260514_001_metadata.json
  rejected/                     ← Rejected images (batch subfolders)
    2026-05-14_Barcelona/
      IMG_003.CR2
  profiles/
    neutral.pp3
    minimal/                    ← Contrast + Saturation only
      landscape.pp3, architecture.pp3, ...
    medium/                     ← +Sharpening, Shadows/Highlights, Vibrance
      landscape.pp3, architecture.pp3, ...
    full/                       ← Complete: +Local Contrast, NR, Tone Curves
      landscape.pp3, architecture.pp3, ...
  reapply-metadata/             ← GIMP-edited JPEGs go here
  done/                         ← Final files after metadata reapply
  scripts/
    generate_profiles.py
    reapply_metadata.py
  select-pipe-proj/             ← Selection pipeline (Phase 2) batches
    SW-England-May26-01/
      IMG_1525.CR2              ← Original RAWs (or moved to selected/rejected)
      phase_1_report.json       ← Previous run report
      selected-phase_1/         ← Auto-selected RAWs + .pp3 files
        IMG_1527.CR2
        IMG_1527.pp3            ← Scene-specific .pp3 sidecar
      rejected-phase_1/         ← Auto-rejected RAWs
        IMG_1525.CR2
      phase_2_report.json       ← Latest run report
```

### Pipeline Flow (Nextcloud Mode — Default)

```
1. Pipeline reads RAW files from Nextcloud → /Photos/StockFotoCreator/RAW/{batch}/
2. Downloads to ~/stock-pipeline-temp/ (24GB free space)
3. Downloads pp3 profiles from Nextcloud → /Photos/StockFotoCreator/profiles/ (all 3 depths)
4. GPT Vision: scene + metadata once per image
5. Develops each image 3x (minimal, medium, full) with respective pp3 profiles
6. Processes locally in temp dir (bracket, develop, straighten, crop, metadata)
7. Uploads results to Nextcloud → /Photos/StockFotoCreator/output/{batch}_{depth}/
8. Uploads rejected to Nextcloud → /Photos/StockFotoCreator/rejected/{batch}/
9. Cleans up temp dir
```

### Selection Pipeline Flow (Phase 1 — Nextcloud Mode)

```
1. select_pipeline.py reads RAWs from Nextcloud → select-pipe-proj/{batch}/
2. Downloads to ~/stock-pipeline-temp/
3. Groups by AEB (EXIF timestamp + EV)
4. For each group: select best exposure, analyze RAW quality, quality check
5. Extract embedded JPEG thumbnail from CR2 → Gemini Vision (scene, contrast, saturation)
6. Generate .pp3 profile with scene-specific settings + GPT overrides
7. Sort: selected RAWs + .pp3 → selected-phase_1/, rejected → rejected-phase_1/
8. Upload results + phase_1_report.json to Nextcloud
9. Clean up temp dir (unless --keep-temp)
```

### Output Structure (all-depths mode)

```
Nextcloud: /Photos/StockFotoCreator/
  output/
    2026-05-14_Barcelona_minimal/
      Barcelona_20260514_001_minimal.jpg
      Barcelona_20260514_001_minimal_metadata.json
    2026-05-14_Barcelona_medium/
      Barcelona_20260514_001_medium.jpg
      Barcelona_20260514_001_medium_metadata.json
    2026-05-14_Barcelona_full/
      Barcelona_20260514_001_full.jpg
      Barcelona_20260514_001_full_metadata.json
```

### Scene Profiles (Contrast/Saturation Defaults)

| Scene | Contrast | Saturation | Notes |
|-------|----------|------------|-------|
| landscape | 0 | +10 | Vibrance boost |
| architecture | +10 | 0 | Extra sharpening |
| portrait | 0 | -5 | Soft sharpening, shadow recovery |
| street | +5 | 0 | Slight contrast pop |
| food | +5 | +15 | Vibrance for appetizing colors |
| macro | +10 | +5 | Sharp details |
| night | +15 | 0 | Highlight compression, shadow lift |
| underwater | +10 | +10 | Color recovery |
| fog | -20 | -10 | Soft, ethereal |
| default | 0 | 0 | Neutral baseline |

### Profile Depth Levels

| Depth | Contains | GPT Override | Use Case |
|-------|----------|--------------|----------|
| minimal | Contrast + Saturation | Yes | Quick processing, let GPT decide |
| medium | +Sharpening, Shadows/Highlights, Vibrance | Yes | Default, good balance |
| full | +Local Contrast, Noise Reduction, Tone Curves | No (scene only) | Max quality, profiles are tuned |

### CLI Usage

```bash
# Nextcloud mode (default): all 3 depths, input from Nextcloud RAW folder
.venv/bin/python3 photo_pipeline.py Barcelona_Trip
.venv/bin/python3 photo_pipeline.py Barcelona_Trip --all-depths

# Single depth only
.venv/bin/python3 photo_pipeline.py Barcelona_Trip --profile-depth full

# Local mode: use local directories (for testing)
.venv/bin/python3 photo_pipeline.py ./RAW/Barcelona_Trip --local

# Manual batch name
.venv/bin/python3 photo_pipeline.py Barcelona_Trip --batch "Barcelona_Vacation"

# Dry run (shows what would be processed)
.venv/bin/python3 photo_pipeline.py Barcelona_Trip --dry-run

# Disable auto-straighten
.venv/bin/python3 photo_pipeline.py Barcelona_Trip --no-straighten

# Disable smart cropping
.venv/bin/python3 photo_pipeline.py Barcelona_Trip --no-crop

# Keep temp files after Nextcloud processing (for debugging)
.venv/bin/python3 photo_pipeline.py Barcelona_Trip --keep-temp

# Selection Pipeline (Phase 2): .pp3 only, no JPEGs
.venv/bin/python3 select_pipeline.py SW-England-May26-01
.venv/bin/python3 select_pipeline.py SW-England-May26-01 --dry-run
.venv/bin/python3 select_pipeline.py SW-England-May26-01 --keep-temp
.venv/bin/python3 select_pipeline.py SW-England-May26-01 --max-images 20
```

### GIMP-Bearbeitungs-Workflow (Metadaten wiederherstellen)

GIMP strippt IPTC/XMP beim JPEG-Export. Lösung: `scripts/reapply_metadata.py`

**Nextcloud mode (default):**
1. Pipeline erstellt JPEG + `_metadata.json` in Nextcloud `output/{batch}/`
2. User lädt JPEG von Nextcloud herunter, bearbeitet in GIMP
3. Bearbeitetes JPEG in Nextcloud `reapply-metadata/` hochladen
4. `.venv/bin/python3 scripts/reapply_metadata.py` ausführen
5. Metadaten werden angewendet, Datei nach Nextcloud `done/{batch}/` hochgeladen
6. Original aus `reapply-metadata/` wird gelöscht

```bash
# Nextcloud mode (default)
.venv/bin/python3 scripts/reapply_metadata.py
.venv/bin/python3 scripts/reapply_metadata.py --batch "2026-05-14_Barcelona"
.venv/bin/python3 scripts/reapply_metadata.py --dry-run

# Local mode
.venv/bin/python3 scripts/reapply_metadata.py --local
```

### Architecture

Two Python scripts:
- `photo_pipeline.py` — Full JPEG pipeline (develop + metadata + upload)
- `select_pipeline.py` — Selection Phase (Phase 2): no JPEGs, only .pp3 + sort selected/rejected

Gemini/GPT Vision called once per image for scene + metadata.

**Photo Pipeline**: Bracket → Select → Quality → Name → Classify → Develop → PostProcess → AutoStraighten+Crop → Metadata → Upload

**Select Pipeline**: Bracket → Select → Quality → Classify → Generate .pp3 → Sort selected/rejected → Upload + Report

**Nextcloud integration** via `NextcloudClient` class (WebDAV):
- `list_dir()` — list files in a Nextcloud directory
- `download_file()` — download file from Nextcloud
- `download_dir()` — download all files from a directory
- `upload_file()` — upload file to Nextcloud (auto-creates parent dirs)
- `mkdir()` — create directory chain on Nextcloud
- `file_exists()` / `delete_file()` — check/delete

Profile generator: `scripts/generate_profiles.py` creates all 30 pp3 profiles from templates.

### Auto-Straighten + Smart Crop (Step 7.5)

After post-processing, before metadata:
1. **Auto-Straighten**: Canny edge + HoughLinesP detects dominant horizontal/vertical lines → sub-degree rotation
2. **Smart Crop**: 
   - Max-rectangle crop after rotation (remove black corners)
   - Entropy-based edge trimming: strips with low entropy (< threshold) are "boring" → trim
   - Saliency protection: edges with high contrast relative to center are "part of the subject" → keep
3. **Stock minimum**: After every crop step, verify ≥ 5MP and ≥ 2500×3500 (Adobe Stock)
4. No rotation limit (any angle detected is applied)

CLI flags:
- `--no-straighten` — Disable auto-straightening
- `--no-crop` — Disable smart cropping

### Stock Minimum Requirements (Adobe Stock)

| Parameter | Value |
|-----------|-------|
| Min megapixels | 5 MP |
| Min width | 2500 px |
| Min height | 3500 px |
| Format | JPEG sRGB |
| Quality | 95% |

### Next Steps

- [x] Create default .pp3 profiles in profiles/
- [x] Add profile depth levels (minimal/medium/full)
- [x] Add --profile-depth CLI flag
- [x] Add batch folder output (date+location subfolders)
- [x] Add GPT override conditional (skip for full depth)
- [x] Update directory structure
- [x] Add auto-straighten (Hough lines) + smart crop (entropy + saliency)
- [x] Add --no-straighten and --no-crop CLI flags
- [x] Update stock minimum to Adobe Stock specs (2500×3500, 5MP)
- [x] Create Nextcloud directory structure
- [x] Upload pp3 profiles + scripts to Nextcloud
- [x] Add NextcloudClient class (WebDAV operations)
- [x] Switch pipeline to Nextcloud-first mode (download/process/upload)
- [x] Update reapply_metadata.py for Nextcloud mode
- [x] Create select_pipeline.py (Phase 1: .pp3 only, no JPEGs)
- [ ] Run select_pipeline.py for SW-England-May26-01 (all 182 CR2)
- [ ] Compare phase_1 auto-selection with user's manual selection
- [ ] Test bracket grouping with real CR2 files
- [ ] Adjust quality gate thresholds
- [ ] Test GPT Vision scene classification
- [ ] Test rawtherapee-cli integration with pp3 profiles
- [ ] Test full end-to-end pipeline with Nextcloud
- [ ] Upload pipeline to GitHub