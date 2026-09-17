# cclens

Read `README.md` first: it carries the design intent, the data model, the API
and the configuration reference. This file adds only what an agent needs.

- Stdlib only at runtime. A new dependency needs a stated reason in the PR.
- A new log source is one `Source` in `cclens/sources.py`. That module's
  docstring is the whole contract. Nothing else should learn to tell sources
  apart.
- `python3 -m pytest` runs on synthetic fixtures. Never point a test at a real
  `.claude` directory.
- No em-dashes in code, comments, docs or commit messages.
