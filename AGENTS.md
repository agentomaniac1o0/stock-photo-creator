# Stock Photo Creator — AGENTS.md

## Project: stock-photo-creator

Automated pipeline for processing vacation photos (CR2/CR3) into stock-ready JPEGs.

### Key Decisions (2026-05-14)

| Decision | Choice |
|----------|--------|
| RAW format | Canon CR2/CR3, 3-shot AEB |
| Bracket selection | EXIF-based grouping (timestamp ±2s, different EV) |
| RAW development | rawtherapee-cli + scene-specific .pp3 profiles |
| Quality filter | Rejected images → /rejected/ (not deleted) |
| GIMP step v1 | Omitted — direct RT → Stock |
| Metadata | Gemini/GPT Vision: title + description + 40 keywords + scene + contrast/saturation |
| Batch mode | Full folder at once |
| pp3 profiles | Scene-specific (landscape, architecture, portrait, street, food, macro, night, fog, default) |
| File naming | `<Location>_<YYYYMMDD>_<NNN>.jpg` |
| Location source | GPS reverse geocoding (Nominatim), fallback: Gemini Vision |
| Artist | dynamixx |
| Copyright | Real name (configurable) |
| Source tag | Photo (not "AI Generated") |
| Nextcloud path | /Photos/StockFotoCreator/output/ |
| Keyword logic | Inherited from metabatch.py: 40 keywords, 24 singles / 16 phrases |

### Architecture

Single Python script (`photo_pipeline.py`), no multi-agent system. 
Gemini/GPT Vision called once per image for scene + metadata.
Linear pipeline: Bracket → Select → Quality → Name → Classify → Develop → PostProcess → Metadata → Upload

### Original metabatch.py

Located at Nextcloud: /Photos/StockFotoCreator/metabatch.py
Functions used:
- `trim_to_n_with_mix()` — keyword mix enforcement (24 singles / 16 phrases)
- `make_title()` / `make_description()` — title/description formatting
- `normalize_location()` — location cleanup
- `dedupe_preserve_order()` — keyword deduplication
- GPT Vision prompt (adapted for scene + contrast/saturation)

### Next Steps (Prototype, 2026-05-15)

- [ ] Create default .pp3 profiles in profiles/
- [ ] Test bracket grouping with real CR2 files
- [ ] Adjust quality gate thresholds
- [ ] Test GPT Vision scene classification
- [ ] Test rawtherapee-cli integration
- [ ] Test full end-to-end pipeline
- [ ] Upload pipeline to GitHub