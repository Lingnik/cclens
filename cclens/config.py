"""Configuration.

Values come from the mapping passed to `from_env`, never from os.environ
directly, so tests and callers can supply their own environment.

CCLENS_CLAUDE_HOME   roots to index, colon separated, each optionally
                     labelled `name=path`. Default: ~/.claude
CCLENS_DB            index location. Default: $XDG_CACHE_HOME/cclens/index.db
CCLENS_HOST          bind address. Default: 127.0.0.1
CCLENS_PORT          bind port. Default: 8731
CCLENS_FTS_CHARS     per entry cap on indexed search text. Default: 4000
CCLENS_EXCLUDE       source kinds to skip, comma separated.
CCLENS_PROJECT_DIRS  transcript directories to read under each root, comma
                     separated. Default: projects
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

KINDS = ("transcript", "audit", "state", "statusline")
DEFAULT_PROJECT_DIRS = ("projects",)
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class ConfigError(Exception):
    """Configuration is unusable. Raised at boot with the offender named."""


@dataclass(frozen=True)
class Root:
    """One .claude directory. `name` labels the host it belongs to, and
    `project_dirs` names the transcript trees to read inside it, since a host
    may keep more than one."""

    name: str
    path: Path
    project_dirs: tuple[str, ...] = DEFAULT_PROJECT_DIRS


@dataclass(frozen=True)
class Config:
    roots: tuple[Root, ...]
    db_path: Path
    host: str
    port: int
    fts_chars: int
    exclude: frozenset[str]
    project_dirs: tuple[str, ...] = DEFAULT_PROJECT_DIRS

    def enabled(self, kind: str) -> bool:
        return kind not in self.exclude

    @property
    def enabled_kinds(self) -> tuple[str, ...]:
        return tuple(k for k in KINDS if self.enabled(k))

    @property
    def loopback_only(self) -> bool:
        return self.host in LOOPBACK


def _parse_roots(raw: str) -> tuple[Root, ...]:
    roots: list[Root] = []
    seen: dict[str, str] = {}
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, spec = chunk.partition("=")
        if not spec:
            name, spec = "", name
        path = Path(spec).expanduser()
        if not path.is_dir():
            raise ConfigError(f"CCLENS_CLAUDE_HOME: not a directory: {path}")
        real = str(path.resolve())
        if real in seen:
            continue
        seen[real] = real
        roots.append(Root(name=name or _default_name(path, len(roots)), path=path))
    if not roots:
        raise ConfigError("CCLENS_CLAUDE_HOME: no usable roots")
    labels = [r.name for r in roots]
    if len(set(labels)) != len(labels):
        raise ConfigError(f"CCLENS_CLAUDE_HOME: duplicate root names: {labels}")
    return tuple(roots)


def _default_name(path: Path, ordinal: int) -> str:
    """Label a root by the user directory it sits under, which is stable enough
    to tell two hosts apart in the UI."""
    parent = path.expanduser().resolve().parent.name
    return parent or f"root{ordinal}"


def _parse_project_dirs(raw: str) -> tuple[str, ...]:
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    for name in names:
        if "/" in name or name in (".", ".."):
            raise ConfigError(f"CCLENS_PROJECT_DIRS: not a directory name: {name!r}")
    return names or DEFAULT_PROJECT_DIRS


def _parse_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key}: not an integer: {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{key}: must not be negative: {value}")
    return value


def from_env(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())

    roots = _parse_roots(env.get("CCLENS_CLAUDE_HOME") or str(home / ".claude"))

    db_raw = env.get("CCLENS_DB", "").strip()
    if db_raw:
        db_path = Path(db_raw).expanduser()
    else:
        cache = env.get("XDG_CACHE_HOME", "").strip()
        base = Path(cache).expanduser() if cache else home / ".cache"
        db_path = base / "cclens" / "index.db"

    exclude = frozenset(
        k.strip() for k in env.get("CCLENS_EXCLUDE", "").split(",") if k.strip()
    )
    unknown = exclude - set(KINDS)
    if unknown:
        raise ConfigError(f"CCLENS_EXCLUDE: unknown kinds: {sorted(unknown)}")

    project_dirs = _parse_project_dirs(env.get("CCLENS_PROJECT_DIRS", ""))
    roots = tuple(replace(root, project_dirs=project_dirs) for root in roots)

    return Config(
        project_dirs=project_dirs,
        roots=roots,
        db_path=db_path,
        host=env.get("CCLENS_HOST", "").strip() or "127.0.0.1",
        port=_parse_int(env, "CCLENS_PORT", 8731),
        fts_chars=_parse_int(env, "CCLENS_FTS_CHARS", 4000),
        exclude=exclude,
    )


def override(config: Config, **kwargs: object) -> Config:
    """Apply command line overrides on top of the environment."""
    clean = {k: v for k, v in kwargs.items() if v is not None}
    if "roots" in clean and isinstance(clean["roots"], str):
        clean["roots"] = _parse_roots(clean["roots"])
    if "db_path" in clean:
        clean["db_path"] = Path(str(clean["db_path"])).expanduser()
    if "project_dirs" in clean and isinstance(clean["project_dirs"], str):
        clean["project_dirs"] = _parse_project_dirs(clean["project_dirs"])
    if "project_dirs" in clean:
        roots = clean.get("roots", config.roots)
        clean["roots"] = tuple(
            replace(root, project_dirs=clean["project_dirs"]) for root in roots)
    if "exclude" in clean and isinstance(clean["exclude"], str):
        kinds = frozenset(k.strip() for k in clean["exclude"].split(",") if k.strip())
        unknown = kinds - set(KINDS)
        if unknown:
            raise ConfigError(f"--exclude: unknown kinds: {sorted(unknown)}")
        clean["exclude"] = kinds
    return replace(config, **clean)
