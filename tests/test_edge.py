#!/usr/bin/env python3
"""Edge case tests."""

import asyncio, json, sys, websockets
import urllib.request

URL = "ws://localhost:8000/ws"
RESET_URL = "http://localhost:8000/__test_reset"


def reset_server():
    """Wipe server-side state so each edge test starts clean (test-only
    endpoint, gated by ENABLE_TEST_RESET on the server)."""
    try:
        req = urllib.request.Request(RESET_URL, method="POST", data=b"")
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}  {'' if cond else extra}")

async def recv(ws, timeout=1.5):
    try: return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
    except asyncio.TimeoutError: return None

async def drain(ws, timeout=0.3):
    out = []
    while True:
        m = await recv(ws, timeout=timeout)
        if m is None: return out
        out.append(m)


async def recv_type(ws, mtype, timeout=1.5, tries=6):
    """Wait for a message of a specific type, skipping unrelated broadcasts
    (e.g. team_lobby_state) that may arrive first."""
    for _ in range(tries):
        m = await recv(ws, timeout=timeout)
        if m is None:
            return None
        if m.get("type") == mtype:
            return m
    return None


# ---- Late-join: existing players see the new player in team_room_state
async def test_late_join_broadcast():
    print("\n[E1] Late join broadcasts roster to existing players")
    a = await websockets.connect(URL); b = await websockets.connect(URL); c = await websockets.connect(URL)
    try:
        await a.send(json.dumps({"type": "team_join", "nickname": "lj1"}))
        await drain(a)
        await b.send(json.dumps({"type": "team_join", "nickname": "lj2"}))
        await drain(b); await drain(a)
        await a.send(json.dumps({"type": "team_start"}))
        m_a = await recv_type(a, "team_match_start"); m_b = await recv_type(b, "team_match_start")
        room = m_a["room"]
        await a.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "lj1"}))
        await b.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "lj2"}))
        await drain(a); await drain(b)

        # c late-joins
        await c.send(json.dumps({"type": "team_join", "nickname": "lj3"}))
        m_c = await recv_type(c, "team_match_start")
        check("lj3 gets team_match_start (late)", m_c and m_c.get("type") == "team_match_start" and m_c.get("late_join"))
        # Existing players should get a team_room_state broadcast
        msgs_a = await drain(a, timeout=0.5)
        rs = next((m for m in msgs_a if m.get("type") == "team_room_state"), None)
        check("lj1 (existing) gets team_room_state about lj3", rs is not None)
        if rs:
            nicks = [e["nick"] for e in rs.get("roster", [])]
            check("roster contains lj3", "lj3" in nicks, extra=str(nicks))

        # lj3 connects via game-page WS and gets team_game_start
        await c.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "lj3"}))
        m = await recv_type(c, "team_game_start")
        check("lj3 game_start after join_room", m and m.get("type") == "team_game_start")
        if m:
            nicks = [e["nick"] for e in m.get("roster", [])]
            check("lj3 sees full roster (3 players)", set(nicks) == {"lj1", "lj2", "lj3"}, extra=str(nicks))
    finally:
        await a.close(); await b.close(); await c.close()


# ---- Fill 20-slot room: 21st player should NOT late-join (full)
async def test_room_full():
    print("\n[E2] Room full at 20 players → 21st can't late-join")
    conns = []
    try:
        # Two start players
        a = await websockets.connect(URL); conns.append(a)
        b = await websockets.connect(URL); conns.append(b)
        await a.send(json.dumps({"type": "team_join", "nickname": "f1"})); await drain(a)
        await b.send(json.dumps({"type": "team_join", "nickname": "f2"})); await drain(b); await drain(a)
        await a.send(json.dumps({"type": "team_start"}))
        m_a = await recv_type(a, "team_match_start"); m_b = await recv_type(b, "team_match_start")
        room = m_a["room"]
        await a.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "f1"}))
        await b.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "f2"}))
        await drain(a); await drain(b)
        # Now late-join 18 more to fill the room to 20 total
        for i in range(18):
            c = await websockets.connect(URL); conns.append(c)
            await c.send(json.dumps({"type": "team_join", "nickname": f"f{i+3}"}))
            m = await recv_type(c, "team_match_start")
            check(f"f{i+3} late-joins active room",
                  m and m.get("type") == "team_match_start" and m.get("late_join"),
                  extra=str(m))
            # finalize via game-page WS too
            await c.send(json.dumps({"type": "team_join_room", "room": room, "nickname": f"f{i+3}"}))
            await drain(c)
            await drain(a); await drain(b)
        # 21st attempts join
        c21 = await websockets.connect(URL); conns.append(c21)
        await c21.send(json.dumps({"type": "team_join", "nickname": "f21"}))
        m = await recv(c21)
        # Should fall through to NEW lobby since room is full (20/20)
        check("21st joiner falls back to a fresh lobby",
              m and m.get("type") == "team_joined", extra=str(m))
    finally:
        for c in conns: await c.close()


# ---- Player leaves mid-match → others get team_player_left
async def test_player_leave():
    print("\n[E3] Mid-match leave broadcasts team_player_left")
    a = await websockets.connect(URL); b = await websockets.connect(URL)
    try:
        await a.send(json.dumps({"type": "team_join", "nickname": "L1"})); await drain(a)
        await b.send(json.dumps({"type": "team_join", "nickname": "L2"})); await drain(b); await drain(a)
        await a.send(json.dumps({"type": "team_start"}))
        m_a = await recv_type(a, "team_match_start"); m_b = await recv_type(b, "team_match_start")
        room = m_a["room"]
        await a.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "L1"}))
        await b.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "L2"}))
        await drain(a); await drain(b)
        # b leaves
        await b.close()
        await asyncio.sleep(0.3)
        msgs = await drain(a, timeout=0.5)
        # Reconnect-friendly design: an abrupt disconnect first emits
        # team_player_disconnected (slot held for RECONNECT_TTL). A definitive
        # team_player_left only fires after the TTL. Either counts as "notified".
        left = next((m for m in msgs
                     if m.get("type") in ("team_player_disconnected", "team_player_left")
                     and m.get("nick") == "L2"), None)
        check("L1 notified of L2 leaving", left is not None, extra=str(left))
    finally:
        await a.close()
        try: await b.close()
        except: pass


# ---- Late joiner can leave/rejoin
async def test_rejoin():
    print("\n[E4] Player can rejoin after disconnect")
    a = await websockets.connect(URL); b = await websockets.connect(URL)
    try:
        await a.send(json.dumps({"type": "team_join", "nickname": "rj1"})); await drain(a)
        await b.send(json.dumps({"type": "team_join", "nickname": "rj2"})); await drain(b); await drain(a)
        await a.send(json.dumps({"type": "team_start"}))
        m_a = await recv_type(a, "team_match_start"); m_b = await recv_type(b, "team_match_start")
        room = m_a["room"]
        await a.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "rj1"}))
        await b.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "rj2"}))
        await drain(a); await drain(b)
        # b disconnects
        await b.close(); await asyncio.sleep(0.4)
        # b reconnects with same nick
        b2 = await websockets.connect(URL)
        await b2.send(json.dumps({"type": "team_join", "nickname": "rj2"}))
        m = await recv(b2)
        check("rj2 can rejoin under same nick (no ник занят)",
              m and m.get("type") == "team_match_start", extra=str(m))
        await b2.close()
    finally:
        await a.close()
        try: await b.close()
        except: pass


# ---- Concurrent team_start: only one should succeed
async def test_concurrent_start():
    print("\n[E5] Concurrent team_start — only one match starts")
    a = await websockets.connect(URL); b = await websockets.connect(URL)
    try:
        await a.send(json.dumps({"type": "team_join", "nickname": "cs1"})); await drain(a)
        await b.send(json.dumps({"type": "team_join", "nickname": "cs2"})); await drain(b); await drain(a)
        # Both press start simultaneously
        await asyncio.gather(
            a.send(json.dumps({"type": "team_start"})),
            b.send(json.dumps({"type": "team_start"})),
        )
        msgs_a = await drain(a); msgs_b = await drain(b)
        all_msgs = msgs_a + msgs_b
        starts = [m for m in all_msgs if m.get("type") == "team_match_start"]
        check("exactly 2 match_start (one per player) regardless of double press",
              len(starts) == 2, extra=f"got {len(starts)}")
    finally:
        await a.close(); await b.close()


async def main():
    tests = [test_late_join_broadcast, test_room_full,
             test_player_leave, test_rejoin, test_concurrent_start]
    for t in tests:
        reset_server()
        try: await t()
        except Exception as e:
            FAIL.append(f"{t.__name__} CRASHED")
            print(f"  ✗ {t.__name__} CRASHED: {e}")
        # Allow server-side cleanup of orphan rooms before the next test.
        await asyncio.sleep(0.6)
    print(f"\nPASS: {len(PASS)}  FAIL: {len(FAIL)}")
    if FAIL:
        for f in FAIL: print("  -", f)
        sys.exit(1)
    print("All edge tests passed.")

asyncio.run(main())
