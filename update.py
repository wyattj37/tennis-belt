import numpy as np
import pandas as pd
from datetime import date
import subprocess

atp_new_matches = [
    {'tourney_name': 'Wimbledon',
    'round': 'QF',
    'surface': 'Grass',
    'tourney_date': '2026-07-08',
    'winner_name': 'Arthur Fery',
    'winner_ioc': 'GBR',
    'loser_name': 'Flavio Cobolli',
    'loser_ioc': 'ITA',
    'score': '6-4 7-6(4) 6-0',
    'defenses': 2,
    'change': 'No'
    },

]

wta_new_matches = [
    {'tourney_name': 'Wimbledon',
    'round': 'F',
    'surface': 'Grass',
    'tourney_date': '2026-07-11',
    'winner_name': 'Linda Noskova',
    'winner_ioc': 'CZE',
    'loser_name': 'Karolina Muchova',
    'loser_ioc': 'CZE',
    'score': '6-2 5-7 6-3',
    'defenses': 0,
    'change': 'Yes'
    },

    {'tourney_name': 'Wimbledon',
    'round': 'QF',
    'surface': 'Grass',
    'tourney_date': '2026-07-07',
    'winner_name': 'Karolina Muchova',
    'winner_ioc': 'CZE',
    'loser_name': 'Naomi Osaka',
    'loser_ioc': 'JPN',
    'score': '7-6(4) 6-4',
    'defenses': 5,
    'change': 'No'
    },

    {'tourney_name': 'Wimbledon',
    'round': 'SF',
    'surface': 'Grass',
    'tourney_date': '2026-07-09',
    'winner_name': 'Karolina Muchova',
    'winner_ioc': 'CZE',
    'loser_name': 'Coco Gauff',
    'loser_ioc': 'USA',
    'score': '6-2 1-6 7-6(10)',
    'defenses': 6,
    'change': 'No'
    },
]

atp_new_df = pd.DataFrame(atp_new_matches)
wta_new_df = pd.DataFrame(wta_new_matches)

# if there are multiple new matches, make sure they are ordered correctly
for df in [atp_new_df, wta_new_df]:
    if len (df) > 0:
        df['tourney_date'] = pd.to_datetime(df['tourney_date'], format="%Y-%m-%d")
        round_order = ['RR', 'R128', 'R64', 'R32', 'R16', 'QF', 'SF', 'F']
        df['round'] = pd.Categorical(df['round'], categories=round_order, ordered=True)
        df = df.sort_values(by=['tourney_date', 'round'], ascending=[True, False])


# read in old data, combine with new
atp_df = pd.read_json("data/matches_all.json")
wta_df = pd.read_json("data/wta_matches_all.json")

atp_df = pd.concat([atp_new_df[::-1], atp_df], ignore_index=True)
wta_df = pd.concat([wta_new_df[::-1], wta_df], ignore_index=True)

def validate_matches(df, tour):
    dup_cols = ['tourney_name', 'round', 'winner_name', 'loser_name', 'tourney_date']
    dups = df[df.duplicated(subset=dup_cols, keep=False)]
    if not dups.empty:
        raise SystemExit(f"[{tour}] Duplicate match(es) detected:\n{dups[dup_cols + ['defenses']]}")

    # Defenses should follow: change='Yes' -> 0; change='No' -> next_row_defenses + 1
    # (walking from newest [index 0] to oldest).
    for i in range(len(df) - 1):
        row, nxt = df.iloc[i], df.iloc[i + 1]
        if row['change'] == 'Yes':
            # change='Yes' rows may have defenses=0 or be left blank
            if pd.isna(row['defenses']) or row['defenses'] in (0, ''):
                continue
            expected = 0
        elif nxt['change'] == 'Yes':
            expected = 1
        else:
            expected = nxt['defenses'] + 1
        if row['defenses'] != expected:
            raise SystemExit(
                f"[{tour}] Defenses sequence broken at row {i} "
                f"({row['winner_name']} vs {row['loser_name']} @ {row['tourney_name']} {row['round']}): "
                f"defenses={row['defenses']}, expected={expected}. "
                f"Likely a missed match or duplicate."
            )

validate_matches(atp_df, 'ATP')
validate_matches(wta_df, 'WTA')

atp_df.to_json("data/matches_all.json", orient="records")
wta_df.to_json("data/wta_matches_all.json", orient="records")

# do the wta lineage
active = False
wta_lineage_list = []
curr_defenses = 0

for idx, row in wta_df.iterrows():
  if row['change'] == 'No':
    if active:
      continue
    else:
      active = True
      curr_defenses = row['defenses']
  else:
    active = False
    wta_lineage_row = {
        'holder' : row['winner_name'],
        'country' : row['winner_ioc'],
        'won_from' : row['loser_name'],
        'date_won' : row['tourney_date'],
        'tournament_won' : row['tourney_name'],
        'round_won' : row['round'],
        'score' : row['score'],
        'defenses' : curr_defenses,
    }
    wta_lineage_list.append(wta_lineage_row)
    curr_defenses = 0

rosewall_row = {
        'holder' : 'Virginia Wade',
        'country' : 'GBR',
        'won_from' : '---',
        'date_won' : wta_df.iloc[-1]['tourney_date'],
        'tournament_won' : '---',
        'round_won' : '---',
        'score' : '---',
        'defenses' : 0,
    }
wta_lineage_list.append(rosewall_row)
wta_lineage_df = pd.DataFrame(wta_lineage_list)
wta_lineage_df.to_json("data/wta_lineage.json", orient="records")

# do the atp lineage
active = False
atp_lineage_list = []
curr_defenses = 0

for idx, row in atp_df.iterrows():
  if row['change'] == 'No':
    if active:
      continue
    else:
      active = True
      curr_defenses = row['defenses']
  else:
    active = False
    atp_lineage_row = {
        'holder' : row['winner_name'],
        'country' : row['winner_ioc'],
        'won_from' : row['loser_name'],
        'date_won' : row['tourney_date'],
        'tournament_won' : row['tourney_name'],
        'round_won' : row['round'],
        'score' : row['score'],
        'defenses' : curr_defenses,
    }
    atp_lineage_list.append(atp_lineage_row)
    curr_defenses = 0

rosewall_row = {
        'holder' : 'Ken Rosewall',
        'country' : 'AUS',
        'won_from' : '---',
        'date_won' : atp_df.iloc[-1]['tourney_date'],
        'tournament_won' : '---',
        'round_won' : '---',
        'score' : '---',
        'defenses' : 7,
    }
atp_lineage_list.append(rosewall_row)
atp_lineage_df = pd.DataFrame(atp_lineage_list)
atp_lineage_df.to_json("data/atp_lineage.json", orient="records")

# update atp stats
atp_player_stats_df = pd.read_json("data/player_stats.json")
for match in atp_new_matches:
    winner_mask = atp_player_stats_df["winner_name"] == match['winner_name']

    # First-time champion: seed a zeroed row, then fall through to the normal updates.
    if not winner_mask.any():
        new_row = {
            'winner_name': match['winner_name'],
            'defenses': 0,
            'reign_number': 0,
            'W': 0,
            'L': 0,
            'total_matches': 0,
            'win_rate': 0.0,
            'total_defenses': 0,
        }
        atp_player_stats_df = pd.concat([atp_player_stats_df, pd.DataFrame([new_row])], ignore_index=True)
        winner_mask = atp_player_stats_df["winner_name"] == match['winner_name']

    loser_mask = atp_player_stats_df["winner_name"] == match['loser_name']

    atp_player_stats_df.loc[winner_mask, 'W'] += 1
    atp_player_stats_df.loc[loser_mask, 'L'] += 1
    atp_player_stats_df.loc[winner_mask, 'total_matches'] += 1
    atp_player_stats_df.loc[loser_mask, 'total_matches'] += 1
    atp_player_stats_df.loc[winner_mask, 'win_rate'] = atp_player_stats_df.loc[winner_mask, 'W'] / atp_player_stats_df.loc[winner_mask, 'total_matches']
    atp_player_stats_df.loc[loser_mask, 'win_rate'] = atp_player_stats_df.loc[loser_mask, 'W'] / atp_player_stats_df.loc[loser_mask, 'total_matches']

    if match['change'] == 'Yes':
        atp_player_stats_df.loc[winner_mask, 'reign_number'] += 1
    else:
        atp_player_stats_df.loc[winner_mask, 'total_defenses'] += 1

# update wta stats
wta_player_stats_df = pd.read_json("data/wta_player_stats.json")
for match in wta_new_matches:
    winner_mask = wta_player_stats_df["winner_name"] == match['winner_name']

    # First-time champion: seed a zeroed row, then fall through to the normal updates.
    if not winner_mask.any():
        new_row = {
            'winner_name': match['winner_name'],
            'defenses': 0,
            'reign_number': 0,
            'W': 0,
            'L': 0,
            'total_matches': 0,
            'win_rate': 0.0,
            'total_defenses': 0,
        }
        wta_player_stats_df = pd.concat([wta_player_stats_df, pd.DataFrame([new_row])], ignore_index=True)
        winner_mask = wta_player_stats_df["winner_name"] == match['winner_name']

    loser_mask = wta_player_stats_df["winner_name"] == match['loser_name']

    wta_player_stats_df.loc[winner_mask, 'W'] += 1
    wta_player_stats_df.loc[loser_mask, 'L'] += 1
    wta_player_stats_df.loc[winner_mask, 'total_matches'] += 1
    wta_player_stats_df.loc[loser_mask, 'total_matches'] += 1
    wta_player_stats_df.loc[winner_mask, 'win_rate'] = wta_player_stats_df.loc[winner_mask, 'W'] / wta_player_stats_df.loc[winner_mask, 'total_matches']
    wta_player_stats_df.loc[loser_mask, 'win_rate'] = wta_player_stats_df.loc[loser_mask, 'W'] / wta_player_stats_df.loc[loser_mask, 'total_matches']

    if match['change'] == 'Yes':
        wta_player_stats_df.loc[winner_mask, 'reign_number'] += 1
    else:
        wta_player_stats_df.loc[winner_mask, 'total_defenses'] += 1

# Recompute Consec. Defenses (max consecutive defenses in any single reign) from
# the lineage. Preserves seed values that exceed the lineage max (e.g. reigns
# that predate the tracked history).
for stats_df, lineage_df in [(atp_player_stats_df, atp_lineage_df),
                             (wta_player_stats_df, wta_lineage_df)]:
    lineage_max = lineage_df.groupby('holder')['defenses'].max()
    for holder, max_d in lineage_max.items():
        mask = stats_df['winner_name'] == holder
        if mask.any():
            current = int(stats_df.loc[mask, 'defenses'].iloc[0])
            if int(max_d) > current:
                stats_df.loc[mask, 'defenses'] = int(max_d)

atp_player_stats_df.to_json("data/player_stats.json", orient="records")
wta_player_stats_df.to_json("data/wta_player_stats.json", orient="records")

# push updates to live site
def run(cmd):
    subprocess.run(cmd, shell=True, check=True)

# test site
# run("hugo server")

# deploy site
run("hugo -d docs")

# git steps
run("git add .")
run(f'git commit -m "{date.today():%m/%d} update"')
run("git push")
