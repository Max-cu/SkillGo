# Final workflow automatic delivery

This change only affects the framework. Existing Skill packages, design rules and validators remain unchanged.

Enable `SKILLGO_WORKFLOW_AUTO_DELIVERY_VERSION_IDS` on API and workers with the exact reviewed version UUID(s). Default empty disables it. The first adapter supports the exact existing TCC build/finalize/export recipe (steps 3–5, nine commands). It does not infer task completion from arbitrary successful commands, a workflow name, or a directory containing PPT files. Other workflows and versions continue to require `finish`.

For a single-Skill delivery-only job:

1. Match the approved version and exact command sequence, including all final validators and cleanup.
2. Immediately before the exporter, record the names already in its `exports` directory. Listing errors disable automatic delivery; no directory contents are supplied to the model.
3. From this exporter invocation's successful stdout, require exactly one existing `✅ PPTX exported: <absolute path>` line. The path must be directly inside this Skill's exports directory, end in `.pptx`, and must not have existed before the invocation. No glob or “latest file” selection is used. Capture its bytes through the existing symlink-safe collector and bind size plus SHA256.
4. Wait for every remaining command to succeed. Final verification/cleanup failure or cancellation prevents automatic finish.
5. Recollect the same source and require size and hash to match. Retain the existing plan/Skill finish guards. Return through the existing worker artifact persistence path, which must save the actual file before task success. Record a `workflow_auto_delivery` event; no synthetic model response or `finish` call is generated.

Missing/ambiguous stdout, unsupported recipes, existing-path overwrite, retry starting after the export command, or inability to prove freshness falls back to the ordinary agent. A correctable final-byte mismatch is reported to the agent rather than marked successful. Multi-Skill and full-verification jobs keep the old completion behavior.

The reviewed exporter creates a fresh name when the target exists. Programs that overwrite their previous filename are intentionally unsupported by this adapter. There is no claim that command success proves semantic PPT quality: Skill validators run unchanged.

This saves only the redundant post-workflow model turn. It does not speed up content creation or guarantee a fixed number of seconds saved. The adapter performs metadata listing and byte checks locally, with no additional model calls and no context compression.
