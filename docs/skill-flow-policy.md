# Follow the Skill workflow

Administrators can toggle **沿用 Skill 既定流程** next to each sandbox Skill version in Skill management. Default off for existing and newly uploaded versions. The framework does not classify arbitrary Skills from headings or ask a model to classify them. Review the complete Skill, including input routing and preparation, before enabling it.

The switch removes the requirement for a separate model-written plan when a task uses just this version. It does not execute stage headings mechanically, rewrite the Skill, remove its references/checks, or decide its input branches. The model still reads the complete approved Skill and user request, adapts choices during ordinary execution, and may call update_plan when it needs one. No generated route card is fabricated; the task timeline records that Skill flow mode is selected, and any voluntarily supplied model plan is displayed normally.

Multiple selected Skills always retain ordinary planning. Final acceptance policy, tool validation, cancellation, sandbox restrictions and automatic export delivery are unchanged. Full-verification mode still requires valid verifier evidence. Pre-plan inspection quotas and their “write a plan next” hints are only active when planning is mandatory.

`PATCH /admin/skill-versions/{id}/skill-flow` accepts `{ "enabled": true }` with administrator authorization and emits an audit entry. The setting is a database policy, never read from a Skill manifest, uploaded document or model output. Non-sandbox versions cannot enable it. It is independent of network permission.

The job copies the policy at creation, including console, workflow API and retry paths using the shared job factory. Changing a version switch does not alter existing or queued jobs. Old snapshots keep their existing plan; restoration uses the job's frozen policy, and does not delete a previously created plan.

## Deployment

Migration `20261022_0009` adds default-false Boolean columns to skill_versions and workflow_jobs. No existing version is automatically opted in. Deploy API, workers, environment worker and frontend together during an idle window. Apply the additive migration once with the new image before starting the new services. Existing rows and files are retained. A code rollback to a release that only knows revision 0008 requires the migration downgrade before starting that release; downgrading removes these policy flags.

The optimization saves mandatory plan generation only when the model follows the new guidance. It makes no fixed latency promise and does not prevent the model from choosing a useful plan for a complex task.
