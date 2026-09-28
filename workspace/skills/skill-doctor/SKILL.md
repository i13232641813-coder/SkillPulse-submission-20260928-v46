---
name: skill-doctor
description: Use when: user asks to check, verify, repair, or audit a skill.
license: Apache-2.0
---
# Skill Doctor

Run five local governance checks for a Skill already present in this demo's workspace.

## Run

Pass the exact local Skill directory name in `query`, for example `rag-blueprint`.
The runner calls the same local governance checker as SkillPulse. It does not accept paths,
remote packages, or self-check requests. Unhealthy targets are reported rather than executed.

## Output
Return a JSON text report containing actual check results and their limits. The Signed
check is only a repository-local SHA-256 allowlist comparison, not publisher authentication.

