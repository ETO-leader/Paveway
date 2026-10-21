# Project conventions

- Use GPT as the user-visible assistant name. The agent should identify itself as GPT or use the first person, never as a coach or a coaching title.
- Preserve files named `event.md` when changing display names. The runtime system instruction applies the naming convention without rewriting those files or user-authored prompts/Skills.
- Keep runtime user settings, custom Skill bodies, conversations, credentials, proxy values and session snapshots out of this source repository and its history. The separate local user-data Git archive is intentional; only push it when the user has configured its dedicated remote.
- `.codex/skills/SKILL.md`, bundled templates and project documentation are versioned project instructions. Do not place private user information or credentials in them.
