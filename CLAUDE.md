# Suno-

## Agent skills

This repo is configured for [mattpocock/skills](https://github.com/mattpocock/skills).
Installed under `.claude/skills/`; update with `npx skills@latest update`.

### Issue tracker

Issues live in this repo's GitHub Issues, driven through the `gh` CLI (REST fallback
documented for environments where GraphQL is blocked). See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, each label string equal to its name, all present in the tracker.
See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and one `docs/adr/` at the repo root, both created lazily
by `/domain-modeling` rather than upfront. See `docs/agents/domain.md`.
