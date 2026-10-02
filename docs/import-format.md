# Import Format

- Document class: interface reference

`knowitall2 import <file>` reads memories from another system. The file is
JSON Lines in UTF-8: one JSON object per line, blank lines ignored.

```json
{"text": "Frigate is at https://frigate.example.test.", "kind": "fact", "subjects": ["Frigate"], "created_at": "2026-08-14T10:00:00Z", "origin": "old:42"}
{"text": "Always keep router backups in /srv/backups.", "kind": "rule", "source": "user"}
{"text": "Releases are cut from main.", "kind": "decision", "scope": "project", "project": {"path": "C:\\Projects\\app", "remote": "https://git.example.test/team/app.git"}}
```

## Fields

- `text` (required): one self-contained statement, at most 2000 characters.
- `kind`: `fact` (default), `procedure`, `decision`, `lesson`, `rule`, or
  `note`.
- `subjects`, `tags`: lists of short labels, at most 8 each.
- `scope`: `global` (default) or `project`.
- `project`: for `scope: project`, an object with any of:
  - `path`: a local working copy. It is used when it exists, so the memory
    joins that copy's project as its current Git remote identifies it;
  - `remote`: the project's Git remote URL, used when there is no local copy;
  - `name`: a display name for a project known only by its remote.
- `source`: `inferred` (default), `observed`, or `user`. Use `user` only for
  the user's own words; a `rule` requires it.
- `created_at`: when the memory was first recorded, in ISO 8601. It defaults to
  the import time and must not be in the future.
- `origin`: a stable id from the source system.
- `conflicts_with`: origins of other lines in the same file that disagree with
  this one. Each pair becomes a question for the user.

## Behavior

- Every line passes the same checks as `remember`. A line with a secret, tool
  markup, or a rule without the user's words is rejected; the rest continue.
- A line whose text is already stored in the same scope confirms that memory
  instead of adding a copy, so importing the same file twice is harmless.
- `--dry-run` runs the whole import and rolls it back, reporting each line's
  outcome. `--label` names the source, which is shown in each memory's
  provenance as `import:<label>`.
- After an import, background maintenance reviews the changed groups as
  usual, merging duplicates of what was already known.
