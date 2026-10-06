"""Normal SP Draft offers. Pure state operations: no wallet, inventory or I/O.

The native client still needs to emit choices/player and choose requests. This
module deliberately does not simulate those requests or auto-pick a player.
"""

from copy import deepcopy
import random


class DraftSlotError(ValueError):
    pass


def _integer(value):
    try:
        return int(value or 0)
    except (ValueError, TypeError, OverflowError):
        return 0


def slot_id(value):
    if isinstance(value, bool):
        raise DraftSlotError('invalid slot')
    try:
        result = int(value)
    except (ValueError, TypeError, OverflowError):
        raise DraftSlotError('invalid slot') from None
    if str(result) != str(value).strip() or not 0 <= result < 23:
        raise DraftSlotError('slot must be an integer from 0 to 22')
    return result


def identity(item):
    """Icon versions can have different assets; prohibit the same named Icon."""
    if str(item.get('specialType', '')).upper() == 'ICON':
        name = str(item.get('name', '')).strip().casefold()
        if name:
            return ('icon', name)
    return ('asset', _integer(item.get('assetId')))


def sample_definitions(definitions, picked, allowed_positions=None, count=5, rng=None):
    """Every valid card version is eligible; select distinct players first.

    Do not collapse each player to their highest-rated version or truncate the
    catalog. Position matching applies to each version (e.g. Birthday LB).
    Weight player identities by their best eligible rating, then draw a version
    uniformly. This is a local Draft policy, not a claim about EA's algorithm.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise DraftSlotError('count must be a nonnegative integer')
    rng = rng or random.SystemRandom()
    picked = picked if isinstance(picked, dict) else {}
    used = {identity(x) for x in picked.values() if isinstance(x, dict)}
    positions = [allowed_positions] if isinstance(allowed_positions, str) else (allowed_positions or ())
    allowed = {str(x).strip().upper() for x in positions}
    groups = {}
    for definition in definitions or ():
        if not isinstance(definition, dict) or not 0 < _integer(definition.get('rating')) <= 99:
            continue
        key = identity(definition)
        if _integer(definition.get('assetId')) <= 0 or key in used:
            continue
        if allowed and str(definition.get('position', '')).upper() not in allowed:
            continue
        groups.setdefault(key, []).append(definition)
    keys = list(groups)
    weights = [2.0 ** ((max(_integer(x.get('rating')) for x in groups[key]) - 75) / 5.0)
               for key in keys]
    result = []
    for _ in range(min(count, len(keys))):
        index = rng.choices(range(len(keys)), weights=weights, k=1)[0]
        key = keys.pop(index)
        weights.pop(index)
        result.append(deepcopy(rng.choice(groups[key])))
    return result


def _offers(state):
    """Read an isolated candidate cache; publish only after validation succeeds."""
    offers = state.get('slotOffers')
    formation = str(state.get('formation', ''))
    if not isinstance(offers, dict) or state.get('slotOffersFormation') != formation:
        return {}
    return deepcopy(offers)


def _legacy_offer(state, position, captain):
    """A pending pre-rebuild offer can still be accepted after a restart."""
    kind = 'captain' if captain else 'player'
    if state.get('lastDraftChoiceKind') != kind:
        return None
    if not captain and state.get('lastPositionId') != position:
        return None
    return state.get('lastDraftChoiceItems')


def _valid_items(items, picked):
    if not isinstance(items, list) or not items:
        return False
    used = {identity(x) for x in picked.values() if isinstance(x, dict)}
    seen = set()
    used_ids = {_integer(x.get('id')) for x in picked.values() if isinstance(x, dict)}
    seen_ids = set()
    for item in items:
        if not isinstance(item, dict):
            return False
        asset = item.get('assetId')
        resource = item.get('resourceId', item.get('definitionId', asset))
        if (not isinstance(item, dict) or isinstance(item.get('id'), bool)
                or not isinstance(item.get('id'), int) or not 0 < item['id'] < 2**31
                or isinstance(asset, bool) or not isinstance(asset, int) or asset <= 0
                or isinstance(resource, bool) or not isinstance(resource, int) or resource <= 0
                or str(item.get('itemType', '')).lower() != 'player'
                or _integer(item.get('resourceId', item.get('definitionId', item.get('assetId')))) <= 0):
            return False
        key = identity(item)
        iid = _integer(item.get('id'))
        if (key == ('asset', 0) or key in used or key in seen
                or iid in used_ids or iid in seen_ids):
            return False
        seen.add(key)
        seen_ids.add(iid)
    return True


def offer(state, position, generate, captain=False):
    position = slot_id(position)
    expected = 'CAPTAIN_DRAFT' if captain else 'PLAYER_DRAFT'
    if state.get('draftStage') != expected:
        raise DraftSlotError('choices unavailable at this stage')
    picked = state.get('pickedBySlot')
    picked = picked if isinstance(picked, dict) else {}
    if isinstance(picked.get(str(position), picked.get(position)), dict):
        raise DraftSlotError('slot already occupied')
    offers = _offers(state)
    key = 'captain' if captain else str(position)
    items = offers.get(key)
    if not _valid_items(items, picked):
        # Legacy compatibility is only for states without a modern cache.
        # A modern cache with another formation must never revive old choices.
        items = _legacy_offer(state, position, captain) if 'slotOffers' not in state else None
        if not _valid_items(items, picked):
            items = generate()
        if not _valid_items(items, picked):
            raise DraftSlotError('no eligible player choices')
        offers[key] = deepcopy(items)
    state['slotOffers'] = offers
    state['slotOffersFormation'] = str(state.get('formation', ''))
    return deepcopy(items)


def choose(state, position, index, captain=False):
    position = slot_id(position)
    if captain and position >= 11:
        raise DraftSlotError('captain must occupy a starter slot')
    expected = 'CAPTAIN_DRAFT' if captain else 'PLAYER_DRAFT'
    if state.get('draftStage') != expected:
        raise DraftSlotError('selection unavailable at this stage')
    picked = state.get('pickedBySlot')
    picked = picked if isinstance(picked, dict) else {}
    if isinstance(picked.get(str(position), picked.get(position)), dict):
        raise DraftSlotError('slot already occupied')
    key = 'captain' if captain else str(position)
    # Do not recreate/reset offers on a rejected selection.
    offers = state.get('slotOffers')
    offers = offers if isinstance(offers, dict) else {}
    items = offers.get(key) if state.get('slotOffersFormation') == str(state.get('formation', '')) else None
    if 'slotOffers' not in state:
        items = _legacy_offer(state, position, captain)
    if isinstance(index, bool) or not isinstance(index, int) or not isinstance(items, list) or not 0 <= index < len(items):
        raise DraftSlotError('no offered choice for this slot')
    if not _valid_items([items[index]], picked):
        raise DraftSlotError('stale or duplicate player choice')
    chosen = deepcopy(items[index])
    picked = deepcopy(picked)
    picked[str(position)] = chosen
    if not _valid_items(list(picked.values()), {}):
        raise DraftSlotError('invalid existing picked card metadata')
    try:
        picks = [int(x.get('resourceId', x.get('definitionId', x.get('assetId', 0))) or 0)
                 for x in picked.values() if isinstance(x, dict)]
        if any(x <= 0 for x in picks):
            raise ValueError('invalid resource ID')
    except (ValueError, TypeError, OverflowError):
        raise DraftSlotError('invalid existing picked card metadata') from None
    offers = deepcopy(offers)
    offers.pop(key, None)
    # Build the entire candidate state before publishing any native pick.
    for other_key, other_items in list(offers.items()):
        if not _valid_items(other_items, picked):
            offers.pop(other_key, None)
    state['pickedBySlot'] = picked
    state['picks'] = picks
    if captain:
        state['captainSlot'] = position
    state['draftStage'] = 'PLAYER_DRAFT'
    state['lastPositionId'] = position
    state['slotOffers'] = offers
    state['slotOffersFormation'] = str(state.get('formation', ''))
    return chosen
