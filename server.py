#!/usr/bin/env python3
"""Card FDP — Multi-room Railway server."""

import asyncio, json, os, random, string
from aiohttp import web
import aiohttp

# ── Card logic ────────────────────────────────────────────────────────────────

BASE_ORDER  = ['3','2','A','K','Q','J','7','6','5','4']
SUITS       = ['Hearts','Spades','Diamonds','Clubs']
# Cycle is dynamic based on player count.
# Max cards = (40-1) // num_players (reserve 1 for trump card)
# Cycle: blind, 2..max, max-1..2, blind (repeating)

def build_cycle(num_players):
    max_nc = (40 - 1) // max(num_players, 1)
    max_nc = min(max_nc, 8)   # cap at 8 for small player counts
    up   = list(range(2, max_nc + 1))
    # If max is capped (can't reach 8), play the peak twice
    peak = [max_nc, max_nc] if max_nc < 8 else [max_nc]
    down = list(range(max_nc - 1, 1, -1))
    normal = up[:-1] + peak + down   # up without last + peak twice + down
    return [(1, True)] + [(n, False) for n in normal]

def round_num_cards(idx, num_players=2):
    cycle = build_cycle(num_players)
    return cycle[idx % len(cycle)]

def round_display_seq(round_idx, num_players=2, window=16):
    start = max(0, round_idx - 3)
    return [{'idx': i,
             'nc':  round_num_cards(i, num_players)[0],
             'blind': round_num_cards(i, num_players)[1]}
            for i in range(start, start + window)]

def build_order(trump_value):
    if trump_value not in BASE_ORDER: return BASE_ORDER[:]
    idx = BASE_ORDER.index(trump_value)
    above = BASE_ORDER[(idx - 1) % len(BASE_ORDER)]
    return [above] + [v for v in BASE_ORDER if v != above]

def card_rank(card, order):
    vi = order.index(card['v']) if card['v'] in order else len(order)
    return vi * 4 + SUITS.index(card['s'])

def build_deck():
    return [{'v': v, 's': s} for v in BASE_ORDER for s in SUITS]

def deal_cards(num_cards, players):
    deck = build_deck(); random.shuffle(deck)
    hands = {p: deck[i*num_cards:(i+1)*num_cards] for i, p in enumerate(players)}
    remaining = deck[len(players)*num_cards:]
    return hands, remaining[0] if remaining else None

# ── Room / Game state ─────────────────────────────────────────────────────────

class Room:
    def __init__(self, code, name):
        self.code     = code
        self.name     = name
        self.clients  = {}   # player_name -> ws
        self.g        = GameState()
        self.chat_log = []   # list of {player, text, ts}

    def player_count(self):
        return len(self.g.players)

    def status(self):
        return self.g.phase

class GameState:
    def __init__(self): self.reset()
    def reset(self):
        self.phase='lobby'; self.players=[]; self.lives={}
        self.eliminated=[]; self.round_idx=0; self.hands={}
        self.bids={}; self.tricks_won={}; self.current_trick=[]
        self.last_trick=[]; self.trick_num=0; self.trick_leader=0
        self.bid_order=[]; self.bid_idx=0; self.trump_card=None
        self.card_order=BASE_ORDER[:]; self.round_results=[]
        self.host=None; self.first_bidder_idx=0
    def alive_players(self):
        return [p for p in self.players if self.lives.get(p,0)>0]
    def num_players_alive(self):
        return max(len(self.alive_players()), 1)
    def num_cards(self):
        return round_num_cards(self.round_idx, self.num_players_alive())[0]
    def is_blind(self):
        return round_num_cards(self.round_idx, self.num_players_alive())[1]
    def to_public(self):
        return {
            'phase':self.phase,'players':self.players,
            'lives':{k:int(v) for k,v in self.lives.items()},
            'lives_list':[int(self.lives.get(p,0)) for p in self.players],
            'eliminated':self.eliminated,'round_idx':self.round_idx,
            'round_seq':round_display_seq(self.round_idx, self.num_players_alive()),
            'num_cards':self.num_cards(),'is_blind':self.is_blind(),
            'cycle':[(x,b) for x,b in build_cycle(self.num_players_alive())],
            'bids':self.bids,
            'tricks_won':self.tricks_won,'current_trick':self.current_trick,
            'last_trick':self.last_trick,'trick_num':self.trick_num,
            'trick_leader':self.trick_leader,'bid_idx':self.bid_idx,
            'bid_order':self.bid_order,'trump_card':self.trump_card,
            'card_order':self.card_order,'round_results':self.round_results,
            'host':self.host,
        }
    def to_player(self, name):
        pub = self.to_public()
        if self.is_blind():
            # Blind round: player sees all OTHER hands, not their own
            pub['my_hand'] = []   # own card hidden
            pub['others_hands'] = {p: self.hands.get(p,[])
                                   for p in self.alive_players() if p != name}
        else:
            pub['my_hand'] = self.hands.get(name, [])
            pub['others_hands'] = {}
        pub['my_name'] = name
        return pub

# Global rooms dict: code -> Room
ROOMS = {}

def make_code():
    while True:
        code = ''.join(random.choices(string.ascii_uppercase, k=4))
        if code not in ROOMS: return code

def get_or_create_room(code, name=None):
    if code not in ROOMS:
        ROOMS[code] = Room(code, name or f'Table {code}')
    return ROOMS[code]

# ── Room helpers ──────────────────────────────────────────────────────────────

async def broadcast_room(room):
    dead = []
    for name, ws in list(room.clients.items()):
        try:
            await ws.send_json({'type':'state','state':room.g.to_player(name),
                                'room_code':room.code,'room_name':room.name,
                                'chat_log':room.chat_log[-80:]})
        except: dead.append(name)
    for n in dead: room.clients.pop(n, None)

async def broadcast_chat(room):
    """Send only a chat update (lighter than full state)."""
    dead = []
    for name, ws in list(room.clients.items()):
        try:
            await ws.send_json({'type':'chat','chat_log':room.chat_log[-80:]})
        except: dead.append(name)
    for n in dead: room.clients.pop(n, None)

async def send_error(ws, msg):
    try: await ws.send_json({'type':'error','msg':msg})
    except: pass

def start_round(g):
    alive=g.alive_players(); nc=g.num_cards()
    g.hands, g.trump_card = deal_cards(nc, alive)
    g.card_order = build_order(g.trump_card['v']) if g.trump_card else BASE_ORDER[:]
    for p in alive: g.hands[p].sort(key=lambda c: -card_rank(c, g.card_order))
    g.bids={}; g.tricks_won={p:0 for p in alive}
    start = g.first_bidder_idx % len(alive)
    g.bid_order = alive[start:]+alive[:start]
    g.bid_idx=0; g.current_trick=[]; g.last_trick=[]
    g.trick_num=0; g.trick_leader=start; g.round_results=[]
    g.phase='bidding'

def next_trick(g):
    g.last_trick=[]; g.current_trick=[]; g.trick_num+=1

async def blind_auto_play(room):
    """In blind rounds, server reveals and plays each card automatically with delays."""
    g = room.g
    alive = g.alive_players()
    # Play cards one by one in trick order, 1.2s apart so everyone sees each reveal
    order = alive[g.trick_leader:] + alive[:g.trick_leader]
    for player in order:
        await asyncio.sleep(1.2)
        hand = g.hands.get(player, [])
        if not hand: continue
        card = hand.pop(0)
        g.current_trick.append({'player': player, 'card': card})
        await broadcast_room(room)
    # All cards on table — pause 2s then resolve
    await asyncio.sleep(2.0)
    winner_entry = min(g.current_trick, key=lambda e: card_rank(e['card'], g.card_order))
    winner = winner_entry['player']
    g.tricks_won[winner] += 1
    g.trick_leader = alive.index(winner)
    g.last_trick = list(g.current_trick)
    # Score round (blind round has only 1 trick)
    results = []
    for p in alive:
        bid = g.bids.get(p, 0); won = g.tricks_won[p]; diff = abs(bid - won)
        g.lives[p] = max(0, g.lives[p] - diff)
        if g.lives[p] == 0 and p not in g.eliminated: g.eliminated.append(p)
        results.append({'player':p,'bid':bid,'won':won,'diff':diff,'lives':int(g.lives[p])})
    g.round_results = results
    g.phase = 'round_result'
    await broadcast_room(room)

async def resolve_trick_after_delay(room):
    await asyncio.sleep(2.0)
    g = room.g
    winner_entry = min(g.current_trick, key=lambda e: card_rank(e['card'], g.card_order))
    winner = winner_entry['player']
    g.tricks_won[winner] += 1
    alive = g.alive_players()
    g.trick_leader = alive.index(winner)
    g.last_trick = list(g.current_trick)
    if g.trick_num >= g.num_cards():
        results=[]
        for p in alive:
            bid=g.bids.get(p,0); won=g.tricks_won[p]; diff=abs(bid-won)
            g.lives[p]=max(0,g.lives[p]-diff)
            if g.lives[p]==0 and p not in g.eliminated: g.eliminated.append(p)
            results.append({'player':p,'bid':bid,'won':won,'diff':diff,'lives':g.lives[p]})
        g.round_results=results; g.phase='round_result'
    else:
        next_trick(g)
    await broadcast_room(room)

# ── Lobby API ─────────────────────────────────────────────────────────────────

async def api_rooms(request):
    """Return list of rooms for the lobby page."""
    data = [{'code':r.code,'name':r.name,'players':r.player_count(),'status':r.status()}
            for r in ROOMS.values()]
    return web.json_response(data)

async def api_create_room(request):
    body = await request.json()
    name = str(body.get('name',''))[:40].strip() or 'New Table'
    code = make_code()
    get_or_create_room(code, name)
    return web.json_response({'code':code})

# ── WebSocket handler ─────────────────────────────────────────────────────────

async def ws_handler(request):
    code = request.match_info['code'].upper()
    room = get_or_create_room(code)
    g    = room.g

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
                    if name in room.clients and room.clients[name] is not ws:
                        await send_error(ws,'Name taken in this room.'); continue
                    if g.phase != 'lobby' and name not in g.players:
                        await send_error(ws,'Game already started.'); continue
                    if len(g.players)>=7 and name not in g.players:
                        await send_error(ws,'Room full (max 7).'); continue
                    player_name = name; room.clients[name] = ws
                    if name not in g.players: g.players.append(name); g.lives[name]=5
                    if g.host is None: g.host = name
                    import time
                    room.chat_log.append({'player':'','text':f'{name} joined the table','ts':int(time.time()),'system':True})
                    await broadcast_room(room)

                elif action == 'start':
                    if player_name != g.host: await send_error(ws,'Only host can start.'); continue
                    if len(g.players)<1: await send_error(ws,'Need players.'); continue
                    g.round_idx=0
                    g.first_bidder_idx=random.randrange(len(g.alive_players()))
                    start_round(g); await broadcast_room(room)

                elif action == 'bid':
                    if g.phase!='bidding': continue
                    alive=g.alive_players()
                    if g.bid_idx>=len(alive): continue
                    current_bidder=g.bid_order[g.bid_idx]
                    if player_name!=current_bidder: await send_error(ws,"Not your turn."); continue
                    try: bid=int(data['bid'])
                    except: await send_error(ws,'Invalid bid.'); continue
                    nc=g.num_cards()
                    if bid<0 or bid>nc: await send_error(ws,f'Bid 0–{nc}.'); continue
                    if g.bid_idx==len(alive)-1:
                        if sum(g.bids.values())+bid==nc:
                            await send_error(ws,f'Last bidder: cannot bid {nc-sum(g.bids.values())} (total={nc}).'); continue
                    g.bids[current_bidder]=bid; g.bid_idx+=1
                    if g.bid_idx>=len(alive):
                        g.phase='playing'; g.trick_num=0; next_trick(g)
                        await broadcast_room(room)
                        if g.is_blind():
                            # Blind round: server plays all cards automatically
                            asyncio.ensure_future(blind_auto_play(room))
                        continue
                    await broadcast_room(room)

                elif action == 'play_card':
                    if g.phase!='playing': continue
                    alive=g.alive_players()
                    turn_idx=(g.trick_leader+len(g.current_trick))%len(alive)
                    if player_name!=alive[turn_idx]: await send_error(ws,"Not your turn."); continue
                    hand=g.hands.get(player_name,[])
                    ci=data.get('card_idx')
                    if ci is None or not(0<=int(ci)<len(hand)): await send_error(ws,'Invalid card.'); continue
                    card=hand.pop(int(ci))
                    g.current_trick.append({'player':player_name,'card':card})
                    if len(g.current_trick)==len(alive):
                        await broadcast_room(room)
                        asyncio.ensure_future(resolve_trick_after_delay(room))
                    else:
                        await broadcast_room(room)

                elif action == 'next_round':
                    if player_name!=g.host: continue
                    if g.phase=='round_result':
                        g.round_idx+=1
                        g.first_bidder_idx=(g.first_bidder_idx+1)%max(1,len(g.alive_players()))
                        if len(g.alive_players())<=1: g.phase='game_over'
                        else: start_round(g)
                        await broadcast_room(room)

                elif action == 'restart':
                    if player_name!=g.host: continue
                    names=list(room.clients.keys()); g.reset()
                    g.players=names; g.lives={n:5 for n in names}
                    g.host=names[0] if names else None
                    await broadcast_room(room)

                elif action == 'chat':
                    text = str(data.get('text','')).strip()[:200]
                    if not text or not player_name: continue
                    import time
                    room.chat_log.append({'player':player_name,'text':text,'ts':int(time.time())})
                    if len(room.chat_log) > 200: room.chat_log = room.chat_log[-200:]
                    await broadcast_chat(room)

            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    except: pass
    finally:
        if player_name:
            room.clients.pop(player_name, None)
            if g.phase=='lobby':
                g.players=[p for p in g.players if p!=player_name]
                g.lives.pop(player_name,None)
                if g.host==player_name: g.host=g.players[0] if g.players else None
            # Clean up empty rooms
            if not room.clients and g.phase=='lobby':
                ROOMS.pop(room.code, None)
        await broadcast_room(room)
    return ws

# ── HTML pages ────────────────────────────────────────────────────────────────

LOBBY_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FDP — Tables</title>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;700&family=Inter:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0a;--sur:#141414;--sur2:#1e1e1e;--sur3:#272727;--bor:#2a2a2a;--bor2:#383838;
  --text:#f0ece4;--muted:#777;--dim:#3a3a3a;--gold:#c9a84c;--gold2:#f0c060;--gold-dim:rgba(201,168,76,.18);
  --green:#00b894;--red2:#e74c3c;--r:12px;--r2:8px;--r3:6px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;min-height:100vh;font-size:14px}
h1{font-family:'Cinzel',serif;font-weight:700;color:var(--gold2);font-size:2rem}
h2{font-family:'Cinzel',serif;font-weight:600;color:var(--gold);font-size:1.1rem}
.wrap{max-width:760px;margin:0 auto;padding:2rem 1rem}
.hero{text-align:center;margin-bottom:2.5rem}
.hero p{color:var(--muted);margin-top:.5rem;font-size:13px}
.suits{font-size:1.5rem;letter-spacing:.4rem;opacity:.4;margin-bottom:1rem}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.35rem;padding:.65rem 1.4rem;border-radius:var(--r3);border:1px solid var(--bor2);background:transparent;color:var(--text);cursor:pointer;font-size:13px;font-family:'Inter',sans-serif;font-weight:500;transition:all .14s}
.btn:hover{background:var(--sur3)}.btn:active{transform:scale(.97)}
.btn-gold{background:var(--gold);border-color:var(--gold);color:#111;font-weight:600}
.btn-gold:hover{background:var(--gold2)}
.inp{width:100%;background:var(--sur2);border:1px solid var(--bor2);border-radius:var(--r3);color:var(--text);padding:.65rem 1rem;font-size:14px;font-family:'Inter',sans-serif;outline:none;transition:border .2s}
.inp:focus{border-color:var(--gold)}
.section{background:var(--sur);border:1px solid var(--bor);border-radius:var(--r);padding:1.4rem;margin-bottom:1.25rem}
.section h2{margin-bottom:1rem}
.create-row{display:flex;gap:.65rem;align-items:center;flex-wrap:wrap}
.rooms-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:.75rem;margin-top:.75rem}
.room-card{background:var(--sur2);border:1px solid var(--bor2);border-radius:var(--r2);padding:1rem;cursor:pointer;transition:border-color .15s,transform .12s;text-decoration:none;display:block}
.room-card:hover{border-color:var(--gold);transform:translateY(-2px)}
.room-card-name{font-family:'Cinzel',serif;font-weight:600;color:var(--text);font-size:.95rem;margin-bottom:.35rem}
.room-card-meta{font-size:12px;color:var(--muted);display:flex;gap:.75rem;align-items:center;flex-wrap:wrap}
.status-dot{width:7px;height:7px;border-radius:50%;display:inline-block;flex-shrink:0}
.st-lobby{background:#555}.st-bidding,.st-playing{background:var(--green)}.st-round_result,.st-game_over{background:var(--red2)}
.code-badge{font-size:11px;padding:2px 8px;border-radius:20px;background:var(--gold-dim);color:var(--gold);border:1px solid rgba(201,168,76,.3);font-weight:600;letter-spacing:.05em}
.empty{color:var(--dim);font-size:13px;padding:.5rem 0}
.join-code-row{display:flex;gap:.65rem;align-items:center;flex-wrap:wrap}
#err{color:var(--red2);font-size:12px;margin-top:.4rem;display:none}
.divider{border:none;border-top:1px solid var(--bor);margin:1.25rem 0}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div class="suits">♠ ♥ ♦ ♣</div>
    <h1>FDP</h1>
    <p>Create a table or join an existing one</p>
  </div>

  <div class="section">
    <h2>Create a new table</h2>
    <div class="create-row">
      <input class="inp" id="room-name" placeholder="Table name (e.g. Friday Night)" style="flex:1;min-width:160px" maxlength="40">
      <button class="btn btn-gold" onclick="createRoom()">Create table</button>
    </div>
    <div id="err"></div>
  </div>

  <div class="section">
    <h2>Join by code</h2>
    <div class="join-code-row">
      <input class="inp" id="room-code" placeholder="4-letter code (e.g. ABCD)" style="width:180px;text-transform:uppercase" maxlength="4">
      <button class="btn btn-gold" onclick="joinByCode()">Join</button>
    </div>
  </div>

  <div class="section">
    <h2>Open tables</h2>
    <div id="rooms-grid" class="rooms-grid"><div class="empty">Loading...</div></div>
    <div style="margin-top:.75rem;text-align:right">
      <button class="btn" onclick="loadRooms()" style="font-size:12px">↻ Refresh</button>
    </div>
  </div>
</div>

<script>
async function loadRooms() {
  const res = await fetch('/api/rooms');
  const rooms = await res.json();
  const grid = document.getElementById('rooms-grid');
  if (!rooms.length) { grid.innerHTML='<div class="empty">No open tables yet — create one!</div>'; return; }
  grid.innerHTML = rooms.map(r => {
    const st = r.status;
    const stLabel = st==='lobby'?'Waiting':st==='bidding'?'Bidding':st==='playing'?'Playing':st==='round_result'?'Round end':'Game over';
    return `<a class="room-card" href="/room/${r.code}">
      <div class="room-card-name">${esc(r.name)}</div>
      <div class="room-card-meta">
        <span class="code-badge">${r.code}</span>
        <span><span class="status-dot st-${st}"></span> ${stLabel}</span>
        <span>👤 ${r.players}</span>
      </div>
    </a>`;
  }).join('');
}

async function createRoom() {
  const name = document.getElementById('room-name').value.trim() || 'New Table';
  const res = await fetch('/api/rooms', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  const data = await res.json();
  window.location.href = '/room/' + data.code;
}

function joinByCode() {
  const code = document.getElementById('room-code').value.trim().toUpperCase();
  if (code.length !== 4) { showErr('Enter a 4-letter code'); return; }
  window.location.href = '/room/' + code;
}

function showErr(msg) {
  const el=document.getElementById('err'); el.textContent=msg; el.style.display='block';
  setTimeout(()=>el.style.display='none', 3000);
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

document.getElementById('room-name').addEventListener('keydown',e=>{if(e.key==='Enter')createRoom();});
document.getElementById('room-code').addEventListener('keydown',e=>{if(e.key==='Enter')joinByCode();});
loadRooms();
setInterval(loadRooms, 5000);  // auto-refresh every 5s
</script>
</body>
</html>
"""

GAME_HTML = r"""<!DOCTYPE html>
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
.screen{display:none;min-height:100vh;flex-direction:column}.screen.active{display:flex}
#s-join{align-items:center;justify-content:center;background:radial-gradient(ellipse at center,#181818 0%,#080808 100%)}
.jcard{background:var(--sur);border:1px solid var(--bor2);border-radius:var(--r);padding:2.5rem 2rem;width:min(380px,92vw);text-align:center}
.jcard h1{font-size:1.9rem;margin-bottom:.25rem}
.jcard p{color:var(--muted);font-size:12px;margin-bottom:1.4rem}
.suits{font-size:1.3rem;margin-bottom:1.3rem;letter-spacing:.35rem;opacity:.5}
.inp{width:100%;background:var(--sur2);border:1px solid var(--bor2);border-radius:var(--r3);color:var(--text);padding:.65rem 1rem;font-size:14px;font-family:'Inter',sans-serif;outline:none;text-align:center;transition:border .2s;margin-bottom:.75rem}
.inp:focus{border-color:var(--gold)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.35rem;padding:.65rem 1.4rem;border-radius:var(--r3);border:1px solid var(--bor2);background:transparent;color:var(--text);cursor:pointer;font-size:13px;font-family:'Inter',sans-serif;font-weight:500;transition:all .14s;white-space:nowrap}
.btn:hover{background:var(--sur3)}.btn:active{transform:scale(.97)}.btn:disabled{opacity:.3;cursor:not-allowed}
.btn-gold{background:var(--gold);border-color:var(--gold);color:#111;font-weight:600}
.btn-gold:hover{background:var(--gold2)}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:.85rem 1.4rem;background:var(--sur);border-bottom:1px solid var(--bor);flex-shrink:0;gap:.6rem;flex-wrap:wrap;min-height:60px}
.tbl{display:flex;align-items:center;gap:.65rem}
.topbar h1{font-size:1.35rem;letter-spacing:.04em}
.rtag{font-size:13px;color:var(--muted);background:var(--sur2);border:1px solid var(--bor);border-radius:20px;padding:3px 12px;white-space:nowrap}
.code-tag{font-size:12px;color:var(--gold);background:var(--gold-dim);border:1px solid rgba(201,168,76,.3);border-radius:20px;padding:3px 10px;font-weight:600;letter-spacing:.06em;cursor:pointer;white-space:nowrap}
.code-tag:hover{opacity:.8}
.cdot{width:7px;height:7px;border-radius:50%;background:var(--dim);flex-shrink:0}
.cdot.on{background:var(--green)}.cdot.off{background:var(--red2)}
.seqt{display:flex;gap:3px;align-items:center}
.spip{width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:500;color:var(--dim);border:1px solid var(--bor);flex-shrink:0}
.spip.done{color:var(--muted);background:var(--sur2)}.spip.now{background:var(--gold);color:#111;border-color:var(--gold);font-weight:700}
.tbar{display:flex;align-items:center;gap:1.1rem;padding:.7rem 1.4rem;background:var(--sur2);border-bottom:1px solid var(--bor);flex-shrink:0;flex-wrap:wrap}
.tlabel{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px}
.tchip{display:inline-flex;align-items:center;gap:.3rem;background:var(--sur3);border:1px solid var(--gold);border-radius:var(--r3);padding:5px 13px;font-weight:700;font-size:1.4rem}
.opills{display:flex;gap:3px;flex-wrap:wrap}
.opill{font-size:13px;padding:3px 10px;border-radius:20px;background:var(--sur3);color:var(--muted);border:1px solid var(--bor)}
.opill.top{background:var(--gold-dim);color:var(--gold);border-color:var(--gold)}
#table-wrap{flex:1;display:flex;align-items:center;justify-content:center;padding:.75rem;position:relative;min-height:0;overflow:hidden}
.felt{position:relative;width:min(680px,96vw);aspect-ratio:1.6;border-radius:50%;
  background:radial-gradient(ellipse at 38% 38%,#1f5535 0%,#163d25 55%,#0d2810 100%);
  box-shadow:0 0 0 7px #0b1f0f,0 0 0 11px #091a0c,0 0 0 13px #07150a,0 24px 70px rgba(0,0,0,.85);
  display:flex;align-items:center;justify-content:center;flex-shrink:0}
.seat{position:absolute;display:flex;flex-direction:column;align-items:center;gap:3px;transform:translate(-50%,-50%);z-index:2}
.avatar{width:68px;height:68px;border-radius:50%;background:var(--sur);border:2px solid var(--bor2);display:flex;align-items:center;justify-content:center;font-family:'Cinzel',serif;font-size:1.1rem;font-weight:700;color:var(--gold);transition:border-color .2s,box-shadow .2s;position:relative;flex-shrink:0;cursor:default}
.avatar.my-turn{border-color:var(--gold2);box-shadow:0 0 0 3px var(--gold-dim),0 0 22px rgba(201,168,76,.25)}
.avatar.is-me{border-color:var(--green)}
.avatar.out{opacity:.3;filter:grayscale(1)}
.sname{font-size:12px;font-weight:500;color:var(--text);max-width:80px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;background:rgba(0,0,0,.6);border-radius:4px;padding:2px 7px;line-height:1.4}
.sname.is-me{color:var(--green)}
.shearts{display:flex;gap:2px;justify-content:center}
.sh{font-size:11px;line-height:1}.sh.alive{color:#e74c3c !important}.sh.dead{color:#2a2a2a !important;opacity:.4}
.bid-badge{position:absolute;top:-5px;right:-5px;background:var(--sur);border:1px solid var(--gold);border-radius:10px;font-size:10px;font-weight:600;color:var(--gold);padding:1px 5px;line-height:1.4;white-space:nowrap}
.won-badge{position:absolute;bottom:-5px;right:-5px;background:var(--green-bg);border:1px solid var(--green);border-radius:10px;font-size:10px;font-weight:600;color:var(--green);padding:1px 5px;line-height:1.4}
.pot{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;z-index:1}
.pot-inner{display:flex;flex-direction:column;align-items:center;gap:.4rem;max-width:300px}
.trick-row{display:flex;gap:5px;flex-wrap:wrap;justify-content:center}
.tc{width:58px;height:82px;border-radius:7px;background:#131313;border:1px solid #2a2a2a;display:flex;flex-direction:column;align-items:center;justify-content:center;font-weight:700;font-size:1.5rem;position:relative;flex-shrink:0}
.tc.winner-card{}
.tc .cs{font-size:.9rem;opacity:.75}
.tc.hearts{color:var(--heart)}.tc.spades{color:var(--spade)}.tc.diamonds{color:var(--diamond)}.tc.clubs{color:var(--club)}
.tc-name{font-size:11px;color:rgba(255,255,255,.45);text-align:center;max-width:56px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:3px}
.trick-entry{display:flex;flex-direction:column;align-items:center}
.pot-msg{font-size:13px;color:rgba(255,255,255,.45);font-family:'Cinzel',serif;letter-spacing:.06em;text-align:center}
.lt-label{font-size:9px;color:rgba(255,255,255,.25);text-transform:uppercase;letter-spacing:.08em;text-align:center}
#hand-panel{background:var(--sur);border-top:1px solid var(--bor);padding:.85rem 1.2rem 1.2rem;flex-shrink:0}
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:.55rem;flex-wrap:wrap;gap:.35rem}
.htitle{font-size:13px;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}
.ti{font-size:13px;font-weight:500;padding:4px 14px;border-radius:20px}
.ti.mine{background:var(--gold-dim);color:var(--gold);border:1px solid var(--gold)}
.ti.wait{background:var(--sur2);color:var(--muted);border:1px solid var(--bor)}
.cards-row{display:flex;gap:5px;flex-wrap:wrap;align-items:flex-end;justify-content:center}
.hcard{width:66px;height:94px;border-radius:8px;background:var(--sur2);border:1.5px solid var(--bor2);display:flex;flex-direction:column;align-items:center;justify-content:center;font-weight:700;font-size:1.6rem;transition:transform .11s,border-color .11s,box-shadow .11s;user-select:none;flex-shrink:0}
.hcard.pick{cursor:pointer}.hcard.pick:hover{transform:translateY(-10px)}
.hcard.selected{transform:translateY(-14px);border-color:var(--gold);box-shadow:0 0 16px var(--gold-dim)}
.hcard .cs{font-size:.95rem;opacity:.75}
.hcard.hearts{color:var(--heart)}.hcard.spades{color:var(--spade)}.hcard.diamonds{color:var(--diamond)}.hcard.clubs{color:var(--club)}
.pbrow{display:flex;align-items:center;gap:.75rem;margin-top:.55rem;justify-content:center}
#bid-panel{background:var(--sur);border-top:1px solid var(--bor);padding:.65rem 1rem;flex-shrink:0}
.bhand{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:.55rem}
.bcard{width:54px;height:76px;border-radius:6px;background:var(--sur2);border:1px solid var(--bor);display:flex;flex-direction:column;align-items:center;justify-content:center;font-weight:700;font-size:1.3rem}
.bcard .cs{font-size:.85rem;opacity:.75}
.bcard.hearts{color:var(--heart)}.bcard.spades{color:var(--spade)}.bcard.diamonds{color:var(--diamond)}.bcard.clubs{color:var(--club)}
.bidr{display:flex;align-items:center;gap:.65rem;flex-wrap:wrap}
.binp{width:72px;background:var(--sur2);border:1px solid var(--bor2);border-radius:var(--r3);color:var(--text);padding:.45rem .65rem;font-size:1.1rem;font-weight:600;font-family:'Inter',sans-serif;outline:none;text-align:center;transition:border .2s}
.binp:focus{border-color:var(--gold)}
.bnote{font-size:12px;color:var(--red2);margin-top:.4rem}
#s-lobby{background:var(--bg)}
.lwrap{max-width:680px;margin:0 auto;padding:1.25rem 1rem;width:100%}
.prow{display:flex;align-items:center;justify-content:space-between;padding:.5rem .7rem;border-bottom:1px solid var(--bor);gap:.5rem}
.prow:last-child{border-bottom:none}
.hbadge{font-size:10px;padding:2px 7px;border-radius:20px;font-weight:500}
.hbadge.host{background:var(--gold-dim);color:var(--gold);border:1px solid rgba(201,168,76,.3)}
.hbadge.you{background:var(--sur3);color:var(--muted)}
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
.lh{font-size:12px}.lh.alive{color:#e74c3c !important}.lh.dead{color:#2a2a2a !important;opacity:.4}
.gowinner{text-align:center;padding:1.4rem;background:var(--gold-dim);border:1px solid var(--gold);border-radius:var(--r);margin-bottom:1rem}
.goname{font-family:'Cinzel',serif;font-size:1.9rem;font-weight:700;color:var(--gold2)}
.wait{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:.35rem}
.dot::after{content:'...';animation:blink 1.2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
/* ── CHAT ── */
#chat-btn{position:fixed;bottom:1.4rem;right:1.2rem;width:52px;height:52px;border-radius:50%;background:var(--gold);border:none;color:#111;font-size:1.3rem;cursor:pointer;display:none;align-items:center;justify-content:center;box-shadow:0 4px 16px rgba(0,0,0,.5);z-index:50;transition:transform .15s}
#chat-btn:hover{transform:scale(1.08)}
#chat-badge{position:absolute;top:-3px;right:-3px;background:var(--red2);color:#fff;border-radius:50%;width:18px;height:18px;font-size:10px;font-weight:700;display:none;align-items:center;justify-content:center;line-height:1}
#chat-panel{position:fixed;bottom:0;right:0;width:min(320px,100vw);height:min(460px,70vh);background:var(--sur);border-left:1px solid var(--bor2);border-top:1px solid var(--bor2);border-radius:var(--r) 0 0 0;display:none;flex-direction:column;z-index:200;box-shadow:-4px -4px 24px rgba(0,0,0,.5)}
#chat-panel.open{display:flex}
.chat-head{display:flex;align-items:center;justify-content:space-between;padding:.65rem 1rem;border-bottom:1px solid var(--bor);flex-shrink:0}
.chat-head-title{font-family:'Cinzel',serif;font-size:.85rem;color:var(--gold);font-weight:600}
.chat-close{background:none;border:none;color:var(--muted);font-size:1.1rem;cursor:pointer;padding:0 .2rem;line-height:1}
.chat-close:hover{color:var(--text)}
#chat-msgs{flex:1;overflow-y:auto;padding:.6rem .85rem;display:flex;flex-direction:column;gap:.4rem;scroll-behavior:smooth}
#chat-msgs::-webkit-scrollbar{width:4px}
#chat-msgs::-webkit-scrollbar-track{background:transparent}
#chat-msgs::-webkit-scrollbar-thumb{background:var(--bor2);border-radius:2px}
.chat-msg{display:flex;flex-direction:column;gap:2px;max-width:85%}
.chat-msg.mine{align-self:flex-end;align-items:flex-end}
.chat-msg.other{align-self:flex-start}
.chat-msg.system{align-self:center;max-width:100%}
.chat-sender{font-size:10px;color:var(--muted);font-weight:500;padding:0 .3rem}
.chat-bubble{padding:.4rem .75rem;border-radius:14px;font-size:13px;line-height:1.45;word-break:break-word}
.chat-msg.mine .chat-bubble{background:var(--gold-dim);color:var(--text);border:1px solid rgba(201,168,76,.25);border-radius:14px 14px 3px 14px}
.chat-msg.other .chat-bubble{background:var(--sur2);color:var(--text);border:1px solid var(--bor);border-radius:14px 14px 14px 3px}
.chat-msg.system .chat-bubble{background:transparent;color:var(--dim);font-size:11px;font-style:italic;border:none;padding:.1rem .3rem;text-align:center}
.chat-foot{display:flex;gap:.5rem;padding:.6rem .75rem;border-top:1px solid var(--bor);flex-shrink:0}
#chat-input{flex:1;background:var(--sur2);border:1px solid var(--bor2);border-radius:20px;color:var(--text);padding:.45rem .9rem;font-size:13px;font-family:'Inter',sans-serif;outline:none;transition:border .2s}
#chat-input:focus{border-color:var(--gold)}
#chat-send{background:var(--gold);border:none;border-radius:50%;width:34px;height:34px;color:#111;font-size:1rem;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:background .15s}
#chat-send:hover{background:var(--gold2)}
#toast{position:fixed;bottom:1.4rem;left:50%;transform:translateX(-50%);background:var(--sur2);border:1px solid var(--bor2);color:var(--text);padding:.5rem 1.1rem;border-radius:var(--r3);font-size:13px;display:none;z-index:999;box-shadow:0 4px 20px rgba(0,0,0,.6);max-width:88vw;text-align:center}
@media(max-width:480px){
  .felt{aspect-ratio:1;width:min(340px,95vw)}
  .avatar{width:54px;height:54px;font-size:.9rem}
  .hcard{width:52px;height:74px;font-size:1.2rem}
  #chat-panel{width:100vw;height:55vh;border-radius:var(--r) var(--r) 0 0;border-left:none}
}
</style>
</head>
<body>

<!-- JOIN -->
<div class="screen active" id="s-join">
  <div class="jcard">
    <div class="suits">♠ ♥ ♦ ♣</div>
    <h1>FDP</h1>
    <p id="join-room-label">Joining table...</p>
    <input class="inp" id="inp-name" placeholder="Your name" maxlength="20">
    <button class="btn btn-gold" onclick="joinGame()" style="width:100%">Join table</button>
    <div id="join-status" style="font-size:12px;margin-top:.5rem;display:none;text-align:center"></div>
    <div style="margin-top:1rem"><a href="/" style="color:var(--muted);font-size:12px;text-decoration:none">← Back to tables</a></div>
  </div>
</div>

<!-- LOBBY -->
<div class="screen" id="s-lobby">
  <div class="topbar">
    <div class="tbl">
      <h1>FDP</h1>
      <span class="rtag">Lobby</span>
      <span class="code-tag" id="lobby-code-tag" onclick="copyCode()" title="Click to copy room code">CODE</span>
    </div>
    <div style="display:flex;align-items:center;gap:.45rem"><span class="cdot on" id="cdot-l"></span><span style="color:var(--muted);font-size:11px">Connected</span></div>
  </div>
  <div class="lwrap">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1.1rem;margin-bottom:1.1rem">
      <div>
        <h3 style="margin-bottom:.65rem">Players at the table</h3>
        <div style="background:var(--sur);border:1px solid var(--bor);border-radius:var(--r2)" id="lobby-list"></div>
        <div style="font-size:11px;color:var(--dim);margin-top:.45rem">Share this page's URL or the room code</div>
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
      <h1 id="game-title">FDP</h1>
      <span class="rtag" id="round-tag">Round 1</span>
      <span class="code-tag" id="game-code-tag" onclick="copyCode()" title="Click to copy">CODE</span>
      <div class="seqt" id="seq-track"></div>
    </div>
    <span class="cdot on" id="cdot-g"></span>
  </div>
  <div class="tbar">
    <div><div class="tlabel">Trump card</div><div class="tchip" id="trump-chip">—</div></div>
    <div><div class="tlabel" style="margin-bottom:3px">Card order (strongest → weakest)</div><div class="opills" id="order-pills"></div></div>
    <div style="margin-left:auto;text-align:right">
      <div class="tlabel">Trick</div>
      <div style="font-size:1.35rem;font-weight:600;font-family:'Cinzel',serif;color:var(--text)" id="trick-ctr">—</div>
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
    <div class="bnote" id="bid-note" style="display:none"></div>
    <div class="bidr">
      <div>
        <div style="font-size:11px;color:var(--muted);margin-bottom:.3rem">Tricks (0 to <span id="bid-max"></span>)</div>
        <input class="binp" type="number" id="bid-input" min="0">
      </div>
      <button class="btn btn-gold" onclick="submitBid()">Confirm bid</button>
    </div>

  </div>
  <div id="hand-panel" style="display:none">
    <div class="hdr">
      <div class="htitle" id="hand-title">Your hand</div>
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


<!-- CHAT BUTTON -->
<button id="chat-btn" onclick="toggleChat()">
  💬
  <div id="chat-badge"></div>
</button>

<!-- CHAT PANEL -->
<div id="chat-panel">
  <div class="chat-head">
    <span class="chat-head-title">Table Chat</span>
    <button class="chat-close" onclick="toggleChat()">✕</button>
  </div>
  <div id="chat-msgs"></div>
  <div class="chat-foot">
    <input id="chat-input" placeholder="Type a message..." maxlength="200">
    <button id="chat-send" onclick="sendChat()">➤</button>
  </div>
</div>

<script>
const SC={Hearts:'hearts',Spades:'spades',Diamonds:'diamonds',Clubs:'clubs'};
const SY={Hearts:'♥',Spades:'♠',Diamonds:'♦',Clubs:'♣'};
const ROOM_CODE = location.pathname.split('/').pop().toUpperCase();
let ws, myName, S, sel=null;

// Show room code & name in join screen
document.getElementById('join-room-label').textContent = `Joining table · ${ROOM_CODE}`;
['lobby-code-tag','game-code-tag'].forEach(id=>{
  const el=document.getElementById(id); if(el) el.textContent=ROOM_CODE;
});

function copyCode(){
  navigator.clipboard.writeText(ROOM_CODE).then(()=>toast('Room code copied: '+ROOM_CODE));
}

function connect(){
  const proto=location.protocol==='https:'?'wss':'ws';
  const url=`${proto}://${location.host}/ws/${ROOM_CODE}`;
  setJoinStatus('Connecting...', false);
  try{ ws=new WebSocket(url); } catch(e){ setJoinStatus('Connection failed',true); setTimeout(connect,3000); return; }
  ws.onopen =()=>{ setC(true); setJoinStatus('',false); if(myName) send({action:'join',name:myName}); };
  ws.onclose=()=>{ setC(false); setJoinStatus('Reconnecting...',false); setTimeout(connect,2000); };
  ws.onerror=()=>{ setC(false); };
  ws.onmessage=e=>{
    try{
      const m=JSON.parse(e.data);
      if(m.type==='state'){ S=m.state; if(m.chat_log) renderChat(m.chat_log); render(); }
      else if(m.type==='chat'){ renderChat(m.chat_log); }
      else if(m.type==='error') toast(m.msg,true);
    }catch(err){}
  };
}
function send(o){ if(ws&&ws.readyState===1){ ws.send(JSON.stringify(o)); return true; } return false; }
function setC(on){ ['cdot-l','cdot-g'].forEach(id=>{const el=document.getElementById(id);if(el)el.className='cdot '+(on?'on':'off');}); }
function setJoinStatus(msg,err){ const el=document.getElementById('join-status'); if(!el) return; el.textContent=msg; el.style.color=err?'var(--red2)':'var(--muted)'; el.style.display=msg?'block':'none'; }

function joinGame(){
  const n=document.getElementById('inp-name').value.trim();
  if(!n){ toast('Enter a name',true); return; }
  myName=n; showChatBtn(); if(!send({action:'join',name:n})) setJoinStatus('Connecting...',false);
}
function submitBid(){
  const v=parseInt(document.getElementById('bid-input').value);
  const nc=S.num_cards;
  if(isNaN(v)||v<0||v>nc){ toast('Bid 0–'+nc,true); return; }
  send({action:'bid',bid:v});
}
function playCard(){
  // Blind round: player has 1 hidden card, always index 0
  if(S && S.is_blind){ send({action:'play_card',card_idx:0}); sel=null; return; }
  if(sel===null){ toast('Select a card',true); return; }
  send({action:'play_card',card_idx:sel}); sel=null;
}

function seatPos(n,myIdx){
  const rx=41,ry=36;
  return Array.from({length:n},(_,i)=>{
    const angle=(2*Math.PI/n)*((i-myIdx+n)%n)+Math.PI/2;
    return{x:50+rx*Math.cos(angle),y:50+ry*Math.sin(angle)};
  });
}

function render(){
  if(!S) return;
if(!myName||!S.players.includes(myName)){ show('s-join'); return; }
  document.getElementById('s-result').classList.remove('active');
  document.getElementById('s-gameover').classList.remove('active');
  const ph=S.phase;
  if(ph==='lobby'){ renderLobby(); show('s-lobby'); }
  else{ show('s-game'); renderGame();
    if(ph==='round_result' && S.round_results && S.round_results.length>0) renderResult();
    else if(ph==='game_over') renderGameOver();
  }
}

function renderLobby(){
  const isHost=S.host===myName;
  document.getElementById('lobby-list').innerHTML=S.players.map(p=>
    `<div class="prow"><span style="font-weight:500">${esc(p)}${p===myName?' <span class="hbadge you">you</span>':''}</span>
    <div style="display:flex;align-items:center;gap:.4rem">
      ${p===S.host?'<span class="hbadge host">host</span>':''}
      ${(()=>{const idx=S.players.indexOf(p);const v=S.lives_list&&S.lives_list[idx]!==undefined?Number(S.lives_list[idx]):(S.lives[p]??5);return lbar(v,5);})()}
    </div></div>`).join('')||'<div style="padding:.7rem;color:var(--dim);font-size:12px">No players yet</div>';
  document.getElementById('lobby-action').innerHTML=isHost
    ?`<button class="btn btn-gold" onclick="send({action:'start'})">Start game</button><div style="font-size:11px;color:var(--dim);margin-top:.3rem">You are the host</div>`
    :`<div class="wait"><span>Waiting for host</span><span class="dot"></span></div>`;
}

function renderGame(){
  const s=S,ph=s.phase,nc=s.num_cards;
  const alive=s.players.filter(p=>s.lives[p]>0);
  const myIdx=alive.indexOf(myName);
  document.getElementById('round-tag').textContent=s.is_blind
    ? `Round ${s.round_idx+1} · 👁 Blind round`
    : `Round ${s.round_idx+1} · ${nc} card${nc>1?'s':''}`;
  // Show full cycle, mark position within current cycle
  if(s.cycle && s.cycle.length){
    const cycleLen = s.cycle.length;
    const posInCycle = s.round_idx % cycleLen;
    document.getElementById('seq-track').innerHTML = s.cycle.map(([nc,blind], i) => {
      const isPast   = i < posInCycle;
      const isCurrent= i === posInCycle;
      const cls = isPast ? 'done' : isCurrent ? 'now' : '';
      const label = blind ? '👁' : nc;
      return `<div class="spip ${cls}" title="${blind?'Blind round':nc+' cards'}">${label}</div>`;
    }).join('');
  } else {
    document.getElementById('seq-track').innerHTML=s.round_seq.map(r=>{
      const cls=r.idx<s.round_idx?'done':r.idx===s.round_idx?'now':'';
      return `<div class="spip ${cls}">${r.blind?'👁':r.nc}</div>`;
    }).join('');
  }
  const tc=s.trump_card;
  document.getElementById('trump-chip').innerHTML=tc?`<span class="${SC[tc.s]}">${tc.v}${SY[tc.s]}</span>`:'—';
  document.getElementById('order-pills').innerHTML=s.card_order.map((v,i)=>
    `<span class="opill${i===0?' top':''}">${v}</span>`).join('');
  document.getElementById('trick-ctr').textContent=ph==='playing'?`${s.trick_num}/${nc}`:'—';

  const bidder=ph==='bidding'?s.bid_order[s.bid_idx]:null;
  const tti=(s.trick_leader+s.current_trick.length)%Math.max(1,alive.length);
  const trickP=ph==='playing'?alive[tti]:null;
  const isMyBid=ph==='bidding'&&bidder===myName;
  const trickFull=ph==='playing'&&s.current_trick.length>=alive.length;
  const isMyPlay=ph==='playing'&&trickP===myName&&!trickFull;

  // Seats — use lives_list (indexed array) to avoid mobile dict-lookup bugs
  const felt=document.getElementById('felt');
  felt.querySelectorAll('.seat').forEach(e=>e.remove());
  // Build a reliable lives map from the ordered lives_list array
  const livesArr = s.lives_list || [];
  const getLives = (p) => {
    const idx = s.players.indexOf(p);
    return idx >= 0 && livesArr[idx] !== undefined ? Number(livesArr[idx]) : 0;
  };
  const positions=seatPos(alive.length,myIdx>=0?myIdx:0);
  alive.forEach((p,i)=>{
    const pos=positions[i],isMe=p===myName;
    const isActive=(ph==='bidding'&&bidder===p)||(ph==='playing'&&trickP===p);
    const hasBid=s.bids[p]!==undefined,won=s.tricks_won[p]??0;
    const pLives = getLives(p);
    const seat=document.createElement('div');
    seat.className='seat'; seat.style.left=pos.x+'%'; seat.style.top=pos.y+'%';
    seat.innerHTML=`
      <div class="avatar${isActive?' my-turn':''}${isMe?' is-me':''}${pLives<=0?' out':''}">
        ${p.slice(0,2).toUpperCase()}
        ${hasBid?`<div class="bid-badge">${s.bids[p]}</div>`:''}
        ${ph==='playing'?`<div class="won-badge">${won}/${s.bids[p]??'?'}</div>`:''}
      </div>
      <div class="sname${isMe?' is-me':''}">${esc(p)}</div>
      <div class="shearts">${shb(pLives,5)}</div>`;
    felt.appendChild(seat);
  });

  // Pot
  const pi=document.getElementById('pot-inner');
  let ph2='';
  if(ph==='playing'&&s.current_trick.length>0){
    let best=Infinity,winP=null;
    s.current_trick.forEach(e=>{const r=cr(e.card,s.card_order);if(r<best){best=r;winP=e.player;}});
    const full=s.current_trick.length>=alive.length;
    ph2+=`<div class="trick-row">${s.current_trick.map(e=>{const w=e.player===winP;
      return `<div class="trick-entry"><div class="tc-name">${esc(e.player)}</div>
        <div class="tc ${SC[e.card.s]}">${e.card.v}<span class="cs">${SY[e.card.s]}</span></div>
      </div>`;}).join('')}</div>`;
    if(full) ph2+=`<div class="pot-msg" style="color:var(--gold2)">${esc(winP)} wins this trick!</div>`;
    else if(trickP) ph2+=`<div class="pot-msg">${esc(trickP)}'s turn</div>`;
  }else if(ph==='playing'){
    if(s.last_trick&&s.last_trick.length){
      let best=Infinity,winP=null;
      s.last_trick.forEach(e=>{const r=cr(e.card,s.card_order);if(r<best){best=r;winP=e.player;}});
      ph2+=`<div class="lt-label">Last trick — ${esc(winP)} won</div>
        <div class="trick-row">${s.last_trick.map(e=>`<div class="tc ${SC[e.card.s]}" style="width:30px;height:43px;font-size:.78rem;opacity:.55">${e.card.v}<span class="cs">${SY[e.card.s]}</span></div>`).join('')}</div>`;
    }
    if(s.is_blind){
      ph2+=`<div class="pot-msg" style="color:var(--gold)">Revealing cards...</div>`;
    } else {
      ph2+=`<div class="pot-msg">${esc(trickP||'')} leads</div>`;
    }
  }else if(ph==='bidding'){
    const sb=Object.values(s.bids).reduce((a,b)=>a+b,0);
    ph2=`<div class="pot-msg">Bidding<br><span style="opacity:.5;font-size:10px">Total: ${sb} / ${nc}</span></div>`;
  }
  pi.innerHTML=ph2;

  // Panels
  document.getElementById('hand-panel').style.display=(ph==='bidding'||ph==='playing')?'block':'none';
  const htEl=document.getElementById('hand-title');
  if(htEl) htEl.textContent=s.is_blind?"Other players' cards (your card is face-down)":'Your hand';
  document.getElementById('bid-panel').style.display=isMyBid?'block':'none';
  // Blind round: server plays automatically, no button needed
  document.getElementById('pbrow').style.display=(isMyPlay&&!s.is_blind)?'flex':'none';
  document.getElementById('btn-play').textContent='Play selected card';

  if(ph==='bidding'){
    const ti=document.getElementById('ti-hand');
    if(isMyBid){ ti.textContent='Place your bid below'; ti.className='ti mine'; }
    else{ ti.textContent='Waiting for '+esc(s.bid_order[s.bid_idx]||'')+' to bid'; ti.className='ti wait'; }
    if(s.is_blind){ renderBlindHand(s.others_hands||{}); }
    else{ renderHand(s.my_hand||[],false); }
  }
  if(isMyBid){
    document.getElementById('bid-max').textContent=nc;
    const binp=document.getElementById('bid-input');
    binp.max=nc; binp.min=0; binp.value='';
    if(s.is_blind){
      // Show others' cards as bid reference
      let bhtml='';
      Object.entries(s.others_hands||{}).forEach(([player,cards])=>{
        bhtml+=`<div style="display:flex;flex-direction:column;align-items:center;gap:3px;margin-right:8px">
          <div style="font-size:10px;color:var(--muted)">${esc(player)}</div>
          ${cards.map(c=>`<div class="bcard ${SC[c.s]}">${c.v}<span class="cs">${SY[c.s]}</span></div>`).join('')}
        </div>`;
      });
      document.getElementById('bid-hand').innerHTML=bhtml||'<span style="color:var(--dim);font-size:12px">Waiting for cards...</span>';
    } else {
      document.getElementById('bid-hand').innerHTML=(s.my_hand||[]).map(c=>
        `<div class="bcard ${SC[c.s]}">${c.v}<span class="cs">${SY[c.s]}</span></div>`).join('');
    }
    const isLast=s.bid_idx===alive.length-1;
    const noteEl=document.getElementById('bid-note');
    if(isLast){ const sb=Object.values(s.bids).reduce((a,b)=>a+b,0);
      noteEl.textContent=`⚠ Last bidder — cannot bid ${nc-sb} (total would equal ${nc})`;
      noteEl.style.display='block';
    }else noteEl.style.display='none';
  }
  if(ph==='playing'){
    const ti=document.getElementById('ti-hand');
    if(s.is_blind){
      ti.textContent='Cards are being revealed...'; ti.className='ti wait';
      renderBlindHand(s.others_hands||{});
    } else {
      ti.textContent=isMyPlay?'Your turn!':'Waiting for '+esc(trickP||'...');
      ti.className='ti '+(isMyPlay?'mine':'wait');
      renderHand(s.my_hand||[],isMyPlay);
      if(isMyPlay) document.getElementById('btn-play').disabled=sel===null;
    }
  }
}

function renderHand(hand,pick){
  document.getElementById('hand-cards').innerHTML=hand.map((c,i)=>
    `<div class="hcard ${SC[c.s]}${pick?' pick':''}${pick&&sel===i?' selected':''}"
      onclick="${pick?`selH(${i})`:''}">${c.v}<span class="cs">${SY[c.s]}</span></div>`).join('');
}
function selH(i){ sel=i; renderHand(S.my_hand,true); document.getElementById('btn-play').disabled=false; }

function renderBlindHand(othersHands){
  // Show each other player's card(s) labelled, plus a face-down card for self
  const ph=S.phase; const nc=S.num_cards;
  let html='<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-end;justify-content:center">';
  // Face-down card for self
  html+=`<div style="display:flex;flex-direction:column;align-items:center;gap:4px">
    <div style="font-size:11px;color:var(--green);font-weight:500">You</div>
    <div class="hcard" style="cursor:default;background:#111;border-style:dashed;border-color:#444;color:#333;font-size:2rem">?</div>
  </div>`;
  // Other players' visible cards
  Object.entries(othersHands).forEach(([player, cards])=>{
    html+=`<div style="display:flex;flex-direction:column;align-items:center;gap:4px">
      <div style="font-size:11px;color:var(--muted);font-weight:500">${esc(player)}</div>
      <div style="display:flex;gap:4px">
        ${cards.map(c=>`<div class="hcard ${SC[c.s]}" style="cursor:default">${c.v}<span class="cs">${SY[c.s]}</span></div>`).join('')}
      </div>
    </div>`;
  });
  html+='</div>';
  document.getElementById('hand-cards').innerHTML=html;
}

function renderResult(){
  document.getElementById('res-title').textContent=`Round ${S.round_idx+1} results`;
  // Use lives_list (server-sent ordered array) as the single source of truth
  const getLivesForResult = (p) => {
    const idx = S.players.indexOf(p);
    // Primary: lives_list (guaranteed int array from server)
    if(S.lives_list && S.lives_list[idx] !== undefined) return Number(S.lives_list[idx]);
    // Fallback: round_results r.lives
    const rr = S.round_results.find(r=>r.player===p);
    if(rr) return Number(rr.lives);
    return 0;
  };
  document.getElementById('res-body').innerHTML=S.round_results.map(r=>{const ok=r.diff===0;
    return `<div class="rrow"><span style="font-weight:500">${esc(r.player)}${r.player===myName?' <span class="hbadge you">you</span>':''}</span>
    <span style="color:var(--muted);font-size:12px">bid ${r.bid} · won ${r.won}</span>
    <span class="rbdg ${ok?'ok':'bad'}">${ok?'Perfect':'-'+r.diff+' life'+(r.diff!==1?'s':'')}</span></div>`;}).join('');
  document.getElementById('res-lives').innerHTML=S.players.map(p=>{
    const v = getLivesForResult(p);
    return `<div class="rrow"><span>${esc(p)}${p===myName?' <span class="hbadge you">you</span>':''}</span>${lbar(v,5)}</div>`;
  }).join('');
  const isHost=S.host===myName;
  document.getElementById('res-action').innerHTML=isHost
    ?`<button class="btn btn-gold" onclick="send({action:'next_round'})">Next round →</button>`
    :`<div class="wait"><span>Waiting for host</span><span class="dot"></span></div>`;
  document.getElementById('s-result').classList.add('active');
}
function renderGameOver(){
  const alive=S.players.filter(p=>S.lives[p]>0);
  document.getElementById('go-name').textContent=alive.length===1?alive[0]:'Draw!';
  const sorted=[...S.players].sort((a,b)=>(S.lives[b]||0)-(S.lives[a]||0));
  document.getElementById('go-stands').innerHTML=sorted.map(p=>{
    const pi=S.players.indexOf(p);
    const v=S.lives_list&&S.lives_list[pi]!==undefined?Number(S.lives_list[pi]):0;
    return `<div class="rrow"><span>${esc(p)}${p===myName?' <span class="hbadge you">you</span>':''}</span>${lbar(v,5)}</div>`;}).join('');
  document.getElementById('go-action').innerHTML=S.host===myName
    ?`<button class="btn btn-gold" onclick="send({action:'restart'})">Play again</button>`
    :`<div class="wait"><span>Waiting for host</span><span class="dot"></span></div>`;
  document.getElementById('s-gameover').classList.add('active');
}

function cr(card,order){const vi=order.indexOf(card.v),si=['Hearts','Spades','Diamonds','Clubs'].indexOf(card.s);return(vi<0?order.length:vi)*4+si;}
function lbar(n,max){n=Number(n??0);let h='<div class="lbar">';for(let i=0;i<max;i++)h+=`<span class="lh ${i<n?'alive':'dead'}">♥</span>`;return h+'</div>';}
function shb(n,max){n=Number(n??0);let h='';for(let i=0;i<max;i++)h+=`<span class="sh ${i<n?'alive':'dead'}">♥</span>`;return h;}
function show(id){document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));document.getElementById(id).classList.add('active');}
let _tt;
function toast(msg,err=false){const t=document.getElementById('toast');t.textContent=msg;t.style.borderColor=err?'var(--red2)':'var(--bor2)';t.style.display='block';clearTimeout(_tt);_tt=setTimeout(()=>t.style.display='none',3500);}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
document.getElementById('inp-name').addEventListener('keydown',e=>{if(e.key==='Enter')joinGame();});

// ── Chat ────────────────────────────────────────────────────────────────────
let chatOpen=false, unread=0, lastChatLen=0;

function toggleChat(){
  chatOpen=!chatOpen;
  document.getElementById('chat-panel').classList.toggle('open',chatOpen);
  if(chatOpen){ unread=0; updateBadge(); scrollChat(); document.getElementById('chat-input').focus(); }
}

function sendChat(){
  const inp=document.getElementById('chat-input');
  const text=inp.value.trim();
  if(!text) return;
  send({action:'chat',text});
  inp.value='';
}

document.getElementById('chat-input').addEventListener('keydown',e=>{if(e.key==='Enter')sendChat();});

function renderChat(log){
  if(!log||!log.length) return;
  if(log.length===lastChatLen) return;
  const newMsgs=log.length-lastChatLen;
  lastChatLen=log.length;
  const el=document.getElementById('chat-msgs');
  el.innerHTML=log.map(m=>{
    if(m.system) return `<div class="chat-msg system"><div class="chat-bubble">${esc(m.text)}</div></div>`;
    const isMe=m.player===myName;
    return `<div class="chat-msg ${isMe?'mine':'other'}">
      ${!isMe?`<div class="chat-sender">${esc(m.player)}</div>`:''}
      <div class="chat-bubble">${esc(m.text)}</div>
    </div>`;
  }).join('');
  scrollChat();
  if(!chatOpen && newMsgs>0){
    unread+=newMsgs; updateBadge();
  }
}

function scrollChat(){
  const el=document.getElementById('chat-msgs');
  el.scrollTop=el.scrollHeight;
}

function updateBadge(){
  const badge=document.getElementById('chat-badge');
  if(unread>0){ badge.textContent=unread>9?'9+':unread; badge.style.display='flex'; }
  else badge.style.display='none';
}

// Show chat button once logged in
function showChatBtn(){
  const btn=document.getElementById('chat-btn');
  btn.style.display='flex';
}

connect();
</script>
</body>
</html>
"""

# ── HTTP routes ───────────────────────────────────────────────────────────────

async def index(request):
    resp = web.Response(text=LOBBY_HTML, content_type='text/html')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

async def game_page(request):
    resp = web.Response(text=GAME_HTML, content_type='text/html')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

async def health(request):
    return web.Response(text='ok')

def make_app():
    app = web.Application(client_max_size=1024**2)
    app.router.add_get('/',             index)
    app.router.add_get('/room/{code}',  game_page)
    app.router.add_get('/ws/{code}',    ws_handler)
    app.router.add_get('/api/rooms',    api_rooms)
    app.router.add_post('/api/rooms',   api_create_room)
    app.router.add_get('/health',       health)
    return app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f'Starting on port {port}...')
    web.run_app(make_app(), host='0.0.0.0', port=port, access_log=None)
