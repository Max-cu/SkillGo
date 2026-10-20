# SVG Icon Library

**8,352 SVG icons** across four stylistic libraries, embedded directly into generated SVG. Choose **at most one** library per deck from the four, guided by the deck's visual needs. Upstream versions, licenses, and attribution: [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md).

| Library | Style | Count | viewBox | Prefix |
|---------|-------|-------|---------|--------|
| `chunk-filled` | fill · compact, chunky 16px silhouettes; heavy, solid | 641 | primarily `0 0 16 16` | `chunk-filled/` |
| `tabler-filled` | fill · bezier forms, smooth rounded contours; medium, approachable | 1,055 | `0 0 24 24` | `tabler-filled/` |
| `tabler-outline` | stroke / line art (default stroke-width 2); light, refined; best on screen — thin strokes weaken when printed or projected | 5,138 | `0 0 24 24` | `tabler-outline/` |
| `phosphor-duotone` | duotone · full-opacity shape plus a same-color 20% backplate; medium, layered | 1,518 | `0 0 256 256` | `phosphor-duotone/` |

> 🚫 **`simple-icons` (brand logos) has been removed from this library.**
> Company / product marks carry their own trademark and licensing boundaries, which this project does not take on.
> When a deck needs brand recognition, express it with **text plus a generic icon** — do not paste logos.

## Per-project icons folder

This directory is the global library; the resource owner copies chosen icons into `<project>/icons/<lib>/` before SVG authoring:

```bash
python3 scripts/icon_sync.py . tabler-outline/home tabler-outline/bulb
```

Missing names, or one batch mixing two stylistic libraries, exit non-zero. Files under `<project>/icons/` form the prepared pool and combine freely with user-provided, custom, or imported icons; `finalize_svg.py --only embed-icons`, preview, validation, and native export resolve only this pool. **Custom icons**: drop `.svg` files into `<project>/icons/<lib>/` (any `<lib>`, e.g. `custom/`) and reference `data-icon="<lib>/<name>"`. **Imported vectors**: `imported/` is reserved — one copy at `<project>/icons/imported/<name>.svg`, referenced as `data-icon="imported/<name>"`, never duplicated and never used as a hand-curated library.

> In this skill the project root is the skill root, so `icon_sync.py`'s `<project_path>` is `.`.

## Usage

```xml
<use data-icon="tabler-outline/home" x="100" y="200" width="48" height="48" fill="#0076A8"/>
<use data-icon="chunk-filled/home" x="100" y="200" width="48" height="48" fill="#0076A8"/>
<use data-icon="phosphor-duotone/house" x="100" y="200" width="48" height="48" fill="#0076A8"/>
```

`data-icon` is `<library>/<icon-name>` (filename without `.svg`), case-sensitive because it resolves a real file — bundled names are canonical lowercase (`tabler-outline/award`), custom icons keep their file case; `x`, `y` position; `width`, `height` size (32–48px recommended on a 1280-wide canvas; scale with canvas width); `fill` color. A complete `library/name` identifier is mandatory: bare names, abbreviated namespaces, paths into `templates/icons/`, and unsynced bundled files fail rather than fall back. `finalize_svg.py` embeds every placeholder during post-processing (`scripts/svg_finalize/embed_icons.py svg_output/*.svg` runs it manually).

## Searching for Icons

For a known basename run `icon_sync.py` directly; for an uncertain one search only the chosen library:

```bash
rg --files "assets/icons/tabler-outline" -g '*chart*.svg'
rg --files "assets/icons/chunk-filled" -g '*home*.svg'
```

**Hard rule**: search by the drawable object, not the abstract concept — these libraries store things that can be drawn (`bulb`, `target`, `trending-up`, `alert-triangle`), so `idea`, `goal`, `growth`, `warning`, `innovation` return nothing in most of them; translate the semantic into an object first. **Reference — not a constraint**: one concept usually has several valid objects, chosen per deck from page register and visual style. **Hard rule**: basenames are not portable across the four libraries — `alert-*` exists in the tabler libraries but `phosphor-duotone` uses `warning-*`; `arrow-trend-*` in `chunk-filled` is `trending-*` in `tabler-outline`. Do not load a full index or enumerate broad keyword families; re-pick from the narrow result and rerun the batch until clean; never switch libraries for a missing generic icon. An empty result → try another drawable translation in the same library; when several stay empty, let another carrier (chart, typography, shape) take that semantic rather than forcing a loose icon.

## Style Rules

**No default library — choose from the deck's visual needs** after reading the source. Three questions settle it:

1. **Screen or paper?** Printed / projected output → **avoid `tabler-outline`** (thin strokes weaken).
2. **How much visual weight?** light `tabler-outline` → medium `tabler-filled` / `phosphor-duotone` → heavy `chunk-filled`.
3. **Layering?** Want a "full but not solid" feel → `phosphor-duotone`.

Visual weight runs heavy solid (`chunk-filled`) → medium solid (`tabler-filled`) → medium layered (`phosphor-duotone`) → light stroke (`tabler-outline`). **At most one stylistic library per deck selection**: when it lacks an exact icon, use the closest alternative within it rather than another library — a catalog-selection rule, not a ban on combining assets already in the project's `icons/`.
