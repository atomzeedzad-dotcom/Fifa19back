"""Install the FIFA19 roster before the inherited engine initializes its save."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def install(namespace: dict) -> None:
    document = json.loads((ROOT/'data/fifa19-player-definitions.json').read_text(encoding='utf-8'))
    if document.get('gameYear') != 2019:
        raise ValueError('The FIFA19 server requires a FIFA19 roster')
    players = document['players']
    by_asset = {int(x['assetId']): x for x in players}
    namespace['_PLAYER_DEFS'] = players
    namespace['_PLAYER_DEF_MAP'] = by_asset
    namespace['_load_player_defs'] = lambda allow_download=False: players
    namespace['_apply_official_fut_identity'] = lambda rows, download=False: (rows, document['source'])
    # Prevent the FIFA18 historical special-card corrections/catalogue from
    # changing FIFA19 ratings. No World Cup or archived FIFA18 SBC data.
    for name in ('_special_player_defs', '_icon_player_defs', '_exact_special_player_defs'):
        namespace[name] = lambda: []
    namespace['_verified_special_catalog'] = lambda *args, **kwargs: []
    namespace['_apply_exact_card_override'] = lambda row: row
    namespace['_manager_defs'] = lambda refresh=False: []
    namespace['FIFA18_EXACT_CARD_OVERRIDES'] = {}
    used = set()
    chosen = []
    slots = ['GK', 'RB', 'CB', 'CB', 'LB', 'RM', 'CM', 'CM', 'LM', 'ST', 'ST',
             'GK', 'RB', 'CB', 'LB', 'RM', 'CM', 'LM', 'ST', 'GK', 'CB', 'CM', 'ST']
    for position in slots:
        pool = [x for x in players if x['position'] == position and x['rating'] < 65 and x['assetId'] not in used]
        if not pool:
            raise ValueError(f'FIFA19 roster lacks bronze starter position {position}')
        card = sorted(pool, key=lambda x: (-x['rating'], x['assetId']))[0]
        used.add(card['assetId'])
        chosen.append(card)
    starter_rows = [(x['assetId'], x['rating'], x['position'], x['leagueId'], x['teamId'], x['nation'], x['face']) for x in chosen]
    namespace['STARTER_PLAYER_DEFS'] = starter_rows
    starter_items = []
    for index, row in enumerate(starter_rows):
        item = namespace['_starter_item'](index, row)
        item.update(resourceGameYear=2019, name=chosen[index]['name'], rareflag=0, rareFlag=0)
        starter_items.append(item)
    namespace['STARTER_ITEMS'] = starter_items
    namespace['LOAN_ITEMS'] = [namespace['_starter_item'](100+i, row, pile=6, item_id=781000000001+i, loans=7)
                               for i, row in enumerate(starter_rows[:5])]
