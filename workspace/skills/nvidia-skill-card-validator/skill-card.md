# Skill Card — fixed external validator action

## Description / Use Case
Checks a Skill Card Markdown document for unresolved NVIDIA template review markers.

## Owner and source
The wrapper and execution policy are SkillPulse-authored. The validator action
comes from NVIDIA/skills at fixed commit
`d8519c57da6db5d9bea274ec1724a4a7a56a3dee` and path
`skills/skill-card-generator/scripts/validate_submission.py`.
The upstream script's SPDX license is Apache-2.0. This source attribution does
not establish publisher signature verification or NVIDIA endorsement of SkillPulse.

## Requirements / Dependencies
Linux/DGX, Docker, a SHA-256-pinned copy of the upstream script in the local
outbox, and the reviewed empty container image with exact image ID. No model
or GPU is used by this deterministic action.

## Risks & Mitigations
Only a 32 KiB text input is accepted. The script and input are mounted read-only
inside a no-network, non-root, resource-limited container. Script hash and
image ID are checked before each run. This is a bounded prototype, not full
malware containment. `PASS` only means no VERIFY/SELECT template markers
remain; it is not a security, legal, or publication approval.

## Version / human adapter decision
SkillPulse adapter version 0.1.0, reviewed for this fixed action on 2026-09-28
after a one-time isolated DGX evaluation. It does not release the complete
upstream `skill-card-generator` Skill or arbitrary online candidates.
