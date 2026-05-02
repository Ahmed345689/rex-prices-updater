"""Print a detailed summary of a players.json file — validates that club,
nation, league, position, and rating are captured correctly."""
from __future__ import annotations

import json
import sys
from pathlib import Path


PLACEHOLDER_RE = __import__("re").compile(
    r"^(TeamName|LeagueName|NationName|ClubName|Unknown|N/A|null|none)[\W_]",
    __import__("re").IGNORECASE,
)


def is_placeholder(v: str | None) -> bool:
    return not v or bool(PLACEHOLDER_RE.match(v))


def main(path: str) -> int:
    p = Path(path)
    if not p.exists():
        print(f"File not found: {path}")
        return 1
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Could not parse {path}: {e}")
        return 1

    if not isinstance(d, dict) or "players" not in d:
        kind = type(d).__name__
        keys = list(d) if isinstance(d, dict) else "N/A"
        print(f"Unexpected shape: {kind}; keys={keys}")
        return 1

    players = d.get("players") or []
    total = len(players)

    print("=" * 60)
    print(f"  players.json Summary")
    print("=" * 60)
    print(f"  Season          : {d.get('season', 'N/A')}")
    print(f"  Scraped at      : {d.get('scrapedAt', 'N/A')}")
    print(f"  Total players   : {total}")
    print(f"  Responses cap.  : {d.get('responsesCaptured', 'N/A')}")
    print()

    if total == 0:
        print("  No players found.")
        return 0

    # Field coverage counts
    def count_valid(field: str) -> int:
        return sum(
            1 for pl in players
            if isinstance(pl, dict)
            and not is_placeholder(str(pl.get(field) or ""))
            and pl.get(field) is not None
        )

    has_name    = count_valid("name")
    has_price   = count_valid("price")
    has_pos     = count_valid("position")
    has_rating  = count_valid("rating")
    has_club    = count_valid("club")
    has_nation  = count_valid("nation")
    has_league  = count_valid("league")

    def pct(n: int) -> str:
        return f"{n}/{total} ({100*n//total}%)"

    print("  Field coverage:")
    print(f"    name     : {pct(has_name)}")
    print(f"    price    : {pct(has_price)}")
    print(f"    position : {pct(has_pos)}")
    print(f"    rating   : {pct(has_rating)}")
    print(f"    club     : {pct(has_club)}")
    print(f"    nation   : {pct(has_nation)}")
    print(f"    league   : {pct(has_league)}")
    print()

    # Warn on low coverage
    for field, count in [("club", has_club), ("nation", has_nation),
                          ("league", has_league), ("position", has_pos)]:
        if total > 0 and count / total < 0.5:
            print(f"  WARNING: fewer than 50% of players have a valid '{field}'")
            print(f"           This may mean the API does not return this field,")
            print(f"           or the placeholder-detection needs to be updated.")

    print()
    print("  First 5 players:")
    print(f"  {'Name':<25} {'Pos':>4}  {'Rat':>3}  {'Club':<22} {'Nation':<18} {'League':<20}  Price")
    print("  " + "-" * 105)
    for pl in players[:5]:
        if not isinstance(pl, dict):
            print(f"  {pl!r}")
            continue
        name   = str(pl.get("name") or "N/A")[:24]
        pos    = str(pl.get("position") or "-")[:4]
        rat    = str(pl.get("rating") or "-")[:3]
        club   = str(pl.get("club") or "N/A")[:21]
        nation = str(pl.get("nation") or "N/A")[:17]
        league = str(pl.get("league") or "N/A")[:19]
        price  = pl.get("price")
        print(f"  {name:<25} {pos:>4}  {rat:>3}  {club:<22} {nation:<18} {league:<20}  {price}")

    print("=" * 60)
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "players.json"
    sys.exit(main(target))
