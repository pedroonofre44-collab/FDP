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
ROUND_SEQ  = [1,2,3,4,5,6,7,8,7,6,5,4,3,2,1]

def build_order(trump_value):
    """
    Face-up card shifts order: the card one step stronger than trump
    jumps to #1. trump stays in its normal position.
    Example: trump=6  →  [7, 3, 2, A, K, Q, J, 6, 5, 4]
    """
    if trump_value not in BASE_ORDER:
        return BASE_ORDER[:]
    idx = BASE_ORDER.index(trump_value)
    if idx == 0:
        return BASE_ORDER[:]            # 3 is already strongest
    above = BASE_ORDER[idx - 1]
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
        self.phase         = 'lobby'
        self.players       = []
        self.lives         = {}
        self.eliminated    = []
        self.round_idx     = 0
        self.hands         = {}
        self.bids          = {}
        self.tricks_won    = {}
        self.current_trick = []
        self.trick_num     = 0
        self.trick_leader  = 0
        self.bid_order     = []
        self.bid_idx       = 0
        self.trump_card    = None
        self.card_order    = BASE_ORDER[:]
        self.round_results = []
        self.host          = None

    def alive_players(self):
        return [p for p in self.players if self.lives.get(p, 0) > 0]

    def num_cards(self):
        return ROUND_SEQ[self.round_idx]

    def to_public(self):
        return {
            'phase':         self.phase,
            'players':       self.players,
            'lives':         self.lives,
            'eliminated':    self.eliminated,
            'round_idx':     self.round_idx,
            'round_seq':     ROUND_SEQ,
            'num_cards':     self.num_cards() if self.round_idx < len(ROUND_SEQ) else 0,
            'bids':          self.bids,
            'tricks_won':    self.tricks_won,
            'current_trick': self.current_trick,
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
    G.bid_order     = alive[:]
    G.bid_idx       = 0
    G.current_trick = []
    G.trick_num     = 0
    G.trick_leader  = 0
    G.round_results = []
    G.phase         = 'bidding'

def next_trick():
    G.current_trick = []
    G.trick_num    += 1

def resolve_trick():
    winner_entry = min(G.current_trick, key=lambda e: card_rank(e['card'], G.card_order))
    winner       = winner_entry['player']
    G.tricks_won[winner] += 1
    alive = G.alive_players()
    G.trick_leader = alive.index(winner)
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
                    if len(G.players) < 2:
                        await send_error(ws, 'Need at least 2 players.'); continue
                    G.round_idx = 0
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
                    if bid == nc:
                        await send_error(ws, f'Bid cannot be exactly {nc}.'); continue
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
                        alive = G.alive_players()
                        if len(alive) <= 1 or G.round_idx >= len(ROUND_SEQ):
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

# ─── HTML (single file, inlined) ─────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Card Trick Game</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600;700&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0f0f0f;--surface:#1a1a1a;--surface2:#222;--surface3:#2a2a2a;
  --border:#333;--border2:#444;
  --text:#f0ece4;--muted:#888;--dim:#555;
  --red:#c0392b;--red-light:#e74c3c;--red-glow:rgba(192,57,43,0.15);
  --gold:#c9a84c;--gold-light:#f0c060;--gold-glow:rgba(201,168,76,0.15);
  --green:#27ae60;--green-light:#2ecc71;
  --blue:#2980b9;--blue-light:#3498db;
  --heart:#e74c3c;--spade:#f0ece4;--diamond:#3498db;--club:#2ecc71;
  --r:10px;--r2:6px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh;font-size:15px}
h1,h2,h3{font-family:'Playfair Display',serif}
h1{font-size:2rem;font-weight:700;color:var(--gold-light)}
h2{font-size:1.25rem;font-weight:600;margin-bottom:.75rem}
h3{font-size:1rem;font-weight:600;margin-bottom:.5rem}
#app{max-width:900px;margin:0 auto;padding:1.5rem 1rem}
.screen{display:none}.screen.active{display:block}
.lobby-wrap{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem;margin-top:1.5rem}
@media(max-width:600px){.lobby-wrap{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:1.25rem}
.panel h3{color:var(--gold);font-family:'Playfair Display',serif;font-size:1rem;margin-bottom:1rem;padding-bottom:.5rem;border-bottom:1px solid var(--border)}
input[type=text],input[type=number]{
  width:100%;background:var(--surface2);border:1px solid var(--border2);border-radius:var(--r2);
  color:var(--text);padding:.6rem .85rem;font-size:14px;font-family:'DM Sans',sans-serif;outline:none;transition:border .2s
}
input:focus{border-color:var(--gold)}
.btn{display:inline-flex;align-items:center;gap:.4rem;padding:.6rem 1.25rem;border-radius:var(--r2);
  border:1px solid var(--border2);background:transparent;color:var(--text);cursor:pointer;
  font-size:14px;font-family:'DM Sans',sans-serif;font-weight:500;transition:all .15s;white-space:nowrap}
.btn:hover{background:var(--surface3)}.btn:active{transform:scale(.98)}.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-gold{background:var(--gold);border-color:var(--gold);color:#111;font-weight:600}
.btn-gold:hover{background:var(--gold-light);border-color:var(--gold-light)}
.player-item{display:flex;align-items:center;justify-content:space-between;padding:.5rem 0;border-bottom:1px solid var(--border)}
.player-item:last-child{border-bottom:none}
.player-name{font-weight:500}
.host-badge,.you-badge{font-size:11px;padding:2px 8px;border-radius:20px}
.host-badge{background:var(--gold-glow);color:var(--gold);border:1px solid rgba(201,168,76,.3)}
.you-badge{background:var(--surface3);color:var(--muted)}
.hearts{display:flex;gap:3px;flex-wrap:wrap}
.heart-icon{font-size:14px;line-height:1}
.heart-alive{color:var(--heart)}.heart-dead{color:var(--dim)}
.round-bar{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:.75rem;margin-bottom:1.25rem}
.stat-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--r2);padding:.75rem 1rem}
.stat-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:.25rem}
.stat-val{font-size:1.4rem;font-weight:600;font-family:'Playfair Display',serif;color:var(--text)}
.trump-area{display:flex;align-items:flex-start;gap:1rem;background:var(--surface);border:1px solid var(--gold);
  border-radius:var(--r);padding:.85rem 1.1rem;margin-bottom:1.25rem;flex-wrap:wrap}
.trump-label{font-size:12px;color:var(--gold);text-transform:uppercase;letter-spacing:.07em;margin-bottom:.2rem}
.trump-card-big{font-size:1.6rem;font-family:'Playfair Display',serif;font-weight:700}
.order-pills{display:flex;gap:5px;flex-wrap:wrap;margin-top:.4rem}
.order-pill{font-size:12px;padding:2px 8px;border-radius:20px;background:var(--surface3);color:var(--muted);border:1px solid var(--border)}
.order-pill.first{background:var(--gold-glow);color:var(--gold);border-color:rgba(201,168,76,.4)}
.seq-track{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:1.25rem}
.seq-pip{width:26px;height:26px;border-radius:50%;border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--dim)}
.seq-pip.done{background:var(--surface3);color:var(--muted)}
.seq-pip.active{background:var(--gold);color:#111;font-weight:700;border-color:var(--gold)}
.hand-wrap{display:flex;gap:8px;flex-wrap:wrap;margin:.5rem 0 1rem}
.card{display:inline-flex;flex-direction:column;align-items:center;justify-content:center;
  min-width:52px;padding:.5rem .6rem;border:1px solid var(--border2);border-radius:8px;
  background:var(--surface2);cursor:pointer;font-weight:600;font-size:1.1rem;
  transition:transform .1s,border-color .15s,box-shadow .15s;user-select:none}
.card:hover{transform:translateY(-6px)}.card.selected{border-color:var(--gold);box-shadow:0 0 12px var(--gold-glow);transform:translateY(-8px)}
.card.hearts{color:var(--heart)}.card.spades{color:var(--spade)}.card.diamonds{color:var(--diamond)}.card.clubs{color:var(--club)}
.card-suit-sm{font-size:.7rem;margin-top:2px;opacity:.7}
.table-area{min-height:90px;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:1rem;display:flex;gap:.75rem;flex-wrap:wrap;align-items:center;margin-bottom:1rem}
.table-entry{display:flex;flex-direction:column;align-items:center;gap:4px}
.table-pname{font-size:11px;color:var(--muted);text-align:center}
.table-card{min-width:48px;padding:.4rem .5rem;border:1px solid var(--border2);border-radius:8px;background:var(--surface2);text-align:center;font-weight:600;font-size:1rem}
.winner-entry .table-pname{color:var(--gold)}
.winner-entry .table-card{border-color:var(--gold);box-shadow:0 0 10px var(--gold-glow)}
.bids-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.6rem;margin-bottom:1rem}
.bid-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--r2);padding:.6rem .85rem}
.bid-pname{font-size:12px;color:var(--muted);margin-bottom:2px}
.bid-val{font-size:1.1rem;font-weight:600}.bid-pending{color:var(--dim)}.bid-me{border-color:var(--gold)}
.result-row{display:flex;align-items:center;justify-content:space-between;padding:.6rem 0;border-bottom:1px solid var(--border);gap:.5rem;flex-wrap:wrap}
.result-row:last-child{border-bottom:none}
.result-name{font-weight:500;min-width:90px}.result-stat{font-size:13px;color:var(--muted)}
.badge{display:inline-block;font-size:12px;padding:2px 10px;border-radius:20px;font-weight:500}
.badge-ok{background:rgba(39,174,96,.15);color:var(--green-light);border:1px solid rgba(39,174,96,.3)}
.badge-bad{background:var(--red-glow);color:var(--red-light);border:1px solid rgba(192,57,43,.3)}
.waiting{color:var(--muted);font-size:14px;display:flex;align-items:center;gap:.5rem}
.dot-blink::after{content:'...';animation:blink 1.2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.turn-banner{padding:.6rem 1rem;border-radius:var(--r2);border-left:3px solid var(--gold);
  background:var(--gold-glow);font-size:14px;margin-bottom:.75rem;color:var(--gold-light)}
.not-turn{border-left-color:var(--border);background:var(--surface);color:var(--muted)}
#toast{position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:#222;
  border:1px solid var(--border2);color:var(--text);padding:.6rem 1.25rem;border-radius:var(--r2);
  font-size:14px;display:none;z-index:999;box-shadow:0 4px 24px rgba(0,0,0,.5)}
.top-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:1.5rem;flex-wrap:wrap;gap:.75rem}
.logo{display:flex;align-items:baseline;gap:.5rem}
.logo-sub{font-size:13px;color:var(--muted)}
.conn-dot{width:8px;height:8px;border-radius:50%;background:#555;display:inline-block}
.conn-dot.on{background:var(--green)}.conn-dot.off{background:var(--red)}
.elim{text-decoration:line-through;color:var(--dim)}
.winner-banner{text-align:center;padding:2rem;border:1px solid var(--gold);border-radius:var(--r);background:var(--gold-glow);margin-bottom:1.5rem}
.winner-name{font-size:2.5rem;font-family:'Playfair Display',serif;font-weight:700;color:var(--gold-light)}
@media(max-width:500px){h1{font-size:1.4rem}.card{min-width:44px;font-size:1rem}}
</style>
</head>
<body>
<div id="app">

<!-- JOIN -->
<div class="screen active" id="s-join">
  <div class="top-bar"><div class="logo"><h1>&#9830; Trick Game</h1></div></div>
  <div style="max-width:360px">
    <p style="color:var(--muted);margin-bottom:1.25rem;font-size:14px">Enter your name to join the game room.</p>
    <div style="margin-bottom:.75rem">
      <div style="font-size:13px;color:var(--muted);margin-bottom:.35rem">Your name</div>
      <input type="text" id="inp-name" placeholder="e.g. João" maxlength="20">
    </div>
    <button class="btn btn-gold" onclick="joinGame()" style="width:100%">Join room</button>
    <div id="join-err" style="color:var(--red-light);font-size:13px;margin-top:.5rem;display:none"></div>
  </div>
</div>

<!-- LOBBY -->
<div class="screen" id="s-lobby">
  <div class="top-bar">
    <div class="logo"><h1>&#9830; Trick Game</h1><span class="logo-sub">Lobby</span></div>
    <span><span class="conn-dot on" id="cdot"></span>&nbsp;<span style="font-size:13px;color:var(--muted)" id="conn-label">Connected</span></span>
  </div>
  <div class="lobby-wrap">
    <div class="panel">
      <h3>Players in room</h3>
      <div id="lobby-players"></div>
      <div style="margin-top:1rem;font-size:13px;color:var(--muted)">Share this page's URL with friends to invite them.</div>
    </div>
    <div class="panel">
      <h3>How to play</h3>
      <ul style="font-size:13px;color:var(--muted);line-height:1.9;padding-left:1rem">
        <li>Each player starts with <strong style="color:var(--text)">5 lives</strong></li>
        <li>Rounds: 1–2–3–4–5–6–7–8–7–6–5–4–3–2–1 cards</li>
        <li>A face-up card sets the <strong style="color:var(--gold)">card order</strong> each round</li>
        <li>Bid tricks — <em>never</em> equal to cards in hand</li>
        <li>Miss your bid → lose lives equal to the difference</li>
        <li>Last player alive wins!</li>
      </ul>
      <div style="margin-top:1.25rem" id="start-area"></div>
    </div>
  </div>
</div>

<!-- GAME -->
<div class="screen" id="s-game">
  <div class="top-bar">
    <div class="logo"><h1>&#9830; Trick Game</h1><span class="logo-sub" id="phase-label">Round 1</span></div>
    <span><span class="conn-dot on" id="cdot2"></span></span>
  </div>
  <div class="seq-track" id="seq-track"></div>
  <div class="round-bar">
    <div class="stat-box"><div class="stat-label">Round</div><div class="stat-val" id="sb-round"></div></div>
    <div class="stat-box"><div class="stat-label">Cards/player</div><div class="stat-val" id="sb-cards"></div></div>
    <div class="stat-box"><div class="stat-label">Trick</div><div class="stat-val" id="sb-trick"></div></div>
    <div class="stat-box"><div class="stat-label">Your lives</div><div class="stat-val" id="sb-lives"></div></div>
  </div>
  <div class="trump-area" id="trump-area">
    <div>
      <div class="trump-label">Trump card (face-up)</div>
      <div class="trump-card-big" id="trump-display"></div>
    </div>
    <div style="flex:1">
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:.35rem">Card order this round (strongest → weakest)</div>
      <div class="order-pills" id="order-pills"></div>
    </div>
  </div>
  <div id="turn-banner" class="turn-banner not-turn"></div>
  <div id="bids-section" style="display:none">
    <h3>Bids</h3>
    <div class="bids-grid" id="bids-grid"></div>
  </div>
  <div id="bid-ui" style="display:none">
    <div class="panel" style="margin-bottom:1rem">
      <h3>Your hand</h3>
      <div class="hand-wrap" id="bid-hand"></div>
      <div style="display:flex;gap:.75rem;align-items:flex-end;flex-wrap:wrap;margin-top:.5rem">
        <div>
          <div style="font-size:13px;color:var(--muted);margin-bottom:.3rem">Your bid (cannot be <strong id="forbidden-num"></strong>)</div>
          <input type="number" id="bid-input" min="0" style="width:100px">
        </div>
        <button class="btn btn-gold" onclick="submitBid()">Confirm bid</button>
      </div>
      <div id="bid-err" style="color:var(--red-light);font-size:13px;margin-top:.4rem;display:none"></div>
    </div>
  </div>
  <div id="play-ui" style="display:none">
    <h3 style="margin-bottom:.5rem">Table</h3>
    <div class="table-area" id="table-area"><span style="color:var(--dim);font-size:13px">Waiting for first card...</span></div>
    <div id="play-hand-wrap" style="display:none">
      <h3 style="margin-bottom:.5rem">Your hand — pick a card to play</h3>
      <div class="hand-wrap" id="play-hand"></div>
      <button class="btn btn-gold" id="btn-play" onclick="playCard()" disabled>Play selected card</button>
    </div>
  </div>
  <div style="margin-top:1.5rem">
    <h3>All players</h3>
    <div id="all-players-lives"></div>
  </div>
</div>

<!-- ROUND RESULT -->
<div class="screen" id="s-round-result">
  <div class="top-bar"><div class="logo"><h1>&#9830; Trick Game</h1><span class="logo-sub" id="rr-label">Round results</span></div></div>
  <div class="panel" style="margin-bottom:1rem"><h3>Results</h3><div id="rr-body"></div></div>
  <div class="panel" style="margin-bottom:1rem"><h3>Lives after this round</h3><div id="rr-lives"></div></div>
  <div id="rr-host-btn" style="display:none"><button class="btn btn-gold" onclick="nextRound()">Next round &#8594;</button></div>
  <div id="rr-wait" class="waiting" style="display:none"><span>Waiting for host to continue</span><span class="dot-blink"></span></div>
</div>

<!-- GAME OVER -->
<div class="screen" id="s-gameover">
  <div class="top-bar"><div class="logo"><h1>&#9830; Trick Game</h1></div></div>
  <div class="winner-banner">
    <div style="font-size:13px;color:var(--gold);text-transform:uppercase;letter-spacing:.1em;margin-bottom:.5rem">Winner</div>
    <div class="winner-name" id="go-winner"></div>
  </div>
  <div class="panel" style="margin-bottom:1rem"><h3>Final standings</h3><div id="go-standings"></div></div>
  <div id="go-host-btn" style="display:none"><button class="btn btn-gold" onclick="restartGame()">Play again</button></div>
</div>

</div>
<div id="toast"></div>

<script>
const SUIT_SYM  = {Hearts:'♥',Spades:'♠',Diamonds:'♦',Clubs:'♣'};
const SUIT_CLS  = {Hearts:'hearts',Spades:'spades',Diamonds:'diamonds',Clubs:'clubs'};
let ws, myName, state, selectedCardIdx=null;

function connect(){
  const proto = location.protocol==='https:'?'wss':'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen  = ()=>{ setConn(true); if(myName) send({action:'join',name:myName}); };
  ws.onclose = ()=>{ setConn(false); setTimeout(connect,2000); };
  ws.onerror = ()=>{ setConn(false); };
  ws.onmessage = e=>{
    const m=JSON.parse(e.data);
    if(m.type==='state'){ state=m.state; render(); }
    else if(m.type==='error'){ showToast(m.msg,'red'); }
  };
}
function send(o){ if(ws&&ws.readyState===1) ws.send(JSON.stringify(o)); }
function setConn(on){
  ['cdot','cdot2'].forEach(id=>{const el=document.getElementById(id);if(el)el.className='conn-dot '+(on?'on':'off');});
}

function joinGame(){
  const n=document.getElementById('inp-name').value.trim();
  if(!n){showToast('Enter a name','red');return;}
  myName=n; send({action:'join',name:n});
}
function startGame(){ send({action:'start'}); }
function nextRound() { send({action:'next_round'}); }
function restartGame(){ send({action:'restart'}); }
function submitBid(){
  const v=parseInt(document.getElementById('bid-input').value);
  const nc=state.num_cards;
  if(isNaN(v)||v<0||v>nc){showToast('Bid must be 0–'+nc,'red');return;}
  if(v===nc){showToast('Cannot bid exactly '+nc,'red');return;}
  send({action:'bid',bid:v});
}
function playCard(){
  if(selectedCardIdx===null){showToast('Select a card first','red');return;}
  send({action:'play_card',card_idx:selectedCardIdx});
  selectedCardIdx=null;
}

function render(){
  if(!state) return;
  if(!myName||!state.players.includes(myName)){ show('s-join'); return; }
  const ph=state.phase;
  if(ph==='lobby') { renderLobby(); show('s-lobby'); }
  else if(ph==='bidding'||ph==='playing'){ renderGame(); show('s-game'); }
  else if(ph==='round_result'){ renderRoundResult(); show('s-round-result'); }
  else if(ph==='game_over') { renderGameOver(); show('s-gameover'); }
}

function renderLobby(){
  const isHost=state.host===myName;
  let h='';
  state.players.forEach(p=>{
    h+=`<div class="player-item">
      <span class="player-name">${esc(p)}${p===myName?' <span class="you-badge">you</span>':''}</span>
      <div style="display:flex;align-items:center;gap:.5rem">
        ${p===state.host?'<span class="host-badge">host</span>':''}
        ${heartsBar(state.lives[p]||5,5)}
      </div></div>`;
  });
  document.getElementById('lobby-players').innerHTML=h||'<div style="color:var(--dim);font-size:13px">No players yet</div>';
  const sa=document.getElementById('start-area');
  if(isHost){
    sa.innerHTML=`<button class="btn btn-gold" id="btn-start" onclick="startGame()" ${state.players.length<2?'disabled':''}>Start game</button>
      <div style="font-size:12px;color:var(--dim);margin-top:.4rem">You are the host (min 2 players)</div>`;
  } else {
    sa.innerHTML=`<div class="waiting"><span>Waiting for host to start</span><span class="dot-blink"></span></div>`;
  }
}

function renderGame(){
  const s=state, nc=s.num_cards, alive=s.players.filter(p=>s.lives[p]>0), ph=s.phase;
  document.getElementById('phase-label').textContent=`Round ${s.round_idx+1}`;
  document.getElementById('sb-round').textContent=`${s.round_idx+1}/${s.round_seq.length}`;
  document.getElementById('sb-cards').textContent=nc;
  document.getElementById('sb-trick').textContent=ph==='playing'?`${s.trick_num}/${nc}`:'—';
  document.getElementById('sb-lives').textContent=s.lives[myName]||0;

  let seqH='';
  s.round_seq.forEach((n,i)=>seqH+=`<div class="seq-pip ${i<s.round_idx?'done':i===s.round_idx?'active':''}">${n}</div>`);
  document.getElementById('seq-track').innerHTML=seqH;

  const tc=s.trump_card;
  document.getElementById('trump-display').innerHTML=tc?`<span class="${SUIT_CLS[tc.s]}">${tc.v}${SUIT_SYM[tc.s]}</span>`:'None';
  document.getElementById('order-pills').innerHTML=s.card_order.map((v,i)=>`<span class="order-pill${i===0?' first':''}">${v}</span>`).join('');

  document.getElementById('bids-section').style.display='block';
  document.getElementById('bids-grid').innerHTML=alive.map(p=>{
    const isMe=p===myName, won=s.tricks_won[p]??0, hasBid=s.bids[p]!==undefined;
    return `<div class="bid-box${isMe?' bid-me':''}">
      <div class="bid-pname">${esc(p)}${isMe?' (you)':''}</div>
      <div class="bid-val${hasBid?'':' bid-pending'}">${hasBid?`Bid: ${s.bids[p]}<br><span style="font-size:13px;color:var(--muted)">Won: ${won}</span>`:'...'}</div>
    </div>`;
  }).join('');

  const bidder      = ph==='bidding' ? s.bid_order[s.bid_idx] : null;
  const turnIdx     = ph==='playing' ? (s.trick_leader+s.current_trick.length)%alive.length : -1;
  const trickPlayer = ph==='playing' ? alive[turnIdx] : null;
  const isMyBid     = ph==='bidding' && bidder===myName;
  const isMyPlay    = ph==='playing' && trickPlayer===myName;

  const tb=document.getElementById('turn-banner');
  if(isMyBid){ tb.className='turn-banner'; tb.textContent='Your turn to bid!'; }
  else if(isMyPlay){ tb.className='turn-banner'; tb.textContent='Your turn to play a card!'; }
  else if(ph==='bidding'){ tb.className='turn-banner not-turn'; tb.textContent=`Waiting for ${bidder} to bid...`; }
  else { tb.className='turn-banner not-turn'; tb.textContent=trickPlayer?`Waiting for ${trickPlayer} to play...`:'Waiting...'; }

  document.getElementById('bid-ui').style.display=isMyBid?'block':'none';
  document.getElementById('play-ui').style.display=ph==='playing'?'block':'none';

  if(isMyBid){
    document.getElementById('forbidden-num').textContent=nc;
    document.getElementById('bid-input').max=nc; document.getElementById('bid-input').min=0;
    renderHand(s.my_hand,'bid-hand',false);
  }

  if(ph==='playing'){
    const ta=document.getElementById('table-area');
    if(!s.current_trick||!s.current_trick.length){
      ta.innerHTML='<span style="color:var(--dim);font-size:13px">No cards played yet</span>';
    } else {
      let best=Infinity, winP=null;
      s.current_trick.forEach(e=>{const r=cRank(e.card,s.card_order);if(r<best){best=r;winP=e.player;}});
      ta.innerHTML=s.current_trick.map(e=>{const isW=e.player===winP;
        return `<div class="table-entry${isW?' winner-entry':''}">
          <div class="table-pname">${esc(e.player)}</div>
          <div class="table-card ${SUIT_CLS[e.card.s]}">${e.card.v}${SUIT_SYM[e.card.s]}</div>
          ${isW?'<div style="font-size:10px;color:var(--gold);text-align:center">leading</div>':''}
        </div>`;}).join('');
    }
    const phw=document.getElementById('play-hand-wrap');
    phw.style.display=isMyPlay?'block':'none';
    if(isMyPlay){ renderHand(s.my_hand,'play-hand',true); document.getElementById('btn-play').disabled=selectedCardIdx===null; }
  }

  document.getElementById('all-players-lives').innerHTML=s.players.map(p=>{
    const dead=s.lives[p]<=0;
    return `<div class="player-item">
      <span class="${dead?'elim':'player-name'}">${esc(p)}${p===myName?' <span class="you-badge">you</span>':''}</span>
      ${heartsBar(s.lives[p]||0,5)}</div>`;
  }).join('');
}

function renderHand(hand,elId,sel){
  const el=document.getElementById(elId);
  if(!hand||!hand.length){el.innerHTML='<span style="color:var(--dim)">No cards</span>';return;}
  el.innerHTML=hand.map((c,i)=>`<div class="card ${SUIT_CLS[c.s]}${sel&&selectedCardIdx===i?' selected':''}"
    onclick="${sel?`selectCard(${i})`:''}">${c.v}<div class="card-suit-sm">${SUIT_SYM[c.s]}</div></div>`).join('');
}
function selectCard(i){ selectedCardIdx=i; renderHand(state.my_hand,'play-hand',true); document.getElementById('btn-play').disabled=false; }

function renderRoundResult(){
  const s=state;
  document.getElementById('rr-label').textContent=`Round ${s.round_idx+1} results`;
  document.getElementById('rr-body').innerHTML=s.round_results.map(r=>{const ok=r.diff===0;
    return `<div class="result-row">
      <span class="result-name">${esc(r.player)}${r.player===myName?' <span class="you-badge">you</span>':''}</span>
      <span class="result-stat">Bid ${r.bid} · Won ${r.won}</span>
      <span class="badge ${ok?'badge-ok':'badge-bad'}">${ok?'Perfect!':'-'+r.diff+' life'+(r.diff!==1?'s':'')}</span>
    </div>`;}).join('')||'<span style="color:var(--dim)">No results</span>';
  document.getElementById('rr-lives').innerHTML=s.players.map(p=>
    `<div class="player-item"><span>${esc(p)}${p===myName?' <span class="you-badge">you</span>':''}</span>${heartsBar(s.lives[p]||0,5)}</div>`).join('');
  const isHost=s.host===myName;
  document.getElementById('rr-host-btn').style.display=isHost?'block':'none';
  document.getElementById('rr-wait').style.display=isHost?'none':'flex';
}

function renderGameOver(){
  const s=state, alive=s.players.filter(p=>s.lives[p]>0);
  document.getElementById('go-winner').textContent=alive.length===1?alive[0]:'Draw!';
  const sorted=[...s.players].sort((a,b)=>(s.lives[b]||0)-(s.lives[a]||0));
  document.getElementById('go-standings').innerHTML=sorted.map(p=>
    `<div class="result-row"><span class="result-name">${esc(p)}${p===myName?' <span class="you-badge">you</span>':''}</span>${heartsBar(s.lives[p]||0,5)}</div>`).join('');
  document.getElementById('go-host-btn').style.display=s.host===myName?'block':'none';
}

function cRank(card,order){
  const vi=order.indexOf(card.v), si=['Hearts','Spades','Diamonds','Clubs'].indexOf(card.s);
  return (vi<0?order.length:vi)*4+si;
}
function heartsBar(n,max){
  let h='<div class="hearts">';
  for(let i=0;i<max;i++) h+=`<span class="heart-icon ${i<n?'heart-alive':'heart-dead'}">&#9829;</span>`;
  return h+'</div>';
}
function show(id){ document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active')); document.getElementById(id).classList.add('active'); }
let toastTimer;
function showToast(msg,color=''){
  const t=document.getElementById('toast'); t.textContent=msg;
  t.style.borderColor=color==='red'?'var(--red)':'var(--border2)';
  t.style.display='block'; clearTimeout(toastTimer); toastTimer=setTimeout(()=>t.style.display='none',3000);
}
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

document.getElementById('inp-name').addEventListener('keydown',e=>{ if(e.key==='Enter') joinGame(); });
connect();
</script>
</body>
</html>
"""

# ─── HTTP routes ──────────────────────────────────────────────────────────────

async def index(request):
    return web.Response(text=HTML, content_type='text/html')

# ─── App factory ──────────────────────────────────────────────────────────────

def make_app():
    app = web.Application()
    app.router.add_get('/',   index)
    app.router.add_get('/ws', ws_handler)
    return app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f'Starting server on port {port}...')
    web.run_app(make_app(), host='0.0.0.0', port=port)
