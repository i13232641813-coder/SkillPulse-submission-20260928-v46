# Skill Card

## Description / Use Case
Deterministic local Skill health and trust checks for an existing workspace directory.
## Owner
SkillPulse
## License / Deployment Geography
Apache-2.0
## Requirements / Dependencies
SkillPulse local governance checker; no external API or model.
## Risks & Mitigations
Read-only checks. Existing smoke cases may execute a trusted local runner; do not use
this route for unknown uploaded code. SHA-256 allowlist matching is not a digital signature.
## References / Version / Ethical
0.2.0; local demo implementation, not an NVIDIA-published Skill.

