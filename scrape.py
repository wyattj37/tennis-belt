"""
Daily scraper that proposes belt-match updates to update.py.

Run by GitHub Actions on a daily cron; can also be run locally for testing.
For each tour (ATP, WTA), it:
  1. Reads the current belt holder from {tour}_lineage.json.
  2. Reads the holder's current reign-defense count from {tour}_matches_all.json.
  3. Fetches today's & yesterday's completed matches from ESPN's tennis API.
  4. Finds the singles match (if any) involving the current holder.
  5. Builds the match dict and injects it into update.py's atp_new_matches /
     wta_new_matches list, unless an equivalent dict is already present
     (in update.py or in matches_all.json).

If no match is found for either tour, the script makes no changes (silent).
"""

from __future__ import annotations

import ast
import json
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parent
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/tennis"

ROUND_MAP = {
    "Final": "F",
    "Finals": "F",
    "Semifinals": "SF",
    "Semifinal": "SF",
    "Quarterfinals": "QF",
    "Quarterfinal": "QF",
    "Round of 16": "R16",
    "Round of 32": "R32",
    "Round of 64": "R64",
    "Round of 128": "R128",
    "Round Robin": "RR",
}

# Sizes for the last numbered round before QF, in order: R16, R32, R64, R128.
NUMBERED_ROUND_SIZES = ["R16", "R32", "R64", "R128"]


def build_round_map(event: dict) -> dict[str, str]:
    """ESPN labels main-draw rounds as 'Round 1', 'Round 2', ... whose meaning
    depends on draw size (e.g. in a 32-draw 'Round 2' = R16; in a 96-draw it = R64).
    The last numbered round before QF is always R16, so map relative to the max."""
    nums = set()
    for grp in event.get("groupings", []):
        for comp in grp.get("competitions", []):
            disp = comp.get("round", {}).get("displayName", "")
            if disp.startswith("Round ") and "Qualifying" not in disp:
                tail = disp[len("Round "):].strip()
                if tail.isdigit():
                    nums.add(int(tail))
    if not nums:
        return {}
    max_n = max(nums)
    out: dict[str, str] = {}
    for n in nums:
        idx = max_n - n
        if 0 <= idx < len(NUMBERED_ROUND_SIZES):
            out[f"Round {n}"] = NUMBERED_ROUND_SIZES[idx]
    return out


def extract_ioc(athlete: dict) -> str:
    """ESPN's flag.alt is inconsistent ('USA' vs 'Italy'); the slug in flag.href
    is always a 3-letter IOC code. Use that."""
    href = athlete.get("flag", {}).get("href", "")
    m = re.search(r"/(\w{3})\.png$", href)
    return m.group(1).upper() if m else ""


def norm(s: str) -> str:
    """Loose name comparison: NFKD-strip diacritics, lowercase, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return " ".join(s.lower().split())


def fetch_scoreboard(tour: str, dates: str | None = None) -> dict:
    url = f"{ESPN_BASE}/{tour}/scoreboard"
    if dates:
        url += f"?dates={dates}"
    req = Request(url, headers={"User-Agent": "tennis-belt-scraper/1.0"})
    with urlopen(req, timeout=20) as r:
        return json.load(r)


def get_holder_and_defenses(lineage_path: Path, matches_path: Path) -> tuple[str, int]:
    lineage = json.loads(lineage_path.read_text())
    matches = json.loads(matches_path.read_text())
    holder = sorted(lineage, key=lambda r: r["date_won"], reverse=True)[0]["holder"]
    sorted_matches = sorted(matches, key=lambda r: r["tourney_date"], reverse=True)
    for m in sorted_matches:
        if norm(m["winner_name"]) == norm(holder):
            return holder, int(m["defenses"])
    return holder, 0


def format_set_score(w_set: dict, l_set: dict) -> str:
    w, l = int(w_set["value"]), int(l_set["value"])
    s = f"{w}-{l}"
    # Tiebreak suffix: show the loser-of-the-set's tiebreak point count in parens.
    if w == 7 and l == 6 and "tiebreak" in l_set:
        s += f"({l_set['tiebreak']})"
    elif w == 6 and l == 7 and "tiebreak" in w_set:
        s += f"({w_set['tiebreak']})"
    return s


def format_score(winner: dict, loser: dict) -> str:
    sets = []
    for w_s, l_s in zip(winner.get("linescores", []), loser.get("linescores", [])):
        sets.append(format_set_score(w_s, l_s))
    return " ".join(sets)


def find_belt_match(events: list, holder: str, surfaces: dict, today_iso: str) -> tuple[dict | None, str | None]:
    """Scan events for any singles match involving `holder`.

    Returns (match_dict, note):
      - match_dict: a fully-populated dict if a normal completed match was found.
      - note: a short human-readable description if the holder's match was a
        walkover / retirement / forfeit / cancellation — for surfacing in the PR.
    """
    holder_n = norm(holder)
    match_dict: dict | None = None
    note: str | None = None
    for event in events:
        tourney_name = event.get("name", "")
        event_round_map = build_round_map(event)
        for grp in event.get("groupings", []):
            for comp in grp.get("competitions", []):
                round_disp = comp.get("round", {}).get("displayName", "")
                if "Qualifying" in round_disp:
                    continue
                round_short = ROUND_MAP.get(round_disp) or event_round_map.get(round_disp)
                if not round_short:
                    continue
                status_type = comp.get("status", {}).get("type", {})
                if not status_type.get("completed"):
                    continue
                comp_date = (comp.get("date") or event.get("date", ""))[:10]
                if comp_date < today_iso:
                    today_dt = date.fromisoformat(today_iso)
                    comp_dt = date.fromisoformat(comp_date) if comp_date else None
                    if comp_dt is None or (today_dt - comp_dt).days > 1:
                        continue

                cmps = comp.get("competitors", [])
                if len(cmps) != 2 or any("athlete" not in c for c in cmps):
                    continue
                names_n = [norm(c["athlete"].get("displayName", "")) for c in cmps]
                if holder_n not in names_n:
                    continue

                desc = status_type.get("description", "")
                if any(k in desc for k in ("Walkover", "Retired", "Forfeit", "Canceled")):
                    if note is None:
                        other = next(
                            c["athlete"]["displayName"]
                            for c, n in zip(cmps, names_n) if n != holder_n
                        )
                        note = f"{comp_date} {tourney_name} {round_short}: {holder} vs {other} — {desc}"
                    continue

                if match_dict is not None:
                    continue
                winner = next((c for c in cmps if c.get("winner")), None)
                loser = next((c for c in cmps if not c.get("winner")), None)
                if not winner or not loser:
                    continue

                match_dict = {
                    "tourney_name": tourney_name,
                    "round": round_short,
                    "surface": surfaces.get(tourney_name, ""),
                    "tourney_date": comp_date,
                    "winner_name": winner["athlete"]["displayName"],
                    "winner_ioc": extract_ioc(winner["athlete"]),
                    "loser_name": loser["athlete"]["displayName"],
                    "loser_ioc": extract_ioc(loser["athlete"]),
                    "score": format_score(winner, loser),
                }
    return match_dict, note


def get_existing_list(content: str, var_name: str) -> list:
    """Return the list literal currently assigned to `var_name` in update.py."""
    tree = ast.parse(content)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == var_name:
                return ast.literal_eval(node.value)
    return []


def is_duplicate(match: dict, existing: list) -> bool:
    key = (match["tourney_date"], norm(match["winner_name"]), norm(match["loser_name"]))
    for e in existing:
        ekey = (
            str(e.get("tourney_date", ""))[:10],
            norm(e.get("winner_name", "")),
            norm(e.get("loser_name", "")),
        )
        if ekey == key:
            return True
    return False


def render_dict(match: dict) -> str:
    """Render the match dict in the same style as existing entries in update.py."""
    keys = [
        "tourney_name", "round", "surface", "tourney_date",
        "winner_name", "winner_ioc", "loser_name", "loser_ioc",
        "score", "defenses", "change",
    ]
    lines = ["    {"]
    for i, k in enumerate(keys):
        v = match[k]
        if isinstance(v, str) and "'" not in v:
            rendered = f"'{v}'"
        else:
            rendered = repr(v)
        suffix = "," if i < len(keys) - 1 else ""
        if i == 0:
            lines[0] = f"    {{'{k}': {rendered}{suffix}"
        else:
            lines.append(f"    '{k}': {rendered}{suffix}")
    lines.append("    },")
    return "\n".join(lines)


def inject_into_list(content: str, var_name: str, match: dict) -> str:
    pattern = re.compile(rf"({re.escape(var_name)}\s*=\s*\[)", re.MULTILINE)
    m = pattern.search(content)
    if not m:
        raise ValueError(f"Could not find `{var_name} = [` in update.py")
    rendered = render_dict(match)
    insertion = "\n" + rendered + "\n"
    return content[: m.end()] + insertion + content[m.end():]


def inject_comment(content: str, var_name: str, note: str) -> str:
    """Insert a `# WALKOVER / RETIRED / etc.` comment at the top of the list."""
    pattern = re.compile(rf"({re.escape(var_name)}\s*=\s*\[)", re.MULTILINE)
    m = pattern.search(content)
    if not m:
        raise ValueError(f"Could not find `{var_name} = [` in update.py")
    insertion = f"\n    # NEEDS MANUAL HANDLING: {note}\n"
    return content[: m.end()] + insertion + content[m.end():]


def main() -> int:
    today = date.today()
    today_iso = today.isoformat()
    surfaces = yaml.safe_load((ROOT / "data" / "tournaments.yaml").read_text()) or {}

    update_path = ROOT / "update.py"
    content = update_path.read_text()
    matches_all_paths = {
        "atp": ROOT / "data" / "matches_all.json",
        "wta": ROOT / "data" / "wta_matches_all.json",
    }
    lineage_paths = {
        "atp": ROOT / "data" / "atp_lineage.json",
        "wta": ROOT / "data" / "wta_lineage.json",
    }
    list_vars = {"atp": "atp_new_matches", "wta": "wta_new_matches"}

    summary_lines = []
    for tour in ("atp", "wta"):
        holder, prev_def = get_holder_and_defenses(lineage_paths[tour], matches_all_paths[tour])
        try:
            data = fetch_scoreboard(tour)
        except Exception as e:
            print(f"[{tour}] ESPN fetch failed: {e}", file=sys.stderr)
            continue

        match, note = find_belt_match(data.get("events", []), holder, surfaces, today_iso)

        if note is not None and note not in content:
            content = inject_comment(content, list_vars[tour], note)
            summary_lines.append(
                f"- **{tour.upper()}** ⚠ NEEDS MANUAL HANDLING: {note}"
            )
        elif note is not None:
            print(f"[{tour}] note already in update.py, skipping: {note}")

        if match is None:
            if note is None:
                print(f"[{tour}] no belt match for {holder}")
            continue

        # Dedup against historical matches and pending entries in update.py.
        historical = json.loads(matches_all_paths[tour].read_text())
        if is_duplicate(match, historical):
            print(f"[{tour}] match already in matches_all, skipping")
            continue
        pending = get_existing_list(content, list_vars[tour])
        if is_duplicate(match, pending):
            print(f"[{tour}] match already pending in update.py, skipping")
            continue

        holder_won = norm(match["winner_name"]) == norm(holder)
        match["change"] = "No" if holder_won else "Yes"
        match["defenses"] = (prev_def + 1) if holder_won else 0
        if not match["surface"]:
            print(f"[{tour}] WARNING: no surface known for '{match['tourney_name']}'", file=sys.stderr)

        content = inject_into_list(content, list_vars[tour], match)
        summary_lines.append(
            f"- **{tour.upper()}** ({match['tourney_name']} {match['round']}): "
            f"{match['winner_name']} def. {match['loser_name']} {match['score']} "
            f"— change: {match['change']}, defenses: {match['defenses']}"
            + ("" if match["surface"] else "  ⚠ surface blank — fill in before merging")
        )

    if not summary_lines:
        print("No new belt matches; no changes written.")
        return 0

    update_path.write_text(content)
    (ROOT / "proposal_summary.md").write_text(
        "## Proposed belt match update\n\n" + "\n".join(summary_lines) + "\n"
    )
    print("Wrote proposed update.py and proposal_summary.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
