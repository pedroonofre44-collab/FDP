#!/usr/bin/env python3
"""
Card Trick Game — Railway-ready server
HTTP + WebSocket on a single PORT (Railway sets $PORT automatically).
"""

import asyncio, json, os, random
from aiohttp import web
import aiohttp

# ─── Card logic ───────────────────────────────────────────────────────────────

BASE_ORDER = ['3','2','A','K','Q','J','7','6','5','4']
SUITS      = ['Hearts','Spades','Diamonds','Clubs']
SUIT_SYM   = {'Hearts':'♥','Spades':'♠','Diamonds':'♦','Clubs':'♣'}
# Sequence starts at 2, goes up to 8, down to 2, then repeats forever
# Pattern (one full cycle): 2,3,4,5,6,7,8,7,6,5,4,3
ROUND_CYCLE = [2,3,4,5,6,7,8,7,6,5,4,3]

def round_num_cards(round_idx):
    """Map a round index (0-based) to number of cards. Cycles infinitely."""
    return ROUND_CYCLE[round_idx % len(ROUND_CYCLE)]

def round_display_seq(round_idx, window=20):
    """Return a slice of the infinite sequence for display, centred on current round."""
    start = max(0, round_idx - 4)
    return [{'idx': i, 'nc': round_num_cards(i)} for i in range(start, start + window)]

def build_order(trump_value):
    """
    The card one step BELOW trump in the circular normal order jumps to #1.
    BASE_ORDER is circular: ...7>6>5>4 wraps back to 3>2>A...
    trump=6  → promote 7  → [7, 3, 2, A, K, Q, J, 6, 5, 4]
    trump=3  → promote 4  → [4, 3, 2, A, K, Q, J, 7, 6, 5]
    trump=4  → promote 5  → [5, 3, 2, A, K, Q, J, 7, 6, 4]
    """
    if trump_value not in BASE_ORDER:
        return BASE_ORDER[:]
    idx = BASE_ORDER.index(trump_value)
    # card just weaker than trump in normal order (one index higher, wrapping)
    promote_idx = (idx - 1) % len(BASE_ORDER)
    above = BASE_ORDER[promote_idx]
    rest  = [v for v in BASE_ORDER if v != above]
    return [above] + rest

def card_rank(card, order):
    """Lower = stronger."""
    vi = order.index(card['v']) if card['v'] in order else len(order)
    si = SUITS.index(card['s'])
    return vi * 4 + si

def build_deck():
    return [{'v': v, 's': s} for v in BASE_ORDER for s in SUITS]

def deal_cards(num_cards, players):
    deck = build_deck()
    random.shuffle(deck)
    hands = {p: deck[i*num_cards:(i+1)*num_cards] for i, p in enumerate(players)}
    remaining = deck[len(players)*num_cards:]
    trump_card = remaining[0] if remaining else None
    return hands, trump_card

# ─── Game state ───────────────────────────────────────────────────────────────

class GameState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.phase            = 'lobby'
        self.players          = []
        self.lives            = {}
        self.eliminated       = []
        self.round_idx        = 0
        self.hands            = {}
        self.bids             = {}
        self.tricks_won       = {}
        self.current_trick    = []
        self.last_trick       = []   # kept visible after trick resolves
        self.trick_num        = 0
        self.trick_leader     = 0
        self.bid_order        = []
        self.bid_idx          = 0
        self.trump_card       = None
        self.card_order       = BASE_ORDER[:]
        self.round_results    = []
        self.host             = None
        self.first_bidder_idx = 0    # index into alive_players(), rotates each round

    def alive_players(self):
        return [p for p in self.players if self.lives.get(p, 0) > 0]

    def num_cards(self):
        return round_num_cards(self.round_idx)

    def to_public(self):
        return {
            'phase':         self.phase,
            'players':       self.players,
            'lives':         self.lives,
            'eliminated':    self.eliminated,
            'round_idx':     self.round_idx,
            'round_seq':     round_display_seq(self.round_idx),
            'num_cards':     self.num_cards(),
            'bids':          self.bids,
            'tricks_won':    self.tricks_won,
            'current_trick': self.current_trick,
            'last_trick':    self.last_trick,
            'trick_num':     self.trick_num,
            'trick_leader':  self.trick_leader,
            'bid_idx':       self.bid_idx,
            'bid_order':     self.bid_order,
            'trump_card':    self.trump_card,
            'card_order':    self.card_order,
            'round_results': self.round_results,
            'host':          self.host,
        }

    def to_player(self, name):
        pub = self.to_public()
        pub['my_hand'] = self.hands.get(name, [])
        pub['my_name'] = name
        return pub

G = GameState()
CLIENTS = {}   # name -> ws

# ─── Broadcast helpers ────────────────────────────────────────────────────────

async def broadcast():
    dead = []
    for name, ws in list(CLIENTS.items()):
        try:
            await ws.send_json({'type': 'state', 'state': G.to_player(name)})
        except Exception:
            dead.append(name)
    for name in dead:
        CLIENTS.pop(name, None)

async def send_error(ws, msg):
    try:
        await ws.send_json({'type': 'error', 'msg': msg})
    except Exception:
        pass

# ─── Game logic helpers ───────────────────────────────────────────────────────

def start_round():
    alive = G.alive_players()
    nc    = G.num_cards()
    G.hands, G.trump_card = deal_cards(nc, alive)
    G.card_order   = build_order(G.trump_card['v']) if G.trump_card else BASE_ORDER[:]
    for p in alive:
        G.hands[p].sort(key=lambda c: -card_rank(c, G.card_order))
    G.bids          = {}
    G.tricks_won    = {p: 0 for p in alive}
    # Rotate bid order: first_bidder_idx cycles through alive players each round
    n = len(alive)
    start = G.first_bidder_idx % n
    G.bid_order     = alive[start:] + alive[:start]
    G.bid_idx       = 0
    G.current_trick = []
    G.last_trick    = []
    G.trick_num     = 0
    G.trick_leader  = start   # trick also starts from the same player
    G.round_results = []
    G.phase         = 'bidding'

def next_trick():
    G.last_trick    = []
    G.current_trick = []
    G.trick_num    += 1

def resolve_trick():
    winner_entry = min(G.current_trick, key=lambda e: card_rank(e['card'], G.card_order))
    winner       = winner_entry['player']
    G.tricks_won[winner] += 1
    alive = G.alive_players()
    G.trick_leader = alive.index(winner)
    G.last_trick   = list(G.current_trick)   # save for display
    if G.trick_num >= G.num_cards():
        results = []
        for p in alive:
            bid  = G.bids.get(p, 0)
            won  = G.tricks_won[p]
            diff = abs(bid - won)
            G.lives[p] = max(0, G.lives[p] - diff)
            if G.lives[p] == 0 and p not in G.eliminated:
                G.eliminated.append(p)
            results.append({'player': p, 'bid': bid, 'won': won, 'diff': diff, 'lives': G.lives[p]})
        G.round_results = results
        G.phase = 'round_result'
    else:
        next_trick()

# ─── WebSocket handler ────────────────────────────────────────────────────────

async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    player_name = None
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                action = data.get('action')

                # JOIN
                if action == 'join':
                    name = str(data.get('name', '')).strip()[:20]
                    if not name:
                        await send_error(ws, 'Enter a name.'); continue
                    if name in CLIENTS and CLIENTS[name] is not ws:
                        await send_error(ws, 'Name already taken.'); continue
                    if G.phase != 'lobby' and name not in G.players:
                        await send_error(ws, 'Game already started.'); continue
                    if len(G.players) >= 7 and name not in G.players:
                        await send_error(ws, 'Room full (max 7).'); continue
                    player_name = name
                    CLIENTS[name] = ws
                    if name not in G.players:
                        G.players.append(name)
                        G.lives[name] = 5
                    if G.host is None:
                        G.host = name
                    await broadcast()

                # START
                elif action == 'start':
                    if player_name != G.host:
                        await send_error(ws, 'Only the host can start.'); continue
                    if len(G.players) < 1:
                        await send_error(ws, 'Need at least 1 player.'); continue
                    G.round_idx = 0
                    # Random first bidder for round 1
                    G.first_bidder_idx = random.randrange(len(G.alive_players()))
                    start_round()
                    await broadcast()

                # BID
                elif action == 'bid':
                    if G.phase != 'bidding': continue
                    alive = G.alive_players()
                    if G.bid_idx >= len(alive): continue
                    current_bidder = G.bid_order[G.bid_idx]
                    if player_name != current_bidder:
                        await send_error(ws, "Not your turn to bid."); continue
                    try:
                        bid = int(data['bid'])
                    except Exception:
                        await send_error(ws, 'Invalid bid.'); continue
                    nc = G.num_cards()
                    if bid < 0 or bid > nc:
                        await send_error(ws, f'Bid must be 0–{nc}.'); continue
                    # Only last bidder is restricted: total bids cannot equal nc
                    is_last_bidder = (G.bid_idx == len(alive) - 1)
                    if is_last_bidder:
                        current_sum = sum(G.bids.values()) + bid
                        if current_sum == nc:
                            forbidden = nc - sum(G.bids.values())
                            await send_error(ws, f'As last bidder you cannot bid {forbidden} — total would equal {nc}. Pick a different number.'); continue
                    G.bids[current_bidder] = bid
                    G.bid_idx += 1
                    if G.bid_idx >= len(alive):
                        G.phase = 'playing'
                        G.trick_num = 0
                        next_trick()
                    await broadcast()

                # PLAY CARD
                elif action == 'play_card':
                    if G.phase != 'playing': continue
                    alive = G.alive_players()
                    turn_idx = (G.trick_leader + len(G.current_trick)) % len(alive)
                    expected = alive[turn_idx]
                    if player_name != expected:
                        await send_error(ws, "Not your turn."); continue
                    hand = G.hands.get(player_name, [])
                    ci = data.get('card_idx')
                    if ci is None or not (0 <= int(ci) < len(hand)):
                        await send_error(ws, 'Invalid card.'); continue
                    card = hand.pop(int(ci))
                    G.current_trick.append({'player': player_name, 'card': card})
                    if len(G.current_trick) == len(alive):
                        resolve_trick()
                    await broadcast()

                # NEXT ROUND
                elif action == 'next_round':
                    if player_name != G.host: continue
                    if G.phase == 'round_result':
                        G.round_idx += 1
                        # Advance first_bidder for next round
                        G.first_bidder_idx = (G.first_bidder_idx + 1) % len(G.alive_players())
                        alive = G.alive_players()
                        if len(alive) <= 1:
                            G.phase = 'game_over'
                        else:
                            start_round()
                        await broadcast()

                # RESTART
                elif action == 'restart':
                    if player_name != G.host: continue
                    names = list(CLIENTS.keys())
                    G.reset()
                    G.players = names
                    G.lives   = {n: 5 for n in names}
                    G.host    = names[0] if names else None
                    await broadcast()

            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    except Exception:
        pass
    finally:
        if player_name:
            CLIENTS.pop(player_name, None)
            if G.phase == 'lobby':
                G.players = [p for p in G.players if p != player_name]
                G.lives.pop(player_name, None)
                if G.host == player_name:
                    G.host = G.players[0] if G.players else None
        await broadcast()

    return ws

# ─── HTML ─────────────────────────────────────────────────────────────────────
