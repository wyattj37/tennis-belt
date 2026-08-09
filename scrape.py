"""
Daily scraper that proposes belt-match updates to update.py.

Run by GitHub Actions on a daily cron; can also be run locally for testing
(use --dry-run to print the proposal without touching update.py).

Source: Tennis Abstract (tennisabstract.com). ESPN's public scoreboard API,
which this script used previously, now returns HTTP 403 to every client.

For each tour (ATP, WTA), it:
  1. Reads the current belt holder from {tour}_lineage.json.
  2. Reads the holder's reign-defense count, plus the opponent/round of their
     most recent recorded win, from {tour}_matches_all.json.
  3. Finds the tournaments currently running from Tennis Abstract's home page.
  4. Parses each tournament's completed singles results and collects every
     match involving the holder that comes *after* their last recorded win
     (matches earlier in the same draw pre-date the reign and aren't belt matches).
  5. Injects the not-yet-recorded ones into update.py's atp_new_matches /
     wta_new_matches list, stopping after the holder loses: the belt has changed
     hands, and the new holder's later matches belong to the next run.

Tennis Abstract publishes no per-match dates, so `tourney_date` is left blank
for manual fill-in when verifying the proposal. Blank dates mean duplicate
detection keys on (round, winner, loser) plus the tournament year.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import ssl
import sys
import unicodedata
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parent
TA_BASE = "https://www.tennisabstract.com"

# Draw order, oldest round first. Mirrors update.py's round_order.
ROUND_ORDER = ["RR", "R128", "R64", "R32", "R16", "QF", "SF", "F"]
# Number of players contesting a round -> that round's label.
SIZE_TO_ROUND = {128: "R128", 64: "R64", 32: "R32", 16: "R16"}
# Round labels Tennis Abstract writes out literally rather than numbering.
NAMED_ROUNDS = {"QF", "SF", "F", "RR"}

# Links to live tournaments on the TA home page, e.g. current/2026WTAMontreal.html
EVENT_LINK_RE = re.compile(r"current/(\d{4})(ATP|WTA)([A-Za-z0-9_-]+)\.html")

# A completed singles result, after tag-stripping. For example:
#   R3: (25)Alexandra Eala (PHI) d. Caty Mcnally (USA) 63 57 64
RESULT_RE = re.compile(
    r"^(?P<round>[A-Za-z0-9]+):\s*"
    r"(?:\((?P<wseed>[^)]*)\))?\s*(?P<winner>.+?)\s*\((?P<wioc>[A-Za-z]{3})\)\s*"
    r"d\.\s*"
    r"(?:\((?P<lseed>[^)]*)\))?\s*(?P<loser>.+?)\s*\((?P<lioc>[A-Za-z]{3})\)\s*"
    r"(?P<score>.*)$"
)
# An unplayed slot, e.g. "R1: (2)Elena Rybakina (KAZ) bye"
BYE_RE = re.compile(r"^(?P<round>[A-Za-z0-9]+):.*\bbye\b\s*$", re.I)
# A scheduled match, e.g. "R4: (25)Alexandra Eala (PHI) vs (12)Belinda Bencic (SUI)"
UPCOMING_RE = re.compile(r"^(?P<round>[A-Za-z0-9]+):.*\svs\s", re.I)

# Score annotations meaning the match didn't finish normally and needs a human.
# ATP pages write "WO", WTA pages "W/O"; retirements trail the partial score.
INCOMPLETE_RE = re.compile(r"\b(w/?o|walkover|ret|retired|def|abandoned|unfinished)\b", re.I)


def norm(s: str) -> str:
    """Loose name comparison: strip diacritics and punctuation, lowercase.

    TA is inconsistent about apostrophes and hyphens between pages
    ("Christopher Oconnell" vs "Christopher O'Connell"), so drop them rather
    than treat the two spellings as different players.
    """
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"['`.]", "", s).replace("-", " ")
    return " ".join(s.lower().split())


def fetch(url: str) -> str:
    """GET a page, letting urlopen build its own TLS context.

    Do not pass `context=` on the happy path. Tennis Abstract sits behind
    Cloudflare, which 403s a caller-supplied context from a datacenter IP
    (reproduced 3/3 on a GitHub runner, where the same request without the
    kwarg returns 200) — the handshake differs subtly from the one
    http.client builds. The certifi fallback exists only for python.org
    builds on macOS, which ship without root certificates.
    """
    req = Request(url, headers={"User-Agent": "tennis-belt-scraper/2.0"})
    try:
        with urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except URLError as e:
        if not isinstance(e.reason, ssl.SSLCertVerificationError):
            raise
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        with urlopen(req, timeout=30, context=ctx) as r:
            return r.read().decode("utf-8", errors="replace")


def find_current_events() -> dict[str, list[tuple[str, str, str]]]:
    """Scrape the TA home page for live tournaments.

    Returns {tour: [(year, city, url), ...]}.
    """
    html = fetch(TA_BASE + "/")
    events: dict[str, list[tuple[str, str, str]]] = {"atp": [], "wta": []}
    seen: set[str] = set()
    for year, tour, city in EVENT_LINK_RE.findall(html):
        slug = f"{year}{tour}{city}"
        if slug in seen:
            continue
        seen.add(slug)
        url = f"{TA_BASE}/current/{slug}.html"
        events[tour.lower()].append((year, city, url))
    return events


def extract_js_string(html: str, var: str) -> str:
    """Pull the value of a `var x = '...'` assignment out of the page."""
    m = re.search(re.escape(var) + r"\s*=\s*'(.*?)';", html, re.S)
    return m.group(1).replace("\\'", "'") if m else ""


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    # Head-to-head hints like "[1-1]" trail some rows; they aren't part of the result.
    s = re.sub(r"\[[^\]]*\]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def split_rows(js_string: str) -> list[str]:
    rows = re.split(r"<br\s*/?>", js_string)
    return [r for r in (strip_tags(x) for x in rows) if r]


def next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p *= 2
    return p


def draw_size(rows: list[str]) -> int:
    """Infer the draw size by counting everyone who appeared in round 1.

    Byes contribute one player, played matches two. Counting entrants rather
    than the highest round number seen keeps the mapping stable from day one,
    before the later rounds exist.
    """
    players = 0
    for row in rows:
        head = row.split(":", 1)[0].strip().upper()
        if head != "R1":
            continue
        if BYE_RE.match(row):
            players += 1
        elif RESULT_RE.match(row) or UPCOMING_RE.match(row):
            players += 2
    return next_pow2(players) if players else 0


def round_label(token: str, draw: int) -> str | None:
    """Map a TA round token to the project's label, or None to skip the row.

    TA renders two formats. Some pages label rounds by size already
    ('R32', 'QF'); others number the main draw from 1, so 'R3' means R32 in a
    96-draw but R16 in a 48-draw — resolve those against the draw size, where
    round n is contested by draw / 2**(n-1) players. Qualifying rounds ('Q1')
    return None: they aren't main-draw belt matches.
    """
    t = token.upper()
    if t in NAMED_ROUNDS or t in SIZE_TO_ROUND.values():
        return t
    m = re.fullmatch(r"R([1-9])", t)      # sequential rounds only ever reach R7
    if not m or not draw:
        return None
    return SIZE_TO_ROUND.get(draw // (2 ** (int(m.group(1)) - 1)))


def convert_score(raw: str) -> str | None:
    """Normalise a TA score to '6-3 5-7 6-4' / '7-6(5) 6-1'.

    Handles both renderings: already-hyphenated ('6-3 7-6(8)') and the compact
    form that concatenates the game counts ('63 76(8)'). Either way the parens
    hold the loser's tiebreak points, which is what update.py already stores.
    Returns None on anything unrecognised so the caller can flag it.
    """
    sets = []
    for tok in raw.split():
        m = re.fullmatch(r"(\d+)-(\d+)(?:\((\d+)\))?", tok)
        if not m:
            m = re.fullmatch(r"(\d+)(?:\((\d+)\))?", tok)
            if not m:
                return None
            digits, tb = m.group(1), m.group(2)
            if len(digits) == 2:
                a, b = digits[0], digits[1]
            elif len(digits) == 3 and int(digits[:2]) >= 10:
                a, b = digits[:2], digits[2]      # 10-8
            elif len(digits) == 4:
                a, b = digits[:2], digits[2:]     # 12-10
            else:
                return None
        else:
            a, b, tb = m.group(1), m.group(2), m.group(3)
        s = f"{int(a)}-{int(b)}"
        if tb:
            s += f"({tb})"
        sets.append(s)
    return " ".join(sets) if sets else None


def parse_result(row: str, draw: int) -> dict | None:
    """Turn one completed-singles row into a partial match dict.

    `round` is None when the token can't be resolved (qualifying, or a
    sequential label with no known draw size); callers decide whether that
    row mattered.
    """
    m = RESULT_RE.match(row)
    if not m:
        return None
    return {
        "round": round_label(m.group("round"), draw),
        "round_token": m.group("round"),
        "winner_name": m.group("winner").strip(),
        "winner_ioc": m.group("wioc").upper(),
        "loser_name": m.group("loser").strip(),
        "loser_ioc": m.group("lioc").upper(),
        "raw_score": m.group("score").strip(),
    }


def holder_matches(html: str, holder: str) -> tuple[list[dict], list[str]]:
    """Every completed singles match involving `holder`, in draw order.

    Returns (matches, unresolved). `unresolved` holds rows the holder played
    whose round couldn't be mapped — never drop those silently, since a missed
    belt match is worse than a noisy one.
    """
    completed = split_rows(extract_js_string(html, "completedSingles"))
    upcoming = split_rows(extract_js_string(html, "upcomingSingles"))
    draw = draw_size(completed + upcoming)
    holder_n = norm(holder)

    found, unresolved = [], []
    for row in completed:
        parsed = parse_result(row, draw)
        if not parsed:
            continue
        if holder_n not in (norm(parsed["winner_name"]), norm(parsed["loser_name"])):
            continue
        if parsed["round"] is None:
            if not parsed["round_token"].upper().startswith("Q"):   # qualifying isn't a belt match
                unresolved.append(row)
            continue
        found.append(parsed)
    found.sort(key=lambda p: ROUND_ORDER.index(p["round"]))
    return found, unresolved


def get_holder_state(lineage_path: Path, matches_path: Path) -> tuple[str, int, str, str]:
    """Return (holder, defenses, last_win_opponent, last_win_round).

    The last recorded win anchors the scan: anything earlier in the same draw
    happened before the reign started and is not a belt match.
    """
    lineage = json.loads(lineage_path.read_text())
    matches = json.loads(matches_path.read_text())
    holder = sorted(lineage, key=lambda r: r["date_won"], reverse=True)[0]["holder"]
    for m in sorted(matches, key=lambda r: r["tourney_date"], reverse=True):
        if norm(m["winner_name"]) == norm(holder):
            return holder, int(m["defenses"]), m.get("loser_name", ""), m.get("round", "")
    return holder, 0, "", ""


def get_existing_list(content: str, var_name: str) -> list:
    """Return the list literal currently assigned to `var_name` in update.py."""
    tree = ast.parse(content)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == var_name:
                return ast.literal_eval(node.value)
    return []


def is_duplicate(match: dict, existing: list, year: str) -> bool:
    """Match on (round, winner, loser), scoped to the tournament year.

    Without dates from TA this is the strongest available key; the year keeps
    a repeat of the same pairing and round in a later season from being
    swallowed as a duplicate.
    """
    key = (match["round"], norm(match["winner_name"]), norm(match["loser_name"]))
    for e in existing:
        ekey = (
            str(e.get("round", "")),
            norm(e.get("winner_name", "")),
            norm(e.get("loser_name", "")),
        )
        if ekey != key:
            continue
        edate = str(e.get("tourney_date", ""))
        if edate and year and not edate.startswith(year):
            continue
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


def append_to_list(content: str, var_name: str, match: dict) -> str:
    """Add a match at the end of the list literal.

    update.py reverses these before concatenating onto the newest-first history,
    so the literal has to stay oldest-first — new matches belong at the bottom.
    """
    tree = ast.parse(content)
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == var_name):
            continue
        if not isinstance(node.value, ast.List):
            break
        lines = content.splitlines(keepends=True)
        end = sum(len(l) for l in lines[: node.value.end_lineno - 1]) + node.value.end_col_offset
        close = content.rindex("]", 0, end)
        before, after = content[:close].rstrip(), content[close:]
        if before.endswith("}"):
            before += ","      # previous entry lacked a trailing comma
        return before + "\n" + render_dict(match) + "\n" + after
    raise ValueError(f"Could not find `{var_name} = [...]` in update.py")


def inject_comment(content: str, var_name: str, note: str) -> str:
    """Insert a `# NEEDS MANUAL HANDLING` comment at the top of the list."""
    pattern = re.compile(rf"({re.escape(var_name)}\s*=\s*\[)", re.MULTILINE)
    m = pattern.search(content)
    if not m:
        raise ValueError(f"Could not find `{var_name} = [` in update.py")
    insertion = f"\n    # NEEDS MANUAL HANDLING: {note}\n"
    return content[: m.end()] + insertion + content[m.end():]


def build_name_map(matches: list, stats: list) -> dict[str, str]:
    """norm(name) -> the spelling this project already uses.

    Tennis Abstract's casing differs from ours ("Caty Mcnally" vs "Caty McNally"),
    and update.py joins player_stats on the exact string, so a stray spelling
    silently drops a win or loss. Stats spellings win, since that's the join key.
    """
    name_map: dict[str, str] = {}
    for row in matches:
        for k in ("winner_name", "loser_name"):
            n = row.get(k)
            if n:
                name_map.setdefault(norm(n), n)
    for row in stats:
        n = row.get("winner_name")
        if n:
            name_map[norm(n)] = n
    return name_map


def canonical(name: str, name_map: dict[str, str]) -> str:
    return name_map.get(norm(name), name)


def lookup_surface(surfaces: dict, names: list[str]) -> str:
    for n in names:
        if n and n in surfaces:
            return surfaces[n]
    return ""


def collect_new_matches(
    events: list[tuple[str, str, str]],
    holder: str,
    anchor_opp: str,
    anchor_round: str,
    surfaces: dict,
    tourney_names: dict,
    name_map: dict[str, str],
) -> tuple[list[dict], list[str]]:
    """Gather the holder's unrecorded belt matches across the live tournaments."""
    holder_n = norm(holder)
    new_matches: list[dict] = []
    notes: list[str] = []

    for year, city, url in events:
        try:
            html = fetch(url)
        except Exception as e:
            print(f"  fetch failed for {url}: {e}", file=sys.stderr)
            continue

        found, unresolved = holder_matches(html, holder)
        if not found and not unresolved:
            continue

        slug = f"{year}{city}"
        name = tourney_names.get(slug) or tourney_names.get(city) or city
        surface = lookup_surface(surfaces, [name, city, f"{year} {city}"])

        for row in unresolved:
            notes.append(f"{name}: could not resolve round for '{row}'")

        # Drop anything at or before the holder's last recorded win: those
        # rounds pre-date the reign. If that win isn't in this draw, the reign
        # started elsewhere and every match here is a belt match.
        start = 0
        if anchor_opp and anchor_round in ROUND_ORDER:
            for i, p in enumerate(found):
                if (norm(p["winner_name"]) == holder_n
                        and norm(p["loser_name"]) == norm(anchor_opp)
                        and p["round"] == anchor_round):
                    start = i + 1
                    break

        for p in found[start:]:
            other = p["loser_name"] if norm(p["winner_name"]) == holder_n else p["winner_name"]
            if INCOMPLETE_RE.search(p["raw_score"]):
                notes.append(f"{name} {p['round']}: {holder} vs {other} — {p['raw_score']}")
                break
            score = convert_score(p["raw_score"])
            if score is None:
                notes.append(
                    f"{name} {p['round']}: {holder} vs {other} — unparsed score "
                    f"'{p['raw_score']}'"
                )
                break
            new_matches.append({
                "tourney_name": name,
                "round": p["round"],
                "surface": surface,
                "tourney_date": "",
                "winner_name": canonical(p["winner_name"], name_map),
                "winner_ioc": p["winner_ioc"],
                "loser_name": canonical(p["loser_name"], name_map),
                "loser_ioc": p["loser_ioc"],
                "score": score,
                "_year": year,
            })
            # The holder just lost: the belt moves, so stop here.
            if norm(p["winner_name"]) != holder_n:
                break

    return new_matches, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the proposal without writing update.py")
    args = ap.parse_args()

    surfaces = yaml.safe_load((ROOT / "data" / "tournaments.yaml").read_text()) or {}
    names_path = ROOT / "data" / "ta_tournaments.yaml"
    tourney_names = (yaml.safe_load(names_path.read_text()) or {}) if names_path.exists() else {}

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
    stats_paths = {
        "atp": ROOT / "data" / "player_stats.json",
        "wta": ROOT / "data" / "wta_player_stats.json",
    }
    list_vars = {"atp": "atp_new_matches", "wta": "wta_new_matches"}

    try:
        live = find_current_events()
    except Exception as e:
        print(f"Tennis Abstract home page fetch failed: {e}", file=sys.stderr)
        return 1

    summary_lines: list[str] = []
    for tour in ("atp", "wta"):
        holder, prev_def, anchor_opp, anchor_round = get_holder_state(
            lineage_paths[tour], matches_all_paths[tour]
        )
        events = live.get(tour, [])
        print(f"[{tour}] holder={holder} (defenses={prev_def}), "
              f"live events: {', '.join(c for _, c, _ in events) or 'none'}")
        if not events:
            continue

        historical = json.loads(matches_all_paths[tour].read_text())
        stats = json.loads(stats_paths[tour].read_text())
        name_map = build_name_map(historical, stats)

        found, notes = collect_new_matches(
            events, holder, anchor_opp, anchor_round, surfaces, tourney_names, name_map
        )

        for note in notes:
            if note in content:
                print(f"[{tour}] note already in update.py, skipping: {note}")
                continue
            content = inject_comment(content, list_vars[tour], note)
            summary_lines.append(f"- **{tour.upper()}** ⚠ NEEDS MANUAL HANDLING: {note}")

        pending = get_existing_list(content, list_vars[tour])

        # Pending entries are newer than anything in matches_all, so a holder win
        # sitting in update.py carries the current defense count.
        for e in pending:
            if norm(e.get("winner_name", "")) == norm(holder):
                prev_def = int(e.get("defenses") or 0)

        staged: list[dict] = []
        for match in found:
            year = match.pop("_year")
            if is_duplicate(match, historical, year) or is_duplicate(match, pending, year):
                print(f"[{tour}] already recorded, skipping: "
                      f"{match['winner_name']} d. {match['loser_name']} ({match['round']})")
                continue
            holder_won = norm(match["winner_name"]) == norm(holder)
            match["change"] = "No" if holder_won else "Yes"
            match["defenses"] = (prev_def + 1) if holder_won else 0
            prev_def = match["defenses"]
            staged.append(match)

            flags = []
            if not match["surface"]:
                flags.append("⚠ surface blank")
                print(f"[{tour}] WARNING: no surface known for "
                      f"'{match['tourney_name']}'", file=sys.stderr)
            flags.append("⚠ date blank — fill in before merging")
            summary_lines.append(
                f"- **{tour.upper()}** ({match['tourney_name']} {match['round']}): "
                f"{match['winner_name']} def. {match['loser_name']} {match['score']} "
                f"— change: {match['change']}, defenses: {match['defenses']}"
                + ("  " + ", ".join(flags) if flags else "")
            )

        for match in staged:
            content = append_to_list(content, list_vars[tour], match)

    if not summary_lines:
        print("No new belt matches; no changes written.")
        return 0

    if args.dry_run:
        print("\n--- DRY RUN, update.py not written ---")
        print("\n".join(summary_lines))
        return 0

    update_path.write_text(content)
    (ROOT / "proposal_summary.md").write_text(
        "## Proposed belt match update\n\n" + "\n".join(summary_lines) + "\n"
    )
    print("Wrote proposed update.py and proposal_summary.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
