"""
FPS Arena — backend.

Three modes:
  • bots          — single-player (no server-side state).
  • pvp (1v1)     — two players, single opponent (legacy).
  • team (10v10)  — up to 20 players, split into two teams of up to 10.

Networking: WebSockets only, JSON messages. State is in-memory and ephemeral.
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import json
import os
import uuid
import asyncio
import random
import time

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Serve user-provided gunshot samples and any other static assets.
SOUNDS_DIR = os.path.join(BASE_DIR, "sounds")
os.makedirs(SOUNDS_DIR, exist_ok=True)
app.mount("/sounds", StaticFiles(directory=SOUNDS_DIR), name="sounds")

ROOM_TTL = 3600                # 1 hour max room lifetime
TEAM_MAX = 10                  # max players per team
TEAM_RESPAWN_SECS = int(os.environ.get("TEAM_RESPAWN_SECS", "5"))
MATCH_KILL_LIMIT  = int(os.environ.get("MATCH_KILL_LIMIT", "50"))
MATCH_TIME_LIMIT = int(os.environ.get("MATCH_TIME_LIMIT", "600"))
RECONNECT_TTL = int(os.environ.get("RECONNECT_TTL", "60"))

# --- In-memory state ----------------------------------------------------------

# 1v1 PvP lobby (legacy — kept identical to v1).
lobby: dict[str, WebSocket] = {}      # nickname -> ws (1v1 lobby)
player_rooms: dict[str, str] = {}     # nickname -> room_id (in a 1v1 game)

# Team-mode pre-game lobbies and active rooms.
team_lobby_players: dict[str, "TeamLobbyPlayer"] = {}   # nick -> player record
team_player_rooms: dict[str, str] = {}                  # nick -> team room id

# A single open team lobby ID at a time (new joiners go here). When the
# game starts, this ID is cleared and the next joiner spawns a fresh one.
current_team_lobby_id: str | None = None


# --- 1v1 PvP Room -------------------------------------------------------------

class Room:
    __slots__ = (
        "id", "p1_nick", "p2_nick", "p1_ws", "p2_ws",
        "p1_spawn", "p2_spawn", "scores", "created", "round_active",
        "dying",
    )

    def __init__(self, rid: str, p1: str, p2: str):
        self.id = rid
        self.p1_nick = p1
        self.p2_nick = p2
        self.p1_ws: WebSocket | None = None
        self.p2_ws: WebSocket | None = None
        self.p1_spawn: int = random.randint(0, 1)
        self.p2_spawn: int = 1 - self.p1_spawn
        self.scores: dict[str, int] = {p1: 0, p2: 0}
        self.created: float = time.time()
        self.round_active: bool = True
        # Tracks who has already submitted a "died" for the current round.
        # Without this, follow-up bullets/grenade splash that land while
        # the client is still processing the death animation produce a
        # SECOND "died" message and inflate the killer's score (+2, +3).
        self.dying: set[str] = set()

    def opponent_ws(self, nick: str) -> WebSocket | None:
        return self.p2_ws if nick == self.p1_nick else self.p1_ws

    def opponent_nick(self, nick: str) -> str:
        return self.p2_nick if nick == self.p1_nick else self.p1_nick


rooms: dict[str, Room] = {}


# --- Team Mode ----------------------------------------------------------------

class TeamLobbyPlayer:
    __slots__ = ("nick", "ws", "team", "lobby_id", "joined")

    def __init__(self, nick: str, ws: WebSocket, team: str, lobby_id: str):
        self.nick = nick
        self.ws = ws
        self.team = team           # "A" or "B"
        self.lobby_id = lobby_id
        self.joined = time.time()


class TeamLobby:
    """Pre-game gathering room. Auto-assigns each joiner to the smaller team
    (ties → 'A'). Anyone in the lobby can press START once BOTH teams have
    at least one player; we then promote the lobby into a `TeamRoom`."""

    __slots__ = ("id", "members", "created", "started")

    def __init__(self, lid: str):
        self.id: str = lid
        self.members: dict[str, TeamLobbyPlayer] = {}
        self.created: float = time.time()
        self.started: bool = False

    def team_size(self, team: str) -> int:
        return sum(1 for p in self.members.values() if p.team == team)

    def has_room_for(self, team: str) -> bool:
        return self.team_size(team) < TEAM_MAX

    def assign_team(self) -> str | None:
        """Pick the team with fewer players. Returns None if the lobby is full."""
        a = self.team_size("A")
        b = self.team_size("B")
        if a >= TEAM_MAX and b >= TEAM_MAX:
            return None
        if a >= TEAM_MAX:
            return "B"
        if b >= TEAM_MAX:
            return "A"
        return "A" if a <= b else "B"

    def can_start(self) -> bool:
        # Each team must have at least 1 player. Even 1v1 is allowed.
        return self.team_size("A") >= 1 and self.team_size("B") >= 1

    def snapshot(self) -> dict:
        teamA = [p.nick for p in self.members.values() if p.team == "A"]
        teamB = [p.nick for p in self.members.values() if p.team == "B"]
        return {
            "lobby_id": self.id,
            "teamA": teamA,
            "teamB": teamB,
            "can_start": self.can_start(),
            "max_per_team": TEAM_MAX,
            "started": self.started,
        }


class TeamRoom:
    """Active team match. Players are identified by nickname; team membership
    is fixed at match start. Tracks per-player kills (= leaderboard) plus a
    team kill total."""

    __slots__ = (
        "id", "members", "team_of", "scores", "team_scores",
        "created", "round_active", "spawns", "dying",
        "disconnected_at", "started_at", "ended", "winner",
    )

    def __init__(self, rid: str, lobby: TeamLobby):
        self.id: str = rid
        # nick -> ws (None until the client opens its game-page WS).
        self.members: dict[str, WebSocket | None] = {}
        # nick -> "A"/"B"
        self.team_of: dict[str, str] = {}
        # nick -> spawn index within their team (0..4)
        self.spawns: dict[str, int] = {}
        # nick -> personal kill count (leaderboard)
        self.scores: dict[str, int] = {}
        self.team_scores: dict[str, int] = {"A": 0, "B": 0}
        # nicks currently in their death/respawn timer — guards against
        # follow-up damage producing multiple +1 increments per kill.
        self.dying: set[str] = set()
        self.created: float = time.time()
        self.round_active: bool = True
        # nick -> timestamp when their ws went None (used for reconnect TTL).
        self.disconnected_at: dict[str, float] = {}
        # Wall-clock match start (set once at construction; used for time limit).
        self.started_at: float = time.time()
        # When True, the match has hit its kill limit or timer — no more kills
        self.ended: bool = False
        self.winner: str | None = None

        # Assign spawn slots per team (0..4).
        a_idx = 0
        b_idx = 0
        for nick, p in lobby.members.items():
            self.members[nick] = None
            self.team_of[nick] = p.team
            self.scores[nick] = 0
            if p.team == "A":
                self.spawns[nick] = a_idx
                a_idx += 1
            else:
                self.spawns[nick] = b_idx
                b_idx += 1

    def team_size(self, team: str) -> int:
        return sum(1 for t in self.team_of.values() if t == team)

    def is_match_over(self) -> tuple[bool, str | None]:
        """Check whether the match should end now. Returns (over, winner)
        where winner is 'A', 'B', 'DRAW' or None (not over yet)."""
        if self.ended:
            return True, self.winner
        a = self.team_scores.get("A", 0)
        b = self.team_scores.get("B", 0)
        if a >= MATCH_KILL_LIMIT and a > b:
            return True, "A"
        if b >= MATCH_KILL_LIMIT and b > a:
            return True, "B"
        if time.time() - self.started_at >= MATCH_TIME_LIMIT:
            if a > b: return True, "A"
            if b > a: return True, "B"
            return True, "DRAW"
        return False, None

    def add_player(self, nick: str, ws=None) -> str | None:
        """Add a new player to the room mid-match. Returns the assigned team
        ('A' or 'B'), or None if both teams are at TEAM_MAX. Auto-balances:
        smaller team wins; tie → 'A'."""
        if nick in self.team_of:
            # Already in the room — just refresh the socket.
            if ws is not None:
                self.members[nick] = ws
            return self.team_of[nick]
        size_a = self.team_size("A")
        size_b = self.team_size("B")
        if size_a >= TEAM_MAX and size_b >= TEAM_MAX:
            return None
        if size_a <= size_b and size_a < TEAM_MAX:
            team = "A"
        elif size_b < TEAM_MAX:
            team = "B"
        else:
            team = "A"
        # Next free spawn index for that team (0..TEAM_MAX-1).
        used = {self.spawns[n] for n, t in self.team_of.items() if t == team}
        spawn_idx = 0
        while spawn_idx in used and spawn_idx < TEAM_MAX:
            spawn_idx += 1
        self.team_of[nick] = team
        self.spawns[nick] = spawn_idx
        self.scores[nick] = 0
        self.members[nick] = ws
        return team

    def teammates(self, nick: str) -> list[str]:
        t = self.team_of.get(nick)
        if t is None:
            return []
        return [n for n, tt in self.team_of.items() if tt == t and n != nick]

    def opponents(self, nick: str) -> list[str]:
        t = self.team_of.get(nick)
        if t is None:
            return []
        return [n for n, tt in self.team_of.items() if tt != t]

    def all_other_ws(self, nick: str) -> list[WebSocket]:
        out = []
        for n, ws in self.members.items():
            if n != nick and ws is not None:
                out.append(ws)
        return out

    def snapshot(self) -> dict:
        roster = []
        for nick, team in self.team_of.items():
            roster.append({
                "nick": nick,
                "team": team,
                "kills": self.scores.get(nick, 0),
                "alive": nick not in self.dying,
                "spawn": self.spawns.get(nick, 0),
            })
        return {
            "room": self.id,
            "roster": roster,
            "team_scores": self.team_scores,
        }


team_lobbies: dict[str, TeamLobby] = {}
team_rooms: dict[str, TeamRoom] = {}


# --- Helpers ------------------------------------------------------------------

async def _safe_send(ws: WebSocket | None, data: dict) -> None:
    if ws is None:
        return
    try:
        await ws.send_text(json.dumps(data))
    except Exception:
        pass


async def _broadcast(ws_list, data: dict) -> None:
    if not ws_list:
        return
    payload = json.dumps(data)
    for ws in ws_list:
        try:
            await ws.send_text(payload)
        except Exception:
            pass


def _get_or_create_open_team_lobby() -> TeamLobby:
    """Return the currently-open team lobby, creating one if needed."""
    global current_team_lobby_id
    if current_team_lobby_id and current_team_lobby_id in team_lobbies:
        lobby = team_lobbies[current_team_lobby_id]
        if not lobby.started and (lobby.team_size("A") + lobby.team_size("B")) < 2 * TEAM_MAX:
            return lobby
    lid = uuid.uuid4().hex[:8]
    new_lobby = TeamLobby(lid)
    team_lobbies[lid] = new_lobby
    current_team_lobby_id = lid
    return new_lobby


async def _broadcast_lobby(lobby: TeamLobby) -> None:
    snap = lobby.snapshot()
    snap["type"] = "team_lobby_state"
    ws_list = [p.ws for p in lobby.members.values() if p.ws is not None]
    await _broadcast(ws_list, snap)


async def _broadcast_team_room(room: TeamRoom, msg_type: str = "team_room_state") -> None:
    snap = room.snapshot()
    snap["type"] = msg_type
    await _broadcast(list(room.all_other_ws("__none__")) + [], snap)
    # Also send to all members
    ws_list = [ws for ws in room.members.values() if ws is not None]
    await _broadcast(ws_list, snap)


# --- Routes -------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.get("/game")
async def game_page():
    return FileResponse(os.path.join(BASE_DIR, "game.html"))


@app.post("/__test_reset")
async def _test_reset():
    """Test-only: wipe all in-memory lobby/room state so an automated test
    suite starts each case from a clean slate. Disabled unless the
    ENABLE_TEST_RESET env flag is set, so it is inert in production."""
    if os.environ.get("ENABLE_TEST_RESET") != "1":
        return {"ok": False, "disabled": True}
    global current_team_lobby_id
    lobby.clear()
    rooms.clear()
    player_rooms.clear()
    team_lobbies.clear()
    team_rooms.clear()
    team_lobby_players.clear()
    team_player_rooms.clear()
    current_team_lobby_id = None
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    nickname: str | None = None
    # 1v1 game room (legacy)
    room: Room | None = None
    # Team lobby + team game room
    in_team_lobby_id: str | None = None
    team_room: TeamRoom | None = None

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            t = data.get("type")

            # ===== LOBBY: 1v1 =====================================================
            if t == "register":
                nick = data.get("nickname", "").strip()[:24]
                if not nick:
                    await _safe_send(ws, {"type": "error", "msg": "Пустой никнейм"})
                    continue
                if nick in lobby or nick in player_rooms or nick in team_lobby_players or nick in team_player_rooms:
                    await _safe_send(ws, {"type": "error", "msg": "Ник занят"})
                    continue
                if nickname and nickname in lobby:
                    del lobby[nickname]
                nickname = nick
                lobby[nickname] = ws
                await _safe_send(ws, {"type": "registered", "nickname": nickname})

            elif t == "find":
                target = data.get("target", "").strip()[:24]
                if not nickname:
                    await _safe_send(ws, {"type": "error", "msg": "Сначала введите ник"})
                    continue
                if target == nickname:
                    await _safe_send(ws, {"type": "error", "msg": "Нельзя играть с самим собой"})
                    continue
                if target not in lobby:
                    await _safe_send(ws, {"type": "not_found", "target": target})
                    continue

                target_ws = lobby[target]
                room_id = uuid.uuid4().hex[:8]
                r = Room(room_id, nickname, target)
                rooms[room_id] = r

                lobby.pop(nickname, None)
                lobby.pop(target, None)
                player_rooms[nickname] = room_id
                player_rooms[target] = room_id

                await _safe_send(target_ws, {
                    "type": "matched", "room": room_id,
                    "spawn": r.p2_spawn, "opponent": nickname,
                })
                await _safe_send(ws, {
                    "type": "matched", "room": room_id,
                    "spawn": r.p1_spawn, "opponent": target,
                })

            # ===== LOBBY: TEAM ===================================================
            elif t == "team_join":
                nick = data.get("nickname", "").strip()[:24]
                if not nick:
                    await _safe_send(ws, {"type": "error", "msg": "Пустой никнейм"})
                    continue

                # === RECONNECT: same-nick player whose match is still alive
                # and whose slot is currently disconnected (ws=None). Restore
                # them in place — same team, same spawn, same kill count.
                rid_recon = team_player_rooms.get(nick)
                if rid_recon and rid_recon in team_rooms:
                    tr_recon = team_rooms[rid_recon]
                    if (nick in tr_recon.team_of
                            and tr_recon.members.get(nick) is None
                            and not tr_recon.ended):
                        tr_recon.members[nick] = ws
                        tr_recon.disconnected_at.pop(nick, None)
                        nickname = nick
                        team_room = None  # game-page WS not opened yet — that
                                          # happens in team_join_room below.
                        roster_snap = [
                            {"nick": n, "team": tt, "kills": tr_recon.scores.get(n, 0)}
                            for n, tt in tr_recon.team_of.items()
                        ]
                        await _safe_send(ws, {
                            "type": "team_match_start",
                            "room": rid_recon,
                            "team": tr_recon.team_of[nick],
                            "spawn": tr_recon.spawns.get(nick, 0),
                            "nickname": nick,
                            "roster": roster_snap,
                            "reconnect": True,
                            "scores": tr_recon.scores,
                            "team_scores": tr_recon.team_scores,
                        })
                        await _broadcast_team_room(tr_recon)
                        continue

                if nick in lobby or nick in player_rooms or nick in team_lobby_players or nick in team_player_rooms:
                    await _safe_send(ws, {"type": "error", "msg": "Ник занят"})
                    continue

                # === LATE JOIN: try to drop the player into an already-active
                # match before falling back to the lobby. Find a room with
                # round_active==True and < 2*TEAM_MAX players.
                joined_active = False
                for rid, tr in list(team_rooms.items()):
                    if time.time() - tr.created > ROOM_TTL:
                        continue
                    if not tr.round_active or tr.ended:
                        continue
                    total = len(tr.team_of)
                    if total >= 2 * TEAM_MAX:
                        continue
                    # Skip ghost rooms — no point joining a match where
                    # everyone has disconnected.
                    alive = sum(1 for w in tr.members.values() if w is not None)
                    if alive == 0:
                        continue
                    assigned = tr.add_player(nick, ws=None)
                    if assigned is None:
                        continue
                    nickname = nick
                    team_room = None  # set after team_join_room (game page WS)
                    team_player_rooms[nick] = rid
                    # Send the same payload the lobby-start path sends so the
                    # client can redirect into /game?mode=team.
                    await _safe_send(ws, {
                        "type": "team_match_start",
                        "room": rid,
                        "team": assigned,
                        "spawn": tr.spawns[nick],
                        "nickname": nick,
                        "roster": [
                            {"nick": m, "team": tr.team_of[m]}
                            for m in tr.team_of
                        ],
                        "late_join": True,
                    })
                    # Tell the existing players about the new roster entry
                    # so they can pre-allocate an avatar slot. The game-page
                    # WS for the late joiner connects shortly after.
                    await _broadcast_team_room(tr)
                    joined_active = True
                    break
                if joined_active:
                    continue

                tlobby = _get_or_create_open_team_lobby()
                team = tlobby.assign_team()
                if team is None:
                    await _safe_send(ws, {"type": "error", "msg": "Лобби заполнено, попробуйте позже"})
                    continue
                nickname = nick
                in_team_lobby_id = tlobby.id
                player = TeamLobbyPlayer(nick, ws, team, tlobby.id)
                tlobby.members[nick] = player
                team_lobby_players[nick] = player
                await _safe_send(ws, {
                    "type": "team_joined",
                    "nickname": nick,
                    "team": team,
                    "lobby_id": tlobby.id,
                })
                await _broadcast_lobby(tlobby)

            elif t == "team_start":
                # Any lobby member can hit start; require both teams non-empty.
                if not nickname or not in_team_lobby_id:
                    await _safe_send(ws, {"type": "error", "msg": "Вы не в командном лобби"})
                    continue
                tlobby = team_lobbies.get(in_team_lobby_id)
                if tlobby is None or tlobby.started:
                    await _safe_send(ws, {"type": "error", "msg": "Лобби закрыто"})
                    continue
                if not tlobby.can_start():
                    await _safe_send(ws, {
                        "type": "error",
                        "msg": "Нужен минимум 1 игрок в каждой команде",
                    })
                    continue
                # Promote lobby → room.
                room_id = uuid.uuid4().hex[:8]
                troom = TeamRoom(room_id, tlobby)
                team_rooms[room_id] = troom
                tlobby.started = True
                # Notify everyone in lobby to redirect.
                for p_nick, p in list(tlobby.members.items()):
                    team_lobby_players.pop(p_nick, None)
                    team_player_rooms[p_nick] = room_id
                    await _safe_send(p.ws, {
                        "type": "team_match_start",
                        "room": room_id,
                        "team": p.team,
                        "spawn": troom.spawns[p_nick],
                        "nickname": p_nick,
                        "roster": [
                            {"nick": m, "team": troom.team_of[m]}
                            for m in troom.team_of
                        ],
                    })
                # The current open-lobby slot is consumed; new joins get a
                # fresh lobby next time.
                global current_team_lobby_id
                if current_team_lobby_id == tlobby.id:
                    current_team_lobby_id = None
                # Clean the lobby out of memory shortly.
                team_lobbies.pop(tlobby.id, None)

            elif t == "team_leave_lobby":
                if nickname and in_team_lobby_id:
                    tlobby = team_lobbies.get(in_team_lobby_id)
                    if tlobby is not None:
                        tlobby.members.pop(nickname, None)
                        team_lobby_players.pop(nickname, None)
                        await _broadcast_lobby(tlobby)
                        # If lobby is now empty, drop it.
                        if not tlobby.members:
                            team_lobbies.pop(tlobby.id, None)
                            if current_team_lobby_id == tlobby.id:
                                current_team_lobby_id = None
                    in_team_lobby_id = None
                    nickname = None

            # ===== IN-GAME: 1v1 ==================================================
            elif t == "join_room":
                rid = data.get("room", "")
                nick = data.get("nickname", "")
                # Clean stale 1v1 rooms.
                stale = [k for k, v in rooms.items() if time.time() - v.created > ROOM_TTL]
                for k in stale:
                    rooms.pop(k, None)
                if rid not in rooms:
                    await _safe_send(ws, {"type": "error", "msg": "Комната не найдена"})
                    continue
                r = rooms[rid]
                room = r
                nickname = nick
                if nick == r.p1_nick:
                    r.p1_ws = ws
                elif nick == r.p2_nick:
                    r.p2_ws = ws
                else:
                    await _safe_send(ws, {"type": "error", "msg": "Вы не в этой комнате"})
                    room = None
                    continue

                if r.p1_ws and r.p2_ws:
                    r.round_active = True
                    r.dying.clear()
                    await _safe_send(r.p1_ws, {
                        "type": "game_start", "spawn": r.p1_spawn,
                        "opponent": r.p2_nick, "scores": r.scores,
                    })
                    await _safe_send(r.p2_ws, {
                        "type": "game_start", "spawn": r.p2_spawn,
                        "opponent": r.p1_nick, "scores": r.scores,
                    })
                else:
                    await _safe_send(ws, {"type": "waiting_opponent"})

            elif t == "state":
                if room:
                    opp = room.opponent_ws(nickname or "")
                    data["type"] = "opponent_state"
                    await _safe_send(opp, data)

            elif t == "shot":
                if room:
                    opp = room.opponent_ws(nickname or "")
                    data["type"] = "opponent_shot"
                    await _safe_send(opp, data)

            elif t == "hit":
                if room and room.round_active and nickname not in room.dying:
                    opp = room.opponent_ws(nickname or "")
                    data["type"] = "took_damage"
                    await _safe_send(opp, data)

            elif t == "died":
                if room and nickname:
                    # GUARD against double-counting. If the dying player has
                    # already sent a 'died' for this round, ignore extras —
                    # this is the +2 / +3 score bug fix.
                    if nickname in room.dying or not room.round_active:
                        pass
                    else:
                        room.dying.add(nickname)
                        killer = room.opponent_nick(nickname)
                        if killer in room.scores:
                            room.scores[killer] += 1

                        room.p1_spawn = 1 - room.p1_spawn
                        room.p2_spawn = 1 - room.p2_spawn
                        room.round_active = False

                        death_msg = {
                            "type": "round_over",
                            "killed": nickname, "killer": killer,
                            "scores": room.scores,
                        }
                        await _safe_send(room.p1_ws, death_msg)
                        await _safe_send(room.p2_ws, death_msg)

                        async def _respawn(r: Room = room):
                            await asyncio.sleep(TEAM_RESPAWN_SECS)
                            if r.id not in rooms:
                                return
                            r.round_active = True
                            r.dying.clear()
                            await _safe_send(r.p1_ws, {
                                "type": "respawn", "spawn": r.p1_spawn,
                                "scores": r.scores,
                            })
                            await _safe_send(r.p2_ws, {
                                "type": "respawn", "spawn": r.p2_spawn,
                                "scores": r.scores,
                            })

                        asyncio.create_task(_respawn())

            elif t == "grenade_throw":
                if room:
                    opp = room.opponent_ws(nickname or "")
                    data["type"] = "opponent_grenade"
                    await _safe_send(opp, data)

            # ===== IN-GAME: TEAM =================================================
            elif t == "team_join_room":
                rid = data.get("room", "")
                nick = data.get("nickname", "")
                # Clean stale rooms.
                stale = [k for k, v in team_rooms.items() if time.time() - v.created > ROOM_TTL]
                for k in stale:
                    team_rooms.pop(k, None)
                if rid not in team_rooms:
                    await _safe_send(ws, {"type": "error", "msg": "Командная комната не найдена"})
                    continue
                tr = team_rooms[rid]
                if nick not in tr.team_of:
                    await _safe_send(ws, {"type": "error", "msg": "Вас нет в этой команде"})
                    continue
                tr.members[nick] = ws
                nickname = nick
                team_room = tr
                team_player_rooms[nick] = rid

                # Send game start to this player (others may not be connected yet).
                await _safe_send(ws, {
                    "type": "team_game_start",
                    "team": tr.team_of[nick],
                    "spawn": tr.spawns[nick],
                    "roster": [
                        {"nick": m, "team": tr.team_of[m], "spawn": tr.spawns[m]}
                        for m in tr.team_of
                    ],
                    "scores": tr.scores,
                    "team_scores": tr.team_scores,
                })
                # Notify everyone of updated room state.
                await _broadcast_team_room(tr)

            elif t == "team_state":
                # Forward to all other team-room members.
                if team_room and nickname:
                    data["type"] = "team_player_state"
                    data["from"] = nickname
                    data["team"] = team_room.team_of.get(nickname, "A")
                    await _broadcast(team_room.all_other_ws(nickname), data)

            elif t == "team_shot":
                if team_room and nickname:
                    data["type"] = "team_player_shot"
                    data["from"] = nickname
                    await _broadcast(team_room.all_other_ws(nickname), data)

            elif t == "team_hit":
                # data: {target: 'nick', damage: int}
                if team_room and nickname:
                    target = data.get("target", "")
                    # Friendly-fire OFF: don't apply damage to teammates.
                    if (target and target in team_room.team_of and
                            team_room.team_of.get(target) != team_room.team_of.get(nickname) and
                            target not in team_room.dying and
                            team_room.round_active):
                        target_ws = team_room.members.get(target)
                        await _safe_send(target_ws, {
                            "type": "team_took_damage",
                            "damage": data.get("damage", 15),
                            "from": nickname,
                        })

            elif t == "team_died":
                if team_room and nickname:
                    if nickname in team_room.dying or not team_room.round_active:
                        # Duplicate / late 'died' — ignore (prevents +2/+3 scoring).
                        pass
                    else:
                        team_room.dying.add(nickname)
                        killer = (data.get("killer") or "").strip()
                        # Only credit a kill if the killer is on the opposing team.
                        if (killer and killer in team_room.scores and
                                team_room.team_of.get(killer) != team_room.team_of.get(nickname)):
                            team_room.scores[killer] += 1
                            team_room.team_scores[team_room.team_of[killer]] += 1
                        # Broadcast death — include killer position/look so the
                        # victim's client can run a 2-second killcam.
                        ws_list = [w for w in team_room.members.values() if w is not None]
                        await _broadcast(ws_list, {
                            "type": "team_round_over",
                            "killed": nickname,
                            "killer": killer or "",
                            "scores": team_room.scores,
                            "team_scores": team_room.team_scores,
                            "killer_x": data.get("killer_x"),
                            "killer_y": data.get("killer_y"),
                            "killer_z": data.get("killer_z"),
                            "killed_x": data.get("x"),
                            "killed_y": data.get("y"),
                            "killed_z": data.get("z"),
                        })

                        # Check whether the match has now ended (kill cap or
                        # timer expired). If so, broadcast team_match_end and
                        # skip the respawn loop.
                        match_over, winner = team_room.is_match_over()
                        if match_over:
                            team_room.ended = True
                            team_room.winner = winner
                            await _broadcast(ws_list, {
                                "type": "team_match_end",
                                "winner": winner,
                                "scores": team_room.scores,
                                "team_scores": team_room.team_scores,
                                "kill_limit": MATCH_KILL_LIMIT,
                                "time_limit": MATCH_TIME_LIMIT,
                            })
                        else:
                            async def _team_respawn(tr: TeamRoom = team_room, dead_nick: str = nickname):
                                await asyncio.sleep(TEAM_RESPAWN_SECS)
                                if tr.id not in team_rooms:
                                    return
                                tr.dying.discard(dead_nick)
                                dead_ws = tr.members.get(dead_nick)
                                await _safe_send(dead_ws, {
                                    "type": "team_respawn",
                                    "spawn": tr.spawns.get(dead_nick, 0),
                                    "team": tr.team_of.get(dead_nick, "A"),
                                    "scores": tr.scores,
                                    "team_scores": tr.team_scores,
                                })

                            asyncio.create_task(_team_respawn())

            elif t == "team_grenade":
                if team_room and nickname:
                    data["type"] = "team_player_grenade"
                    data["from"] = nickname
                    await _broadcast(team_room.all_other_ws(nickname), data)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        # 1v1 cleanup
        if nickname:
            lobby.pop(nickname, None)
            if room:
                player_rooms.pop(nickname, None)
                opp_ws = room.opponent_ws(nickname)
                if nickname == room.p1_nick:
                    room.p1_ws = None
                else:
                    room.p2_ws = None
                await _safe_send(opp_ws, {"type": "opponent_left"})
                if not room.p1_ws and not room.p2_ws:
                    rooms.pop(room.id, None)

            # Team lobby cleanup
            if in_team_lobby_id:
                tlobby = team_lobbies.get(in_team_lobby_id)
                if tlobby is not None:
                    tlobby.members.pop(nickname, None)
                    team_lobby_players.pop(nickname, None)
                    if not tlobby.started:
                        await _broadcast_lobby(tlobby)
                    if not tlobby.members:
                        team_lobbies.pop(tlobby.id, None)
                        if current_team_lobby_id == tlobby.id:
                            current_team_lobby_id = None

            # Team game-room cleanup — keep the slot for RECONNECT_TTL seconds
            # so the player can re-open the tab and resume their match.
            tr_to_clean = None
            if team_room:
                tr_to_clean = team_room
            elif nickname in team_player_rooms:
                rid = team_player_rooms.get(nickname)
                tr_to_clean = team_rooms.get(rid) if rid else None

            if tr_to_clean is not None and nickname in tr_to_clean.team_of:
                # Mark ws as None and record disconnect time.
                tr_to_clean.members[nickname] = None
                tr_to_clean.disconnected_at[nickname] = time.time()
                # Notify others that this player went offline (UI can grey out
                # their avatar / label, but keep their leaderboard entry).
                ws_list = [w for w in tr_to_clean.members.values() if w is not None]
                await _broadcast(ws_list, {
                    "type": "team_player_disconnected",
                    "nick": nickname,
                })

                # Drop the room only if EVERY slot is empty.
                if all(w is None for w in tr_to_clean.members.values()):
                    # Even when fully empty, keep the room for a short while so
                    # quick mass-reconnects (e.g. ISP blip) work.
                    pass

                # Schedule the actual eviction after RECONNECT_TTL.
                async def _reconnect_timeout(tr: TeamRoom = tr_to_clean, dead_nick: str = nickname):
                    await asyncio.sleep(RECONNECT_TTL)
                    if tr.id not in team_rooms:
                        return
                    # If the player reconnected, their disconnected_at was cleared.
                    if dead_nick not in tr.disconnected_at:
                        return
                    if tr.members.get(dead_nick) is not None:
                        return
                    # Truly evict.
                    team_player_rooms.pop(dead_nick, None)
                    tr.team_of.pop(dead_nick, None)
                    tr.spawns.pop(dead_nick, None)
                    tr.scores.pop(dead_nick, None)
                    tr.dying.discard(dead_nick)
                    tr.members.pop(dead_nick, None)
                    tr.disconnected_at.pop(dead_nick, None)
                    others = [w for w in tr.members.values() if w is not None]
                    await _broadcast(others, {
                        "type": "team_player_left",
                        "nick": dead_nick,
                    })
                    await _broadcast_team_room(tr)
                    if not tr.members or all(
                        w is None for w in tr.members.values()
                    ):
                        team_rooms.pop(tr.id, None)

                asyncio.create_task(_reconnect_timeout())


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
