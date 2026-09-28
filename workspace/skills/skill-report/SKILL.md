---
name: skill-report
description: Use when a local SkillPulse health result needs a readable evidence report.
license: Apache-2.0
---
# Skill Report

Turn the JSON text returned by `skill-doctor` into a concise Markdown report.

## Run

Connect `skill-doctor.result` to `skill-report.result`. The input must be a
structured SkillPulse report, not arbitrary natural language. This Skill only
formats already measured results; it does not grant compliance certification.

## Output

Markdown with the Skill name, actual five checks, risk, and explicit limitations.
