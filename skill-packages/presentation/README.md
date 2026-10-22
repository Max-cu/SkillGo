# Presentation compiler overlay

This overlay targets a separately supplied, reviewed presentation package with SHA-256
`46a33c26804316da41a39236d77dbb378e02f7b732f4861e908cbeab3105d86c`.
Vendor templates, icons and validators remain in the original ZIP. Private
business inputs and generated decks are never part of this directory.

Build an uploadable package:

```sh
python skill-packages/presentation/build_package.py original.zip optimized.zip
```

Extract the result into an isolated directory and run its offline regression:

```sh
python -m pytest <extracted-skill>/tests/test_structured_workflow.py -q
```

Normal authoring produces a source-bound JSON plan, then the compiler derives
SVG, design specification and pipeline configuration. The existing company
checks still gate build, finalize and export. Complex pages can mix custom SVG
with compiled pages; other chapter structures retain the manual workflow.
`write_json` avoids putting JSON inside an escaped string. `run_verifier.artifacts`
collects the exact original exports before hash-bound validation.

Publish as a new immutable Skill version; running jobs keep their existing
package. Test a real task before claiming an end-to-end speed improvement.
