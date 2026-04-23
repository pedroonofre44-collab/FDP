#!/usr/bin/env python3
"""Card Trick Game — Railway-ready. Single port HTTP+WebSocket."""

import asyncio, json, os, random
from aiohttp import web
import aiohttp

BASE_ORDER  = ['3','2','A','K','Q','J','7','6','5','4']
SUITS       = ['Hearts','Spades','Diamonds','Clubs']
SUIT_SYM    = {'Hearts':'♥','Spades':'♠','Diamonds':'♦','Clubs':'♣'}
ROUND_CYCLE = [2,3,4,5,6,7,8,7,6,5,4,3]

def round_num_cards(idx):
    return ROUND_CYCLE[idx % len(ROUND_CYCLE)]

def round_display_seq(round_idx, window=16):
    start = max(0, round_idx - 3)
    return [{'idx': i, 'nc': round_num_cards(i)} for i in range(start, start + window)]

def build_order(trump_value):
    if trump_value not in BASE_ORDER:
        return BASE_ORDER[:]
    idx = BASE_ORDER.index(trump_value)
    promote_idx = (idx - 1) % len(BASE_ORDER)
    above = BASE_ORDER[promote_idx]
    return [above] + [v for v in BASE_ORDER if v != above]

def card_rank(card, order):
    vi = order.index(card['v']) if card['v'] in order else len(order)
    return vi * 4 + SUITS.index(card['s'])

def build_deck():
    return [{'v': v, 's': s} for v in BASE_ORDER for s in SUITS]

def deal_cards(num_cards, players):
    deck = build_deck()
    random.shuffle(deck)
    hands = {p: deck[i*num_cards:(i+1)*num_cards] for i, p in enumerate(players)}
    remaining = deck[len(players)*num_cards:]
    return hands, remaining[0] if remaining else None

class GameState:
    def __init__(self): self.reset()
    def reset(self):
        self.phase = 'lobby'; self.players = []; self.lives = {}
        self.eliminated = []; self.round_idx = 0; self.hands = {}
        self.bids = {}; self.tricks_won = {}; self.current_trick = []
        self.last_trick = []; self.trick_num = 0; self.trick_leader = 0
        self.bid_order = []; self.bid_idx = 0; self.trump_card = None
        self.card_order = BASE_ORDER[:]; self.round_results = []
        self.host = None; self.first_bidder_idx = 0
    def alive_players(self):
        return [p for p in self.players if self.lives.get(p,0) > 0]
    def num_cards(self):
        return round_num_cards(self.round_idx)
    def to_public(self):
        return {
            'phase': self.phase, 'players': self.players, 'lives': self.lives,
            'eliminated': self.eliminated, 'round_idx': self.round_idx,
            'round_seq': round_display_seq(self.round_idx),
            'num_cards': self.num_cards(),
            'bids': self.bids, 'tricks_won': self.tricks_won,
            'current_trick': self.current_trick, 'last_trick': self.last_trick,
            'trick_num': self.trick_num, 'trick_leader': self.trick_leader,
            'bid_idx': self.bid_idx, 'bid_order': self.bid_order,
            'trump_card': self.trump_card, 'card_order': self.card_order,
            'round_results': self.round_results, 'host': self.host,
        }
    def to_player(self, name):
        pub = self.to_public()
        pub['my_hand'] = self.hands.get(name, [])
        pub['my_name'] = name
        return pub

G = GameState()
CLIENTS = {}

async def broadcast():
    dead = []
    for name, ws in list(CLIENTS.items()):
        try: await ws.send_json({'type':'state','state':G.to_player(name)})
        except: dead.append(name)
    for n in dead: CLIENTS.pop(n, None)

async def send_error(ws, msg):
    try: await ws.send_json({'type':'error','msg':msg})
    except: pass

def start_round():
    alive = G.alive_players(); nc = G.num_cards()
    G.hands, G.trump_card = deal_cards(nc, alive)
    G.card_order = build_order(G.trump_card['v']) if G.trump_card else BASE_ORDER[:]
    for p in alive: G.hands[p].sort(key=lambda c: -card_rank(c, G.card_order))
    G.bids = {}; G.tricks_won = {p:0 for p in alive}
    start = G.first_bidder_idx % len(alive)
    G.bid_order = alive[start:] + alive[:start]
    G.bid_idx = 0; G.current_trick = []; G.last_trick = []
    G.trick_num = 0; G.trick_leader = start; G.round_results = []
    G.phase = 'bidding'

def next_trick():
    G.last_trick = []; G.current_trick = []; G.trick_num += 1

def resolve_trick():
    winner_entry = min(G.current_trick, key=lambda e: card_rank(e['card'], G.card_order))
    winner = winner_entry['player']
    G.tricks_won[winner] += 1
    alive = G.alive_players()
    G.trick_leader = alive.index(winner)
    G.last_trick = list(G.current_trick)
    if G.trick_num >= G.num_cards():
        results = []
        for p in alive:
            bid = G.bids.get(p,0); won = G.tricks_won[p]; diff = abs(bid-won)
            G.lives[p] = max(0, G.lives[p]-diff)
            if G.lives[p]==0 and p not in G.eliminated: G.eliminated.append(p)
            results.append({'player':p,'bid':bid,'won':won,'diff':diff,'lives':G.lives[p]})
        G.round_results = results; G.phase = 'round_result'
    else:
        next_trick()

async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1024*1024)
    await ws.prepare(request)
    player_name = None
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try: data = json.loads(msg.data)
                except: continue
                action = data.get('action')

                if action == 'join':
                    name = str(data.get('name','')).strip()[:20]
                    if not name: await send_error(ws,'Enter a name.'); continue
                    if name in CLIENTS and CLIENTS[name] is not ws: await send_error(ws,'Name taken.'); continue
                    if G.phase != 'lobby' and name not in G.players: await send_error(ws,'Game already started.'); continue
                    if len(G.players) >= 7 and name not in G.players: await send_error(ws,'Room full (max 7).'); continue
                    player_name = name; CLIENTS[name] = ws
                    if name not in G.players: G.players.append(name); G.lives[name] = 5
                    if G.host is None: G.host = name
                    await broadcast()

                elif action == 'start':
                    if player_name != G.host: await send_error(ws,'Only host can start.'); continue
                    if len(G.players) < 1: await send_error(ws,'Need players.'); continue
                    G.round_idx = 0
                    G.first_bidder_idx = random.randrange(len(G.alive_players()))
                    start_round(); await broadcast()

                elif action == 'bid':
                    if G.phase != 'bidding': continue
                    alive = G.alive_players()
                    if G.bid_idx >= len(alive): continue
                    current_bidder = G.bid_order[G.bid_idx]
                    if player_name != current_bidder: await send_error(ws,"Not your turn."); continue
                    try: bid = int(data['bid'])
                    except: await send_error(ws,'Invalid bid.'); continue
                    nc = G.num_cards()
                    if bid < 0 or bid > nc: await send_error(ws,f'Bid 0\u2013{nc}.'); continue
                    if G.bid_idx == len(alive)-1:
                        if sum(G.bids.values())+bid == nc:
                            await send_error(ws,f'Last bidder: cannot bid {nc-sum(G.bids.values())} (total would equal {nc}).'); continue
                    G.bids[current_bidder] = bid; G.bid_idx += 1
                    if G.bid_idx >= len(alive): G.phase='playing'; G.trick_num=0; next_trick()
                    await broadcast()

                elif action == 'play_card':
                    if G.phase != 'playing': continue
                    alive = G.alive_players()
                    turn_idx = (G.trick_leader+len(G.current_trick)) % len(alive)
                    if player_name != alive[turn_idx]: await send_error(ws,"Not your turn."); continue
                    hand = G.hands.get(player_name,[])
                    ci = data.get('card_idx')
                    if ci is None or not (0 <= int(ci) < len(hand)): await send_error(ws,'Invalid card.'); continue
                    card = hand.pop(int(ci))
                    G.current_trick.append({'player':player_name,'card':card})
                    if len(G.current_trick) == len(alive): resolve_trick()
                    await broadcast()

                elif action == 'next_round':
                    if player_name != G.host: continue
                    if G.phase == 'round_result':
                        G.round_idx += 1
                        G.first_bidder_idx = (G.first_bidder_idx+1) % max(1,len(G.alive_players()))
                        if len(G.alive_players()) <= 1: G.phase = 'game_over'
                        else: start_round()
                        await broadcast()

                elif action == 'restart':
                    if player_name != G.host: continue
                    names = list(CLIENTS.keys()); G.reset()
                    G.players = names; G.lives = {n:5 for n in names}
                    G.host = names[0] if names else None
                    await broadcast()

            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    except: pass
    finally:
        if player_name:
            CLIENTS.pop(player_name, None)
            if G.phase == 'lobby':
                G.players = [p for p in G.players if p != player_name]
                G.lives.pop(player_name, None)
                if G.host == player_name: G.host = G.players[0] if G.players else None
        await broadcast()
    return ws

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>FDP</title>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;700&family=Inter:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{
  --felt:#1a4a2e;--felt2:#153d25;--felt-edge:#0d2e1a;
  --gold:#c9a84c;--gold2:#f0c060;--gold-dim:rgba(201,168,76,.18);
  --bg:#0a0a0a;--sur:#141414;--sur2:#1e1e1e;--sur3:#272727;
  --bor:#2a2a2a;--bor2:#383838;
  --text:#f0ece4;--muted:#777;--dim:#3a3a3a;
  --red:#d63031;--red2:#e74c3c;--red-bg:rgba(214,48,49,.13);
  --green:#00b894;--green-bg:rgba(0,184,148,.1);
  --heart:#e74c3c;--spade:#eaeaea;--diamond:#5dade2;--club:#58d68d;
  --r:12px;--r2:8px;--r3:6px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px;overflow-x:hidden}
h1{font-family:'Cinzel',serif;font-weight:700;color:var(--gold2)}
h2{font-family:'Cinzel',serif;font-weight:600;font-size:1.05rem;color:var(--gold)}
h3{font-family:'Cinzel',serif;font-weight:400;font-size:.85rem;color:var(--gold);letter-spacing:.05em}

.screen{display:none;min-height:100vh;flex-direction:column}
.screen.active{display:flex}

/* ── JOIN ── */
#s-join{align-items:center;justify-content:center;background:radial-gradient(ellipse at center,#181818 0%,#080808 100%)}
.jcard{background:var(--sur);border:1px solid var(--bor2);border-radius:var(--r);padding:2.5rem 2rem;width:min(360px,92vw);text-align:center}
.jcard h1{font-size:1.9rem;margin-bottom:.25rem}
.jcard p{color:var(--muted);font-size:12px;margin-bottom:1.4rem}
.suits{font-size:1.3rem;margin-bottom:1.3rem;letter-spacing:.35rem;opacity:.5}
.inp{width:100%;background:var(--sur2);border:1px solid var(--bor2);border-radius:var(--r3);color:var(--text);padding:.65rem 1rem;font-size:14px;font-family:'Inter',sans-serif;outline:none;text-align:center;transition:border .2s;margin-bottom:.75rem}
.inp:focus{border-color:var(--gold)}

/* ── BUTTONS ── */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.35rem;padding:.6rem 1.3rem;border-radius:var(--r3);border:1px solid var(--bor2);background:transparent;color:var(--text);cursor:pointer;font-size:13px;font-family:'Inter',sans-serif;font-weight:500;transition:all .14s;white-space:nowrap}
.btn:hover{background:var(--sur3)}.btn:active{transform:scale(.97)}.btn:disabled{opacity:.3;cursor:not-allowed}
.btn-gold{background:var(--gold);border-color:var(--gold);color:#111;font-weight:600}
.btn-gold:hover{background:var(--gold2)}

/* ── TOPBAR ── */
.topbar{display:flex;align-items:center;justify-content:space-between;padding:.55rem 1.1rem;background:var(--sur);border-bottom:1px solid var(--bor);flex-shrink:0;gap:.5rem;flex-wrap:wrap;min-height:44px}
.tbl{display:flex;align-items:center;gap:.65rem}
.topbar h1{font-size:1rem;letter-spacing:.04em}
.rtag{font-size:11px;color:var(--muted);background:var(--sur2);border:1px solid var(--bor);border-radius:20px;padding:2px 9px;white-space:nowrap}
.cdot{width:7px;height:7px;border-radius:50%;background:var(--dim);flex-shrink:0}
.cdot.on{background:var(--green)}.cdot.off{background:var(--red2)}
.seqt{display:flex;gap:3px;align-items:center}
.spip{width:21px;height:21px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:500;color:var(--dim);border:1px solid var(--bor);flex-shrink:0}
.spip.done{color:var(--muted);background:var(--sur2)}.spip.now{background:var(--gold);color:#111;border-color:var(--gold);font-weight:700}

/* ── TRUMP BAR ── */
.tbar{display:flex;align-items:center;gap:.85rem;padding:.45rem 1.1rem;background:var(--sur2);border-bottom:1px solid var(--bor);flex-shrink:0;flex-wrap:wrap}
.tlabel{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:2px}
.tchip{display:inline-flex;align-items:center;gap:.25rem;background:var(--sur3);border:1px solid var(--gold);border-radius:var(--r3);padding:3px 9px;font-weight:700;font-size:1rem}
.opills{display:flex;gap:3px;flex-wrap:wrap}
.opill{font-size:11px;padding:2px 7px;border-radius:20px;background:var(--sur3);color:var(--muted);border:1px solid var(--bor)}
.opill.top{background:var(--gold-dim);color:var(--gold);border-color:var(--gold)}

/* ── FELT TABLE ── */
#table-wrap{flex:1;display:flex;align-items:center;justify-content:center;padding:.75rem;position:relative;min-height:0;overflow:hidden}
.felt{position:relative;width:min(680px,96vw);aspect-ratio:1.6;border-radius:50%;
  background:radial-gradient(ellipse at 38% 38%,#1f5535 0%,#163d25 55%,#0d2810 100%);
  box-shadow:0 0 0 7px #0b1f0f,0 0 0 11px #091a0c,0 0 0 13px #07150a,0 24px 70px rgba(0,0,0,.85);
  display:flex;align-items:center;justify-content:center;flex-shrink:0}

/* ── SEATS ── */
.seat{position:absolute;display:flex;flex-direction:column;align-items:center;gap:3px;transform:translate(-50%,-50%);z-index:2}
.avatar{width:54px;height:54px;border-radius:50%;background:var(--sur);border:2px solid var(--bor2);display:flex;align-items:center;justify-content:center;font-family:'Cinzel',serif;font-size:.95rem;font-weight:700;color:var(--gold);transition:border-color .2s,box-shadow .2s;position:relative;flex-shrink:0;cursor:default}
.avatar.my-turn{border-color:var(--gold2);box-shadow:0 0 0 3px var(--gold-dim),0 0 22px rgba(201,168,76,.25)}
.avatar.is-me{border-color:var(--green)}
.avatar.out{opacity:.3;filter:grayscale(1)}
.sname{font-size:11px;font-weight:500;color:var(--text);max-width:72px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;background:rgba(0,0,0,.6);border-radius:4px;padding:1px 5px;line-height:1.4}
.sname.is-me{color:var(--green)}
.shearts{display:flex;gap:2px;justify-content:center}
.sh{font-size:9px;line-height:1}.sh.a{color:var(--heart)}.sh.d{color:var(--dim)}
.bid-badge{position:absolute;top:-5px;right:-5px;background:var(--sur);border:1px solid var(--gold);border-radius:10px;font-size:10px;font-weight:600;color:var(--gold);padding:1px 5px;line-height:1.4;white-space:nowrap}
.won-badge{position:absolute;bottom:-5px;right:-5px;background:var(--green-bg);border:1px solid var(--green);border-radius:10px;font-size:10px;font-weight:600;color:var(--green);padding:1px 5px;line-height:1.4}

/* ── CENTRE POT ── */
.pot{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;z-index:1}
.pot-inner{display:flex;flex-direction:column;align-items:center;gap:.4rem;max-width:260px}
.trick-row{display:flex;gap:5px;flex-wrap:wrap;justify-content:center}
.tc{width:36px;height:52px;border-radius:5px;background:#131313;border:1px solid #2a2a2a;display:flex;flex-direction:column;align-items:center;justify-content:center;font-weight:700;font-size:.9rem;position:relative;flex-shrink:0}
.tc.winner-card{border-color:var(--gold);box-shadow:0 0 8px rgba(201,168,76,.4)}
.tc .cs{font-size:.55rem;opacity:.65}
.tc.hearts{color:var(--heart)}.tc.spades{color:var(--spade)}.tc.diamonds{color:var(--diamond)}.tc.clubs{color:var(--club)}
.tc-name{font-size:9px;color:rgba(255,255,255,.45);text-align:center;max-width:38px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:2px}
.trick-entry{display:flex;flex-direction:column;align-items:center}
.pot-msg{font-size:11px;color:rgba(255,255,255,.35);font-family:'Cinzel',serif;letter-spacing:.06em;text-align:center}
.lt-label{font-size:9px;color:rgba(255,255,255,.25);text-transform:uppercase;letter-spacing:.08em;text-align:center}

/* ── HAND PANEL ── */
#hand-panel{background:var(--sur);border-top:1px solid var(--bor);padding:.65rem 1rem 1rem;flex-shrink:0}
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:.55rem;flex-wrap:wrap;gap:.35rem}
.htitle{font-size:11px;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}
.ti{font-size:11px;font-weight:500;padding:3px 11px;border-radius:20px}
.ti.mine{background:var(--gold-dim);color:var(--gold);border:1px solid var(--gold)}
.ti.wait{background:var(--sur2);color:var(--muted);border:1px solid var(--bor)}
.cards-row{display:flex;gap:5px;flex-wrap:wrap;align-items:flex-end;justify-content:center}
.hcard{width:44px;height:62px;border-radius:6px;background:var(--sur2);border:1.5px solid var(--bor2);display:flex;flex-direction:column;align-items:center;justify-content:center;font-weight:700;font-size:1rem;transition:transform .11s,border-color .11s,box-shadow .11s;user-select:none;flex-shrink:0}
.hcard.pick{cursor:pointer}.hcard.pick:hover{transform:translateY(-7px)}
.hcard.selected{transform:translateY(-11px);border-color:var(--gold);box-shadow:0 0 12px var(--gold-dim)}
.hcard .cs{font-size:.58rem;opacity:.7}
.hcard.hearts{color:var(--heart)}.hcard.spades{color:var(--spade)}.hcard.diamonds{color:var(--diamond)}.hcard.clubs{color:var(--club)}
.pbrow{display:flex;align-items:center;gap:.75rem;margin-top:.55rem;justify-content:center}

/* ── BID PANEL ── */
#bid-panel{background:var(--sur);border-top:1px solid var(--bor);padding:.65rem 1rem;flex-shrink:0}
.bhand{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:.55rem}
.bcard{width:36px;height:52px;border-radius:5px;background:var(--sur2);border:1px solid var(--bor);display:flex;flex-direction:column;align-items:center;justify-content:center;font-weight:700;font-size:.85rem}
.bcard .cs{font-size:.52rem;opacity:.7}
.bcard.hearts{color:var(--heart)}.bcard.spades{color:var(--spade)}.bcard.diamonds{color:var(--diamond)}.bcard.clubs{color:var(--club)}
.bidr{display:flex;align-items:center;gap:.65rem;flex-wrap:wrap}
.binp{width:72px;background:var(--sur2);border:1px solid var(--bor2);border-radius:var(--r3);color:var(--text);padding:.45rem .65rem;font-size:1.1rem;font-weight:600;font-family:'Inter',sans-serif;outline:none;text-align:center;transition:border .2s}
.binp:focus{border-color:var(--gold)}
.bnote{font-size:12px;color:var(--red2);margin-top:.4rem}

/* ── LOBBY ── */
#s-lobby{background:var(--bg)}
.lwrap{max-width:680px;margin:0 auto;padding:1.25rem 1rem;width:100%}
.prow{display:flex;align-items:center;justify-content:space-between;padding:.5rem .7rem;border-bottom:1px solid var(--bor);gap:.5rem}
.prow:last-child{border-bottom:none}
.hbadge{font-size:10px;padding:2px 7px;border-radius:20px;font-weight:500}
.hbadge.host{background:var(--gold-dim);color:var(--gold);border:1px solid rgba(201,168,76,.3)}
.hbadge.you{background:var(--sur3);color:var(--muted)}

/* ── OVERLAYS ── */
.ov{position:fixed;inset:0;background:rgba(0,0,0,.87);display:none;align-items:center;justify-content:center;z-index:100;padding:1rem}
.ov.active{display:flex}
.ovbox{background:var(--sur);border:1px solid var(--bor2);border-radius:var(--r);padding:1.4rem 1.25rem;width:min(480px,95vw);max-height:90vh;overflow-y:auto}
.ovbox h2{margin-bottom:.9rem;text-align:center}
.rrow{display:flex;align-items:center;justify-content:space-between;padding:.45rem 0;border-bottom:1px solid var(--bor);gap:.5rem;flex-wrap:wrap}
.rrow:last-child{border-bottom:none}
.rbdg{font-size:11px;padding:2px 9px;border-radius:20px;font-weight:500}
.rbdg.ok{background:var(--green-bg);color:var(--green);border:1px solid rgba(0,184,148,.3)}
.rbdg.bad{background:var(--red-bg);color:var(--red2);border:1px solid rgba(214,48,49,.3)}
.lbar{display:flex;gap:2px}
.lh{font-size:12px}.lh.a{color:var(--heart)}.lh.d{color:var(--dim)}
.gowinner{text-align:center;padding:1.4rem;background:var(--gold-dim);border:1px solid var(--gold);border-radius:var(--r);margin-bottom:1rem}
.goname{font-family:'Cinzel',serif;font-size:1.9rem;font-weight:700;color:var(--gold2)}
.wait{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:.35rem}
.dot::after{content:'...';animation:blink 1.2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

#toast{position:fixed;bottom:1.4rem;left:50%;transform:translateX(-50%);background:var(--sur2);border:1px solid var(--bor2);color:var(--text);padding:.5rem 1.1rem;border-radius:var(--r3);font-size:13px;display:none;z-index:999;box-shadow:0 4px 20px rgba(0,0,0,.6);max-width:88vw;text-align:center}

@media(max-width:480px){
  .felt{aspect-ratio:1;width:min(320px,95vw)}
  .avatar{width:44px;height:44px;font-size:.82rem}
  .hcard{width:38px;height:55px;font-size:.9rem}
}
</style>
</head>
<body>

<!-- JOIN -->
<div class="screen active" id="s-join">
  <div class="jcard">
    <div class="suits">♠ ♥ ♦ ♣</div>
    <h1>Trick Game</h1>
    <p>Enter your name to join the table</p>
    <input class="inp" id="inp-name" placeholder="Your name" maxlength="20">
    <button class="btn btn-gold" onclick="joinGame()" style="width:100%">Join table</button>
    <div id="join-status" style="font-size:12px;margin-top:.5rem;display:none;text-align:center"></div>
  </div>
</div>

<!-- LOBBY -->
<div class="screen" id="s-lobby">
  <div class="topbar">
    <div class="tbl"><h1>Trick Game</h1><span class="rtag">Lobby</span></div>
    <div style="display:flex;align-items:center;gap:.45rem"><span class="cdot on" id="cdot-l"></span><span style="color:var(--muted);font-size:11px">Connected</span></div>
  </div>
  <div class="lwrap">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1.1rem;margin-bottom:1.1rem">
      <div>
        <h3 style="margin-bottom:.65rem">Players at the table</h3>
        <div style="background:var(--sur);border:1px solid var(--bor);border-radius:var(--r2)" id="lobby-list"></div>
        <div style="font-size:11px;color:var(--dim);margin-top:.45rem">Share the URL to invite friends</div>
      </div>
      <div>
        <h3 style="margin-bottom:.65rem">Rules</h3>
        <ul style="font-size:12px;color:var(--muted);line-height:1.95;padding-left:1rem">
          <li>5 lives each — last alive wins</li>
          <li>Rounds: 2→8→2 (cycles forever)</li>
          <li>Face-up card sets order each round</li>
          <li>Last bidder can't make total = cards</li>
          <li>Miss bid → lose lives = difference</li>
        </ul>
        <div style="margin-top:.9rem" id="lobby-action"></div>
      </div>
    </div>
  </div>
</div>

<!-- GAME -->
<div class="screen" id="s-game">
  <div class="topbar">
    <div class="tbl">
      <h1>Trick Game</h1>
      <span class="rtag" id="round-tag">Round 1</span>
      <div class="seqt" id="seq-track"></div>
    </div>
    <span class="cdot on" id="cdot-g"></span>
  </div>
  <div class="tbar">
    <div><div class="tlabel">Trump card</div><div class="tchip" id="trump-chip">—</div></div>
    <div><div class="tlabel" style="margin-bottom:3px">Card order (strongest → weakest)</div><div class="opills" id="order-pills"></div></div>
    <div style="margin-left:auto;text-align:right">
      <div class="tlabel">Trick</div>
      <div style="font-size:1rem;font-weight:600;font-family:'Cinzel',serif;color:var(--text)" id="trick-ctr">—</div>
    </div>
  </div>
  <div id="table-wrap">
    <div class="felt" id="felt">
      <div class="pot"><div class="pot-inner" id="pot-inner"></div></div>
    </div>
  </div>
  <div id="bid-panel" style="display:none">
    <div style="display:flex;align-items:center;gap:.6rem;margin-bottom:.5rem;flex-wrap:wrap">
      <h3>Place your bid</h3>
      <span class="ti mine">Your turn to bid</span>
    </div>
    <div class="bhand" id="bid-hand"></div>
    <div class="bidr">
      <div>
        <div style="font-size:11px;color:var(--muted);margin-bottom:.3rem">Tricks (0 to <span id="bid-max"></span>)</div>
        <input class="binp" type="number" id="bid-input" min="0">
      </div>
      <button class="btn btn-gold" onclick="submitBid()">Confirm bid</button>
    </div>
    <div class="bnote" id="bid-note" style="display:none"></div>
  </div>
  <div id="hand-panel" style="display:none">
    <div class="hdr">
      <div class="htitle">Your hand</div>
      <div id="ti-hand" class="ti wait">Waiting...</div>
    </div>
    <div class="cards-row" id="hand-cards"></div>
    <div class="pbrow" id="pbrow" style="display:none">
      <button class="btn btn-gold" id="btn-play" onclick="playCard()" disabled>Play selected card</button>
    </div>
  </div>
</div>

<!-- RESULT OVERLAY -->
<div class="ov" id="s-result">
  <div class="ovbox">
    <h2 id="res-title">Round results</h2>
    <div id="res-body" style="margin-bottom:.85rem"></div>
    <h3 style="margin-bottom:.5rem">Lives</h3>
    <div id="res-lives" style="margin-bottom:1.1rem"></div>
    <div id="res-action"></div>
  </div>
</div>

<!-- GAME OVER OVERLAY -->
<div class="ov" id="s-gameover">
  <div class="ovbox">
    <div class="gowinner">
      <div style="font-size:10px;color:var(--gold);text-transform:uppercase;letter-spacing:.1em;margin-bottom:.35rem">Winner</div>
      <div class="goname" id="go-name"></div>
    </div>
    <h3 style="margin-bottom:.5rem">Final standings</h3>
    <div id="go-stands" style="margin-bottom:1.1rem"></div>
    <div id="go-action"></div>
  </div>
</div>

<div id="toast"></div>

<script>
const SC = {Hearts:'hearts',Spades:'spades',Diamonds:'diamonds',Clubs:'clubs'};
const SY = {Hearts:'♥',Spades:'♠',Diamonds:'♦',Clubs:'♣'};
let ws, myName, S, sel=null;

// ── Seat positions ────────────────────────────────────────────────────────────
function seatPos(n, myIdx) {
  // myIdx sits at bottom (angle π/2), others go clockwise
  const rx=41, ry=36; // % of felt
  return Array.from({length:n}, (_,i) => {
    const angle = (2*Math.PI/n)*((i-myIdx+n)%n) + Math.PI/2;
    return { x: 50 + rx*Math.cos(angle), y: 50 + ry*Math.sin(angle) };
  });
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol==='https:'?'wss':'ws';
  const url = `${proto}://${location.host}/ws`;
  setJoinStatus('Connecting...', false);
  try { ws = new WebSocket(url); } catch(e) { setJoinStatus('Connection failed: '+e.message, true); setTimeout(connect,3000); return; }
  ws.onopen  = () => { setC(true); setJoinStatus('', false); if (myName) send({action:'join',name:myName}); };
  ws.onclose = () => { setC(false); setJoinStatus('Reconnecting...', false); setTimeout(connect,2000); };
  ws.onerror = (e) => { setC(false); setJoinStatus('Connection error — retrying...', true); };
  ws.onmessage = e => {
    try {
      const m=JSON.parse(e.data);
      if (m.type==='state') { S=m.state; render(); }
      else if (m.type==='error') toast(m.msg, true);
    } catch(err) { console.error('Parse error', err); }
  };
}
function send(o) {
  if (ws && ws.readyState===1) { ws.send(JSON.stringify(o)); return true; }
  return false;
}
function setC(on) {
  ['cdot-l','cdot-g'].forEach(id=>{const el=document.getElementById(id);if(el)el.className='cdot '+(on?'on':'off');});
}
function setJoinStatus(msg, isErr) {
  const el=document.getElementById('join-status');
  if (!el) return;
  el.textContent=msg;
  el.style.color=isErr?'var(--red2)':'var(--muted)';
  el.style.display=msg?'block':'none';
}

function joinGame() {
  const n=document.getElementById('inp-name').value.trim();
  if (!n) { toast('Enter a name',true); return; }
  myName=n;
  if (!send({action:'join',name:n})) {
    // WS not ready yet — it will send on reconnect via onopen
    setJoinStatus('Connecting, please wait...', false);
  }
}
function submitBid() {
  const v=parseInt(document.getElementById('bid-input').value);
  const nc=S.num_cards;
  if (isNaN(v)||v<0||v>nc) { toast('Bid 0–'+nc,true); return; }
  send({action:'bid',bid:v});
}
function playCard() {
  if (sel===null) { toast('Select a card',true); return; }
  send({action:'play_card',card_idx:sel}); sel=null;
}

// ── Render ────────────────────────────────────────────────────────────────────
function render() {
  if (!S) return;
  if (!myName||!S.players.includes(myName)) { show('s-join'); return; }
  document.getElementById('s-result').classList.remove('active');
  document.getElementById('s-gameover').classList.remove('active');
  const ph=S.phase;
  if (ph==='lobby') { renderLobby(); show('s-lobby'); }
  else { show('s-game'); renderGame();
    if (ph==='round_result') renderResult();
    else if (ph==='game_over') renderGameOver();
  }
}

// ── Lobby ─────────────────────────────────────────────────────────────────────
function renderLobby() {
  const isHost=S.host===myName;
  document.getElementById('lobby-list').innerHTML=S.players.map(p=>
    `<div class="prow"><span style="font-weight:500">${esc(p)}${p===myName?' <span class="hbadge you">you</span>':''}</span>
    <div style="display:flex;align-items:center;gap:.4rem">
      ${p===S.host?'<span class="hbadge host">host</span>':''}
      ${lbar(S.lives[p]||5,5)}
    </div></div>`).join('')||'<div style="padding:.7rem;color:var(--dim);font-size:12px">No players yet</div>';
  document.getElementById('lobby-action').innerHTML=isHost
    ?`<button class="btn btn-gold" onclick="send({action:'start'})">Start game</button><div style="font-size:11px;color:var(--dim);margin-top:.3rem">You are the host</div>`
    :`<div class="wait"><span>Waiting for host</span><span class="dot"></span></div>`;
}

// ── Game ──────────────────────────────────────────────────────────────────────
function renderGame() {
  const s=S, ph=s.phase, nc=s.num_cards;
  const alive=s.players.filter(p=>s.lives[p]>0);
  const myIdx=alive.indexOf(myName);

  // Topbar
  document.getElementById('round-tag').textContent=`Round ${s.round_idx+1} · ${nc} cards`;
  document.getElementById('seq-track').innerHTML=s.round_seq.map(r=>
    `<div class="spip ${r.idx<s.round_idx?'done':r.idx===s.round_idx?'now':''}">${r.nc}</div>`).join('');

  // Trump bar
  const tc=s.trump_card;
  document.getElementById('trump-chip').innerHTML=tc?`<span class="${SC[tc.s]}">${tc.v}${SY[tc.s]}</span>`:'—';
  document.getElementById('order-pills').innerHTML=s.card_order.map((v,i)=>
    `<span class="opill${i===0?' top':''}">${v}</span>`).join('');
  document.getElementById('trick-ctr').textContent=ph==='playing'?`${s.trick_num}/${nc}`:'—';

  // Turn logic
  const bidder=ph==='bidding'?s.bid_order[s.bid_idx]:null;
  const tti=(s.trick_leader+s.current_trick.length)%Math.max(1,alive.length);
  const trickP=ph==='playing'?alive[tti]:null;
  const isMyBid=ph==='bidding'&&bidder===myName;
  const isMyPlay=ph==='playing'&&trickP===myName;

  // ── Seats ────────────────────────────────────────────────────────────────
  const felt=document.getElementById('felt');
  felt.querySelectorAll('.seat').forEach(e=>e.remove());
  const safeIdx=myIdx>=0?myIdx:0;
  const positions=seatPos(alive.length, safeIdx);

  alive.forEach((p,i) => {
    const pos=positions[i];
    const isMe=p===myName;
    const isActive=(ph==='bidding'&&bidder===p)||(ph==='playing'&&trickP===p);
    const isOut=s.lives[p]<=0;
    const hasBid=s.bids[p]!==undefined;
    const won=s.tricks_won[p]??0;

    const seat=document.createElement('div');
    seat.className='seat';
    seat.style.left=pos.x+'%';
    seat.style.top=pos.y+'%';
    seat.innerHTML=`
      <div class="avatar${isActive?' my-turn':''}${isMe?' is-me':''}${isOut?' out':''}">
        ${p.slice(0,2).toUpperCase()}
        ${hasBid?`<div class="bid-badge">${s.bids[p]}</div>`:''}
        ${ph==='playing'?`<div class="won-badge">${won}/${s.bids[p]??'?'}</div>`:''}
      </div>
      <div class="sname${isMe?' is-me':''}">${esc(p)}</div>
      <div class="shearts">${shb(s.lives[p]||0,5)}</div>`;
    felt.appendChild(seat);
  });

  // ── Centre pot ───────────────────────────────────────────────────────────
  const pi=document.getElementById('pot-inner');
  let ph2='';

  if (ph==='playing' && s.current_trick.length>0) {
    let best=Infinity, winP=null;
    s.current_trick.forEach(e=>{const r=cr(e.card,s.card_order);if(r<best){best=r;winP=e.player;}});
    ph2+=`<div class="trick-row">${s.current_trick.map(e=>{const w=e.player===winP;
      return `<div class="trick-entry">
        <div class="tc-name">${esc(e.player)}</div>
        <div class="tc ${SC[e.card.s]}${w?' winner-card':''}">${e.card.v}<span class="cs">${SY[e.card.s]}</span></div>
      </div>`;}).join('')}</div>`;
    if (trickP) ph2+=`<div class="pot-msg">${esc(trickP)}'s turn</div>`;
  } else if (ph==='playing') {
    if (s.last_trick&&s.last_trick.length) {
      let best=Infinity,winP=null;
      s.last_trick.forEach(e=>{const r=cr(e.card,s.card_order);if(r<best){best=r;winP=e.player;}});
      ph2+=`<div class="lt-label">Last trick — ${esc(winP)} won</div>
        <div class="trick-row">${s.last_trick.map(e=>`<div class="tc ${SC[e.card.s]}" style="width:30px;height:43px;font-size:.78rem;opacity:.55">${e.card.v}<span class="cs">${SY[e.card.s]}</span></div>`).join('')}</div>`;
    }
    ph2+=`<div class="pot-msg">${esc(trickP||'')} leads</div>`;
  } else if (ph==='bidding') {
    const sb=Object.values(s.bids).reduce((a,b)=>a+b,0);
    ph2=`<div class="pot-msg">Bidding<br><span style="opacity:.5;font-size:10px">Total: ${sb} / ${nc}</span></div>`;
  }
  pi.innerHTML=ph2;

  // ── Panels ───────────────────────────────────────────────────────────────
  document.getElementById('bid-panel').style.display=isMyBid?'block':'none';
  document.getElementById('hand-panel').style.display=ph==='playing'?'block':'none';
  document.getElementById('pbrow').style.display=isMyPlay?'flex':'none';

  if (isMyBid) {
    document.getElementById('bid-max').textContent=nc;
    document.getElementById('bid-input').max=nc;
    document.getElementById('bid-input').min=0;
    document.getElementById('bid-hand').innerHTML=(s.my_hand||[]).map(c=>
      `<div class="bcard ${SC[c.s]}">${c.v}<span class="cs">${SY[c.s]}</span></div>`).join('');
    const isLast=s.bid_idx===alive.length-1;
    const noteEl=document.getElementById('bid-note');
    if (isLast) {
      const sb=Object.values(s.bids).reduce((a,b)=>a+b,0);
      noteEl.textContent=`⚠ Last bidder — cannot bid ${nc-sb} (total would equal ${nc})`;
      noteEl.style.display='block';
    } else noteEl.style.display='none';
  }

  if (ph==='playing') {
    const ti=document.getElementById('ti-hand');
    ti.textContent=isMyPlay?'Your turn!':'Waiting for '+esc(trickP||'...');
    ti.className='ti '+(isMyPlay?'mine':'wait');
    renderHand(s.my_hand||[], isMyPlay);
    if (isMyPlay) document.getElementById('btn-play').disabled=sel===null;
  }
}

function renderHand(hand, pick) {
  document.getElementById('hand-cards').innerHTML=hand.map((c,i)=>
    `<div class="hcard ${SC[c.s]}${pick?' pick':''}${pick&&sel===i?' selected':''}"
      onclick="${pick?`selH(${i})`:''}">${c.v}<span class="cs">${SY[c.s]}</span></div>`).join('');
}
function selH(i) { sel=i; renderHand(S.my_hand,true); document.getElementById('btn-play').disabled=false; }

function renderResult() {
  document.getElementById('res-title').textContent=`Round ${S.round_idx+1} results`;
  document.getElementById('res-body').innerHTML=S.round_results.map(r=>{const ok=r.diff===0;
    return `<div class="rrow"><span style="font-weight:500">${esc(r.player)}${r.player===myName?' <span class="hbadge you">you</span>':''}</span>
    <span style="color:var(--muted);font-size:12px">bid ${r.bid} · won ${r.won}</span>
    <span class="rbdg ${ok?'ok':'bad'}">${ok?'Perfect':'-'+r.diff+' life'+(r.diff!==1?'s':'')}</span></div>`;}).join('');
  document.getElementById('res-lives').innerHTML=S.players.map(p=>
    `<div class="rrow"><span>${esc(p)}${p===myName?' <span class="hbadge you">you</span>':''}</span>${lbar(S.lives[p]||0,5)}</div>`).join('');
  const isHost=S.host===myName;
  document.getElementById('res-action').innerHTML=isHost
    ?`<button class="btn btn-gold" onclick="send({action:'next_round'})">Next round →</button>`
    :`<div class="wait"><span>Waiting for host</span><span class="dot"></span></div>`;
  document.getElementById('s-result').classList.add('active');
}

function renderGameOver() {
  const alive=S.players.filter(p=>S.lives[p]>0);
  document.getElementById('go-name').textContent=alive.length===1?alive[0]:'Draw!';
  const sorted=[...S.players].sort((a,b)=>(S.lives[b]||0)-(S.lives[a]||0));
  document.getElementById('go-stands').innerHTML=sorted.map(p=>
    `<div class="rrow"><span>${esc(p)}${p===myName?' <span class="hbadge you">you</span>':''}</span>${lbar(S.lives[p]||0,5)}</div>`).join('');
  document.getElementById('go-action').innerHTML=S.host===myName
    ?`<button class="btn btn-gold" onclick="send({action:'restart'})">Play again</button>`
    :`<div class="wait"><span>Waiting for host</span><span class="dot"></span></div>`;
  document.getElementById('s-gameover').classList.add('active');
}

function cr(card,order){const vi=order.indexOf(card.v),si=['Hearts','Spades','Diamonds','Clubs'].indexOf(card.s);return(vi<0?order.length:vi)*4+si;}
function lbar(n,max){let h='<div class="lbar">';for(let i=0;i<max;i++)h+=`<span class="lh ${i<n?'a':'d'}">♥</span>`;return h+'</div>';}
function shb(n,max){let h='';for(let i=0;i<max;i++)h+=`<span class="sh ${i<n?'a':'d'}">♥</span>`;return h;}
function show(id){document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));document.getElementById(id).classList.add('active');}
let _tt;
function toast(msg,err=false){const t=document.getElementById('toast');t.textContent=msg;t.style.borderColor=err?'var(--red2)':'var(--bor2)';t.style.display='block';clearTimeout(_tt);_tt=setTimeout(()=>t.style.display='none',3500);}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
document.getElementById('inp-name').addEventListener('keydown',e=>{if(e.key==='Enter')joinGame();});
connect();
</script>
</body>
</html>
"""

async def index(request):
    resp = web.Response(text=HTML, content_type='text/html')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

async def health(request):
    return web.Response(text='ok')

def make_app():
    app = web.Application(client_max_size=1024**2)
    app.router.add_get('/', index)
    app.router.add_get('/ws', ws_handler)
    app.router.add_get('/health', health)
    return app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f'Starting on port {port}...')
    app = make_app()
    web.run_app(app, host='0.0.0.0', port=port, access_log=None)
