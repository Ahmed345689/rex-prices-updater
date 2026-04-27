"""Print a short summary of a players.json file. Defensive against
missing keys, wrong shape, or unparseable content."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(path: str) -> int:
    p = Path(path)
    if not p.exists():
        print(f"File not found: {path}")
        return 1
    try:
        d = json.loads(p.read_text())
    except Exception as e:
        print(f"Could not parse {path}: {e}")
        return 1

    if not isinstance(d, dict) or "players" not in d:
        kind = type(d).__name__
        keys = list(d) if isinstance(d, dict) else "N/A"
        print(f"Unexpected shape: {kind}; keys={keys}")
        return 1

    print(f"totalPlayers={d.get('totalPlayers')} "
          f"pagesScraped={d.get('pagesScraped')} "
          f"scrapedAt={d.get('scrapedAt')}")
    print("first 3:")
    for player in (d.get("players") or [])[:3]:
        if isinstance(player, dict):
            print(f"  {player.get('name')} -> {player.get('price')}")
        else:
            print(f"  {player!r}")
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "players.json"
    sys.exit(main(target))
