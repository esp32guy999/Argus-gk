# Personas

Voice files, independent of the serving model. Argus (default) is `soul.md`.
Anything else is `<id>.md` in this directory.

- First `# heading` is the picker label.
- Hot-read: next turn picks up edits; no restart.
- Model capability notes stay in `soul.d/` (e.g. Loki-the-build has no shell).
  Do not put “you cannot run commands” in a persona unless every model should
  obey it.

`loki.md` ships as the first extra voice. Add more by dropping a file, or use
**Tools → Persona** (form writes one markdown file; voice only, no tools).