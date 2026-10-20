# Parallel subtasks

`SKILLGO_PARALLEL_SUBTASKS_ENABLED` defaults to false. The main model can opt
to split one planned job into independent child loops; children cannot spawn
more children. They share the job's sandbox and do not occupy another worker
slot. Each child gets only its instruction and declared file paths, not the
parent conversation. After return, the parent must perform cross-task checks.

## File and checker boundaries

Child `write_file` accepts only that child's declared output files. The runtime
walks directory descriptors without following symlinks and rejects hardlinked
targets before truncation. Overlapping output allowlists are rejected.

An optional `validation_argv` must match exactly. It runs in a short-lived
checker container with the same image, configured sandbox runtime and resource
limits, a **read-only** job workspace, no network, no capabilities, and private
bounded `/tmp`. Checkers cannot modify any workspace file, including their own
outputs; corrections must use `write_file` before checking again. Dependencies
already installed under `/workspace/deps` remain readable. Build/format commands
which write in place are not valid checkers; use a read-only check mode.

The checker is forcibly removed after success, failure, timeout or cancellation.
It has job/deployment labels for ordinary resource cleanup. If isolated checking
or guarded writes are unavailable, the tool fails instead of using writable
ordinary `command`/`write_text`. Checks with exit code zero bind the current
output content, which must still match at `finish_task`.

## Model request leases

Migration `20261020_0009` adds deployment-local gate and lease tables. Per-job
concurrency defaults to 5; the deployment-wide in-flight request cap defaults
to 10. All workers in a deployment must use the same cap. Separate deployments
using one model server must keep their combined configured caps at or below 20
until load testing establishes another limit.

A lease refresh which fails, times out, or reports a missing lease cancels the
in-flight model operation and awaits its cleanup. The child returns
`MODEL_LEASE_LOST`; siblings continue. Parent cancellation also waits for request
cleanup. Slots are released before retry backoff. Only the affected failed
subtasks should be retried by the parent.

## Opt-in Compose configuration and validation

The ordinary Compose stack keeps existing defaults. To pass experimental flags
explicitly into API and Worker containers, add the optional
`deploy/compose.agent-experiments.yaml` file after `compose.yaml`. All features
in that overlay default off and orchestration defaults to `audit`.

Run backend tests, then set `SKILLGO_TEST_DOCKER_ISOLATION=1` and run
`backend/tests/test_subtask_isolation_runtime.py -k docker` with pytest. The
second suite uses actual read-only Docker mounts to test writes, deletion,
rename, file/directory symlink aliases and hardlink aliases. It needs Docker
and the pinned Python base image; normal unit tests do not pull images.

Deploy only into the selected idle test deployment before enabling a flag.
Record its code SHA, original image IDs, configuration, Skill version, model
options, instruction and attachment hashes. Compare the same PPT input with
all features off and one feature at a time; include real completion, output
quality, elapsed time, actual model usage and estimated context tokens. Keep
production configuration unchanged until these checks pass.
