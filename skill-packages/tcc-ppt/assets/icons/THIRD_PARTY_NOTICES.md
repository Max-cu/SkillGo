# Third-Party Icon Notices

The SVG files under this directory include third-party assets. PPT Master's
MIT license covers PPT Master itself; it does not replace the licenses,
attribution requirements, brand guidelines, or trademark rights that apply to
these assets.

## Bundled snapshots

Snapshot date: **2026-08-09**.

| Directory | Upstream baseline | Bundled files | Compatibility boundary |
|---|---|---:|---|
| `chunk-filled` | [CHUNK Icons](https://www.figma.com/community/file/1327310800295849271/chunk-icons), versionless snapshot cross-checked against the [SVG Repo collection](https://www.svgrepo.com/collection/chunk-16px-thick-interface-icons/) and [Wikimedia Commons category](https://commons.wikimedia.org/wiki/Category:Chunk_Icons) | 641 | PPT Master-normalized files; historical local basenames remain stable |
| `tabler-filled` | [Tabler Icons v3.46.0](https://github.com/tabler/tabler-icons/releases/tag/v3.46.0), commit `8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc` | 1,055 | 1,054 upstream files plus 1 legacy spelling alias |
| `tabler-outline` | [Tabler Icons v3.46.0](https://github.com/tabler/tabler-icons/releases/tag/v3.46.0), commit `8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc` | 5,138 | 5,130 upstream files plus 8 legacy spelling aliases |
| `phosphor-duotone` | [`@phosphor-icons/core@2.1.1`](https://www.npmjs.com/package/@phosphor-icons/core/v/2.1.1), npm integrity `sha512-v4ARvrip4qBCImOE5rmPUylOEK4iiED9ZyKjcvzuezqMaiRASCHKcRIuvvxL/twvLpkfnEODCOJp5dM4eZilxQ==` | 1,518 | 1,512 upstream files plus 6 legacy aliases; upstream `-duotone` filename suffixes are removed locally |

> **Removed 2026-09-30**: the `simple-icons` library (3,675 brand logos,
> Simple Icons 16.28.0) was deleted from this bundle. Brand marks carry
> trademark and usage-guideline constraints that this project does not take
> on; decks needing brand recognition express it with text plus a generic
> icon instead. The corresponding notice section was removed from this file.

The Tabler compatibility aliases are `mood-confuzed` in `tabler-filled`, plus
`brand-adobe-after-effect`, `brand-kako-talk`, `currency-rubel`,
`gender-trasvesti`, `ikosaedr`, `mood-confuzed`, `physotherapist`, and
`sport-billard` in `tabler-outline`.

The Phosphor compatibility aliases are `archive-box`, `archive-tray`,
`folder-notch`, `folder-notch-minus`, `folder-notch-open`, and
`folder-notch-plus`. Their current upstream equivalents are respectively
`box-arrow-down`, `tray-arrow-down`, `folder`, `folder-minus`, `folder-open`,
and `folder-plus`.

## CHUNK Icons attribution

**CHUNK Icons** by **Noah Jacobus** is licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

PPT Master modifications: root dimensions were normalized to 24 by 24,
hard-coded black fills were changed to `currentColor`, SVG Repo wrapper
metadata was removed, and historical local filenames were retained. The icon
geometry remains sourced from CHUNK. The `r` glyph was restored from the
public Wikimedia mirror during the 2026-08-09 reconciliation.

## MIT notices

The following copyright notices and MIT terms apply to the Tabler and
Phosphor assets described above.

### Tabler Icons

Copyright (c) 2020-2026 Paweł Kuna

### Phosphor Icons

Copyright (c) 2023 Phosphor Icons

### MIT License terms

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Update policy

- Pin a released upstream version, or record the audit date for a versionless
  source, before copying files.
- Overwrite files that still exist upstream, but do not automatically delete
  local-only names. Review removals and renames as compatibility changes.
- Recount the directories and validate every SVG after each refresh. Update
  this notice and the README table in the same change.
