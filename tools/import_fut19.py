"""Convert the archived FUTBIN 19 CSV; never confuse its row ID with EA ID."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEAGUES = {
    'Premier League': 13, 'LaLiga Santander': 53, 'Serie A TIM': 31,
    'Bundesliga': 19, 'Ligue 1 Conforama': 16, 'Icons': 2118,
    'EFL Championship': 14, 'EFL League One': 60, 'EFL League Two': 61,
    'Liga NOS': 308, 'Eredivisie': 10, 'MLS': 39,
}


def image_id(url: str, family: str) -> int:
    match = re.search(r'/'+re.escape(family)+r'/p?(\d+)\.png(?:\?|$)', url)
    if not match:
        raise ValueError(f'Missing EA {family} ID in image URL: {url!r}')
    return int(match.group(1))


def convert(path: Path) -> dict:
    players = {}
    excluded = 0
    unknown_leagues = set()
    with path.open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            # Import base cards only. Special design/rarity IDs need a FIFA19
            # client schema; a FUTBIN index is never an EA resource ID.
            if row['Revision'] != 'Normal':
                excluded += 1
                continue
            rid = image_id(row['PlayerPic'], 'players')
            if not 0 < rid < 0x1000000:
                raise ValueError(f'Unexpected base resource ID {rid}')
            club = image_id(row['ClubPic'], 'clubs')
            nation = image_id(row['NationPic'], 'nation')
            rating = int(row['Rating'])
            position = row['Position'].upper()
            league = LEAGUES.get(row['League'], 0)
            if not league:
                unknown_leagues.add(row['League'])
            face = [int(row[k]) for k in ('Pace', 'Shooting', 'Passing', 'Dribbling', 'Defending', 'Phyiscality')]
            if not 1 <= rating <= 99 or len(face) != 6 or not all(0 <= v <= 99 for v in face):
                raise ValueError(f'Invalid card: {row["Name"]}')
            players.setdefault(rid, {
                'assetId': rid, 'resourceId': rid, 'definitionId': rid,
                'resourceGameYear': 2019, 'rating': rating, 'position': position,
                'positions': [position], 'leagueId': league, 'teamId': club,
                'nation': nation, 'name': row['Name'], 'clubName': row['Club'],
                'nationality': row['Country'], 'face': face,
                # Archive does not prove base rare/common classification.
                'rareflag': 0, 'rareFlag': 0, 'rarityVerified': False,
                'source': 'kafagy/fifa-FUT-Data/FutBinCards19.csv (Normal)',
            })
    if len(players) < 10000:
        raise ValueError('Incomplete FIFA19 base roster')
    return {
        'schemaVersion': 2, 'gameYear': 2019,
        'source': 'https://github.com/kafagy/fifa-FUT-Data/blob/master/FutBinCards19.csv',
        'sourceSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'limitations': ['Historical partial snapshot; special cards excluded',
                        'Base rarity unverified; uncommon league IDs remain 0',
                        'Client wire schema and cosmetic IDs unverified'],
        'excludedSpecialRows': excluded, 'unknownLeagues': sorted(unknown_leagues),
        'players': list(players.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', nargs='?', type=Path, default=ROOT/'data/FutBinCards19.csv')
    args = parser.parse_args()
    document = convert(args.csv)
    target = ROOT/'data/fifa19-player-definitions.json'
    target.write_text(json.dumps(document, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'Imported {len(document["players"])} FIFA19 base players; {document["excludedSpecialRows"]} special rows excluded')


if __name__ == '__main__':
    main()
