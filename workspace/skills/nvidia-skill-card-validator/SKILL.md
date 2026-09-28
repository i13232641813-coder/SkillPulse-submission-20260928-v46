---
name: nvidia-skill-card-validator
description: Validate a Skill Card for unresolved review markers using one fixed upstream NVIDIA script action in a bounded DGX container.
license: Apache-2.0
---

# Curated external action adapter

This is a SkillPulse-authored adapter for **one script action** from NVIDIA's
`skill-card-generator` repository. It is not the complete upstream Skill and
does not generate cards, discover assets, verify a publisher signature, or
certify a Skill's safety or legal compliance.

The fixed source is `NVIDIA/skills` commit
`d8519c57da6db5d9bea274ec1724a4a7a56a3dee`, path
`skills/skill-card-generator/scripts/validate_submission.py`, SHA-256
`27df79512570edc4965830a74c4082a5604d89f7fac0ec48c7230fb5fa7cdfb9`.
The upstream file declares Apache-2.0. SkillPulse does not redistribute that
file in this source package: the explicit setup command fetches it from the
fixed GitHub commit and checks its hash before storing it in the local outbox.

The project owner authorized evaluation of this fixed action on the assigned
DGX node on 2026-09-28. Its one-time manual smoke evaluation is recorded in
`evidence/external-skill-action-20260928.json`. The adapter below is a
separate product integration and must be retested after deployment.

## Input and output

- `card` (text, required): Markdown text, maximum 32 KiB.
- `validation` (text): `PASS` if no unresolved VERIFY/SELECT markers remain.
- `source_commit` (string): the pinned Git commit.

The action is available only when the pinned script and the exact reviewed
container image are present on Linux/DGX. The container receives no network
or credentials and uses read-only mounts, a non-root UID, a timeout and CPU,
memory and process limits. This is a bounded prototype, not a production
security sandbox. An unprepared Windows installation marks this Skill
unhealthy and blocks download and execution.
