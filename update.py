import numpy as np
import pandas as pd
from datetime import date
import subprocess

atp_new_matches = [
    {'tourney_name': 'Australian Open',
    'round': 'R128',
    'surface': 'Hard',
    'tourney_date': '2026-01-18',
    'winner_name': 'Jannik Sinner',
    'winner_ioc': 'ITA',
    'loser_name': 'Hugo Gaston',
    'loser_ioc': 'FRE',
    'score': '6-2 6-1 RET',
    'defenses': 6,
    'change': 'No'}
]

wta_new_matches = [
    {'tourney_name': 'Brisbane International',
    'round': 'R128',
    'surface': 'Hard',
    'tourney_date': '2026-01-18',
    'winner_name': 'Aryna Sabalenka',
    'winner_ioc': 'BEL',
    'loser_name': 'Tiantsoa Sarah Rakotomanga Rajaonah',
    'loser_ioc': 'FRE',
    'score': '6-4 6-1',
    'defenses' : 2,
    'change': 'No'}
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

atp_player_stats_df.to_json("data/player_stats.json", orient="records")
wta_player_stats_df.to_json("data/wta_player_stats.json", orient="records")

# push updates to live site
def run(cmd):
    subprocess.run(cmd, shell=True, check=True)

# test site
run("hugo server")

# deploy site
# run("hugo -d docs")

# # git steps
# run("git add .")
# run(f'git commit -m "{date.today():%m/%d} update"')
# run("git push")
