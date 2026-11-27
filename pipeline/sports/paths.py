"""Per-sport data locations. NBA keeps today's flat layout at the data root
(the existing `config` constants, read at call time so test patches apply);
every other sport lives under its registry `data_prefix` (data/nfl/...)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import get


@dataclass(frozen=True)
class SportPaths:
    root: Path        # where this sport's top-level JSON goes
    state: Path
    slates: Path
    history: Path
    lineups: Path
    cache: Path       # raw downloads, outside data/ (never published)


def paths(sport: str, data_dir: Path | None = None, reg: dict | None = None) -> SportPaths:
    from .. import config
    base = Path(data_dir) if data_dir is not None else Path(config.DATA_DIR)
    prefix = get(sport, reg).get("data_prefix") or ""
    if not prefix:
        if data_dir is None:
            return SportPaths(root=Path(config.DATA_DIR), state=Path(config.STATE_DIR), slates=Path(config.SLATES_DIR),
                              history=Path(config.HISTORY_DIR), lineups=Path(config.DATA_DIR) / "lineups",
                              cache=Path(config.CACHE_DIR) / sport.lower())
        root = base
    else:
        root = base / prefix.strip("/")
    return SportPaths(root=root, state=root / "state", slates=root / "slates", history=root / "history",
                      lineups=root / "lineups", cache=Path(config.CACHE_DIR) / sport.lower())


def data_dir() -> Path:
    from .. import config
    return Path(config.DATA_DIR)
