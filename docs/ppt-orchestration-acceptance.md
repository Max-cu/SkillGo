# Export verification and structured PPT authoring

## Failure addressed

When a Skill exported outside `/workspace/output`, `run_verifier` could execute
successfully against an empty delivery snapshot. `finish` then collected the
real output but rejected completion; a no-progress guard prevented immediate
reverification. The observed job added a meaningless write and delivered two
copies of the same PPTX.

`run_verifier.artifacts` now collects explicitly declared original exports
before validation and retry checks. Changed collected bytes count as real
progress; identical bytes preserve validation. The verifier must leave both
original exports and delivery copies unchanged. `finish` accepts the original
paths without another write or copy. Original/output aliases resolve to one
delivery path; separate intentional files are not deduplicated by content.
Legacy Skills can still validate files already under `/workspace/output`.
Missing artifacts produce a preparation diagnostic, rather than a fake
verification success or another consumed correction cycle.

`write_json` serializes finite JSON objects as UTF-8 and uses the existing
`write_file` permissions, size limits and execution path. It is optional; old
Skills and models retain `write_file`.

## Skill changes

The maintained overlay in `skill-packages/presentation` preserves company assets and
all existing validators. Model authoring is concentrated in content selection
and a structured page plan. Deterministic layout, wrapping and SVG generation
run locally, with precise overflow errors and unchanged-page preservation.
Eight text/data layout families, image/text and custom fragments are covered.
The full manual workflow remains available. No platform context truncation,
checkpoint frequency or verification exemptions were introduced.

## Evidence and limits

- Backend: the full pre-integration suite passed 673 tests locally; the added
  Skill-stage integration also passes. One descriptor-based Linux copy test is
  skipped on Windows and must pass in Linux CI. The positive integration test
  completes export collection → verification → finish without dummy writes.
- Skill: six synthetic offline regression tests cover layouts, idempotence,
  overflow atomicity, source requirements, image/custom contracts, old input
  timestamps, DOCX packaging and preservation of over 8,000 bundled icons.
- Independent authoring from the actual product report produced 15 slides.
  Initial errors required only three named content-field corrections. Build,
  finalize, export and original checks passed. PowerPoint visual inspection of
  all eight content slides found no clipping, overlaps or unreadable text;
  sampled factual statements retained source dates, values and qualifications.
- The plan's serialized content count was 6,206 characters vs 24,680 SVG
  characters. A replay of the corrected fixed pipeline took 33.82 seconds
  locally, including 0.85-second compile and 21.73-second export. This excludes
  model authoring and is **not an end-to-end production speed measurement**.
- Local DOCX extraction lacked `mammoth`; evaluation used a complete OOXML text
  extraction verified against all source text runs. Production dependencies
  must still meet the existing Skill runtime requirements. `svglib` was also
  absent locally but native export succeeded. Two accepted density notes are
  retained in compiler diagnostics. The vendor PPTX has 16:9 dimensions with
  a `screen4x3` type attribute; this preexisting metadata remains unchanged.

## Rollout

Deploy platform changes only when workflow jobs, agent runs and job containers
are idle. Preserve separate API/Worker base images, back up source and image
tags, close API admission and recheck idle before replacing Workers. Publish
the ZIP as a new Skill version; never mutate a package used by a running job.
Compare a new run's model time, tool time, rounds, compilation corrections and
delivery attempts against the observed 58m54s / 39-round baseline. Quality and
source fidelity must pass before treating lower latency as an improvement.
