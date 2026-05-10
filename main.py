from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
import json
import os
import uuid
import asyncio
import random
import time

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ROOM_TTL = 3600  # 1 hour max room lifetime

# --- In-memory state ---
lobby: dict[str, WebSocket] = {}      # nickname -> ws (players waiting in lobby)
player_rooms: dict[str, str] = {}     # nickname -> room_id (players in game)


class Room:
    __slots__ = (
        "id", "p1_nick", "p2_nick", "p1_ws", "p2_ws",
        "p1_spawn", "p2_spawn", "scores", "created", "round_active",
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

    def opponent_ws(self, nick: str) -> WebSocket | None:
        return self.p2_ws if nick == self.p1_nick else self.p1_ws

    def opponent_nick(self, nick: str) -> str:
        return self.p2_nick if nick == self.p1_nick else self.p1_nick


rooms: dict[str, Room] = {}


async def _safe_send(ws: WebSocket | None, data: dict) -> None:
    if ws is None:
        return
    try:
        await ws.send_text(json.dumps(data))
    except Exception:
        pass


# --- Routes ---

@app.get("/")
async def root():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.get("/game")
async def game_page():
    return FileResponse(os.path.join(BASE_DIR, "game.html"))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    nickname: str | None = None
    room: Room | None = None

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            t = data.get("type")

            # ---- LOBBY ----
            if t == "register":
                nick = data.get("nickname", "").strip()[:24]
                if not nick:
                    await _safe_send(ws, {"type": "error", "msg": "Пустой никнейм"})
                    continue
                if nick in lobby or nick in player_rooms:
                    await _safe_send(ws, {"type": "error", "msg": "Ник занят"})
                    continue
                # Remove old registration if re-registering
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

                # Send to BOTH players — send to target first (they may have
                # been waiting longer), then to the finder
                await _safe_send(target_ws, {
                    "type": "matched",
                    "room": room_id,
                    "spawn": r.p2_spawn,
                    "opponent": nickname,
                })
                await _safe_send(ws, {
                    "type": "matched",
                    "room": room_id,
                    "spawn": r.p1_spawn,
                    "opponent": target,
                })

            # ---- IN-GAME ----
            elif t == "join_room":
                rid = data.get("room", "")
                nick = data.get("nickname", "")
                # Clean stale rooms
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
                    await _safe_send(r.p1_ws, {
                        "type": "game_start",
                        "spawn": r.p1_spawn,
                        "opponent": r.p2_nick,
                        "scores": r.scores,
                    })
                    await _safe_send(r.p2_ws, {
                        "type": "game_start",
                        "spawn": r.p2_spawn,
                        "opponent": r.p1_nick,
                        "scores": r.scores,
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
                if room:
                    opp = room.opponent_ws(nickname or "")
                    data["type"] = "took_damage"
                    await _safe_send(opp, data)

            elif t == "died":
                if room and nickname:
                    killer = room.opponent_nick(nickname)
                    if killer in room.scores:
                        room.scores[killer] += 1

                    room.p1_spawn = 1 - room.p1_spawn
                    room.p2_spawn = 1 - room.p2_spawn
                    room.round_active = False

                    death_msg = {
                        "type": "round_over",
                        "killed": nickname,
                        "killer": killer,
                        "scores": room.scores,
                    }
                    await _safe_send(room.p1_ws, death_msg)
                    await _safe_send(room.p2_ws, death_msg)

                    async def _respawn(r: Room = room):
                        await asyncio.sleep(5)
                        if r.id not in rooms:
                            return
                        r.round_active = True
                        await _safe_send(r.p1_ws, {
                            "type": "respawn",
                            "spawn": r.p1_spawn,
                            "scores": r.scores,
                        })
                        await _safe_send(r.p2_ws, {
                            "type": "respawn",
                            "spawn": r.p2_spawn,
                            "scores": r.scores,
                        })

                    asyncio.create_task(_respawn())

            elif t == "grenade_throw":
                if room:
                    opp = room.opponent_ws(nickname or "")
                    data["type"] = "opponent_grenade"
                    await _safe_send(opp, data)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if nickname:
            lobby.pop(nickname, None)
            # Only remove from player_rooms if we were IN a game room
            # (not just in lobby waiting). The lobby WS closing after
            # redirect should NOT remove the room mapping.
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
