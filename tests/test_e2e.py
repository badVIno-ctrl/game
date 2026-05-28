#!/usr/bin/env python3
"""End-to-end test suite for play backend."""

import asyncio
import json
import sys
import time
import websockets

URL = "ws://localhost:8000/ws"
PASS = []
FAIL = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}  {extra}")


async def recv_with_timeout(ws, timeout=1.5):
    try:
        m = await asyncio.wait_for(ws.recv(), timeout=timeout)
        return json.loads(m)
    except asyncio.TimeoutError:
        return None


async def drain(ws, timeout=0.3):
    msgs = []
    while True:
        m = await recv_with_timeout(ws, timeout=timeout)
        if m is None:
            return msgs
        msgs.append(m)


# ============================================================================
# TEST 1: Команда — два игрока, авто-распределение, старт
# ============================================================================
async def test_basic_team_flow():
    print("\n[TEST 1] Basic team flow — 2 players, auto-split, start")
    a = await websockets.connect(URL)
    b = await websockets.connect(URL)
    try:
        await a.send(json.dumps({"type": "team_join", "nickname": "alice"}))
        m_a_joined = await recv_with_timeout(a)
        check("alice gets team_joined", m_a_joined and m_a_joined.get("type") == "team_joined")
        check("alice assigned to team A", m_a_joined and m_a_joined.get("team") == "A")
        await drain(a)  # consume lobby state

        await b.send(json.dumps({"type": "team_join", "nickname": "bob"}))
        m_b_joined = await recv_with_timeout(b)
        check("bob assigned to team B (auto-balance)", m_b_joined and m_b_joined.get("team") == "B")
        await drain(b)

        # Alice should see lobby update with bob
        msgs_a = await drain(a)
        lobby_msg = next((m for m in msgs_a if m.get("type") == "team_lobby_state"), None)
        check("alice sees bob in lobby",
              lobby_msg and "bob" in lobby_msg.get("teamB", []),
              extra=str(lobby_msg))
        check("can_start = True with 1+1", lobby_msg and lobby_msg.get("can_start") is True)

        # Start the match
        await a.send(json.dumps({"type": "team_start"}))
        m_a_start = await recv_with_timeout(a)
        m_b_start = await recv_with_timeout(b)
        check("alice gets team_match_start",
              m_a_start and m_a_start.get("type") == "team_match_start")
        check("bob gets team_match_start",
              m_b_start and m_b_start.get("type") == "team_match_start")
        check("both have same room id",
              m_a_start and m_b_start and m_a_start["room"] == m_b_start["room"])
        check("roster has 2 entries",
              m_a_start and len(m_a_start.get("roster", [])) == 2)

        room = m_a_start["room"]

        # Join room
        await a.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "alice"}))
        await b.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "bob"}))

        m_a_gs = await recv_with_timeout(a)
        m_b_gs = await recv_with_timeout(b)
        check("alice gets team_game_start",
              m_a_gs and m_a_gs.get("type") == "team_game_start")
        check("bob gets team_game_start",
              m_b_gs and m_b_gs.get("type") == "team_game_start")
        check("alice's spawn = 0 (first in team A)",
              m_a_gs and m_a_gs.get("spawn") == 0)
        check("bob's spawn = 0 (first in team B)",
              m_b_gs and m_b_gs.get("spawn") == 0)

        return {"room": room, "a": a, "b": b}
    except Exception:
        await a.close(); await b.close()
        raise


# ============================================================================
# TEST 2: Scoring bug — multiple died -> only +1
# ============================================================================
async def test_scoring_bug_fix(a, b):
    print("\n[TEST 2] Scoring fix — 3x 'team_died' from same nick must yield +1")
    await drain(a); await drain(b)

    # bob dies 3 times in a row (simulating bug condition)
    for i in range(3):
        await b.send(json.dumps({"type": "team_died", "killer": "alice"}))
        await asyncio.sleep(0.05)

    msgs = await drain(a, timeout=0.5)
    round_overs = [m for m in msgs if m.get("type") == "team_round_over"]
    check("exactly 1 team_round_over broadcast", len(round_overs) == 1,
          extra=f"got {len(round_overs)}")
    if round_overs:
        check("alice has 1 kill", round_overs[-1]["scores"]["alice"] == 1)
        check("bob has 0 kills",  round_overs[-1]["scores"]["bob"] == 0)
        check("team A has 1 total", round_overs[-1]["team_scores"]["A"] == 1)


# ============================================================================
# TEST 3: Friendly fire OFF
# ============================================================================
async def test_friendly_fire(room):
    print("\n[TEST 3] Friendly fire — hits on teammate must NOT damage")
    # Add a 3rd player to team A. They shoot alice (teammate). Server must drop damage.
    a = await websockets.connect(URL)
    c = await websockets.connect(URL)
    try:
        # need fresh lobby for these. But we want them to JOIN the active room.
        # Currently the server has no late-join, so let's verify friendly fire
        # via two-player setup separately.
        await a.send(json.dumps({"type": "team_join", "nickname": "p1"}))
        await drain(a)
        await c.send(json.dumps({"type": "team_join", "nickname": "p2"}))
        await drain(c); await drain(a)
        await a.send(json.dumps({"type": "team_start"}))
        m_a = await recv_with_timeout(a)
        m_c = await recv_with_timeout(c)
        room2 = m_a["room"]
        # Both joined team A and team B respectively (auto-balance: p1=A, p2=B).
        # We need TWO players on the same team to test friendly fire properly.
        check("p1 -> A, p2 -> B (auto-balance with 1+1)",
              m_a["team"] == "A" and m_c["team"] == "B")
        await a.send(json.dumps({"type": "team_join_room", "room": room2, "nickname": "p1"}))
        await c.send(json.dumps({"type": "team_join_room", "room": room2, "nickname": "p2"}))
        await drain(a); await drain(c)

        # p1 (A) shoots p2 (B) — should land
        await a.send(json.dumps({"type": "team_hit", "target": "p2", "damage": 25}))
        msg = await recv_with_timeout(c, timeout=0.5)
        check("enemy team hit lands", msg and msg.get("type") == "team_took_damage")

        # Now simulate p1 hitting themselves (server has team_of["p1"] == "A", same team -> drop)
        await a.send(json.dumps({"type": "team_hit", "target": "p1", "damage": 25}))
        msg = await recv_with_timeout(a, timeout=0.5)
        check("self-hit (friendly fire) dropped", msg is None,
              extra=f"got {msg}")
    finally:
        await a.close(); await c.close()


# ============================================================================
# TEST 4: Can't start with empty team
# ============================================================================
async def test_no_empty_team_start():
    print("\n[TEST 4] team_start blocked when one team empty")
    a = await websockets.connect(URL)
    try:
        await a.send(json.dumps({"type": "team_join", "nickname": "solo"}))
        await drain(a)
        await a.send(json.dumps({"type": "team_start"}))
        m = await recv_with_timeout(a)
        check("solo gets error", m and m.get("type") == "error")
        check("error mentions 1 per team", m and "1 игрок" in (m.get("msg", "") or ""))
    finally:
        await a.close()


# ============================================================================
# TEST 5: 1v1 PVP scoring bug fix
# ============================================================================
async def test_pvp_1v1_scoring():
    print("\n[TEST 5] 1v1 PvP — 3x 'died' must yield +1")
    a = await websockets.connect(URL)
    b = await websockets.connect(URL)
    try:
        await a.send(json.dumps({"type": "register", "nickname": "x"}))
        await recv_with_timeout(a)
        await b.send(json.dumps({"type": "register", "nickname": "y"}))
        await recv_with_timeout(b)
        await a.send(json.dumps({"type": "find", "target": "y"}))
        m_a = await recv_with_timeout(a)
        m_b = await recv_with_timeout(b)
        room = m_a["room"]
        await a.send(json.dumps({"type": "join_room", "room": room, "nickname": "x"}))
        await b.send(json.dumps({"type": "join_room", "room": room, "nickname": "y"}))
        await drain(a); await drain(b)
        for _ in range(3):
            await b.send(json.dumps({"type": "died"}))
            await asyncio.sleep(0.05)
        msgs = await drain(a, timeout=0.5)
        round_overs = [m for m in msgs if m.get("type") == "round_over"]
        check("exactly 1 round_over in 1v1", len(round_overs) == 1,
              extra=f"got {len(round_overs)}")
        if round_overs:
            check("x: 1, y: 0",
                  round_overs[-1]["scores"].get("x") == 1 and
                  round_overs[-1]["scores"].get("y") == 0)
    finally:
        await a.close(); await b.close()


# ============================================================================
# TEST 6: Nickname collisions blocked
# ============================================================================
async def test_nick_collision():
    print("\n[TEST 6] Duplicate nicks rejected")
    a = await websockets.connect(URL)
    b = await websockets.connect(URL)
    try:
        await a.send(json.dumps({"type": "team_join", "nickname": "samenick"}))
        await drain(a)
        await b.send(json.dumps({"type": "team_join", "nickname": "samenick"}))
        m = await recv_with_timeout(b)
        check("duplicate nick rejected", m and m.get("type") == "error")
    finally:
        await a.close(); await b.close()


# ============================================================================
# TEST 7: Auto-balance with 4 players (2 vs 2)
# ============================================================================
async def test_autobalance_4():
    print("\n[TEST 7] Auto-balance — 4 players should split 2/2")
    conns = []
    teams = []
    try:
        for i, nick in enumerate(["q1", "q2", "q3", "q4"]):
            c = await websockets.connect(URL)
            conns.append(c)
            await c.send(json.dumps({"type": "team_join", "nickname": nick}))
            m = await recv_with_timeout(c)
            teams.append(m["team"])
            await drain(c)
        check("q1 -> A", teams[0] == "A")
        check("q2 -> B (balance)", teams[1] == "B")
        check("q3 -> A", teams[2] == "A")
        check("q4 -> B", teams[3] == "B")
        check("final split 2v2",
              teams.count("A") == 2 and teams.count("B") == 2)
    finally:
        for c in conns:
            await c.close()


# ============================================================================
# TEST 8: Late join into active match (NOT IMPLEMENTED YET)
# ============================================================================
async def test_late_join():
    print("\n[TEST 8] Late join into active match")
    a = await websockets.connect(URL)
    b = await websockets.connect(URL)
    late = await websockets.connect(URL)
    try:
        # Start a 1v1 match
        await a.send(json.dumps({"type": "team_join", "nickname": "early1"}))
        await drain(a)
        await b.send(json.dumps({"type": "team_join", "nickname": "early2"}))
        await drain(b); await drain(a)
        await a.send(json.dumps({"type": "team_start"}))
        m_a = await recv_with_timeout(a)
        m_b = await recv_with_timeout(b)
        room = m_a["room"]
        await a.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "early1"}))
        await b.send(json.dumps({"type": "team_join_room", "room": room, "nickname": "early2"}))
        await drain(a); await drain(b)

        # NOW a 3rd player joins
        await late.send(json.dumps({"type": "team_join", "nickname": "joiner"}))
        m_late = await recv_with_timeout(late, timeout=1.0)
        # FEATURE TEST: should get team_match_start (drop into active room)
        is_match_start = m_late and m_late.get("type") == "team_match_start"
        is_joined_into_room = is_match_start and m_late.get("room") == room
        check("late joiner dropped into active room (LATE JOIN)",
              is_joined_into_room,
              extra=f"got {m_late}")
    finally:
        await a.close(); await b.close(); await late.close()


# ============================================================================
async def main():
    tests = [
        test_basic_team_flow,
        test_no_empty_team_start,
        test_pvp_1v1_scoring,
        test_friendly_fire,
        test_nick_collision,
        test_autobalance_4,
        test_late_join,
    ]
    # test 1 is special — returns connections used by test 2
    print("=" * 60)
    state = await test_basic_team_flow()
    await test_scoring_bug_fix(state["a"], state["b"])
    await state["a"].close()
    await state["b"].close()

    for t in [test_no_empty_team_start, test_pvp_1v1_scoring,
              test_friendly_fire, test_nick_collision,
              test_autobalance_4, test_late_join]:
        try:
            if asyncio.iscoroutinefunction(t):
                if t == test_friendly_fire:
                    await t(None)
                else:
                    await t()
        except Exception as e:
            FAIL.append(f"{t.__name__} CRASHED")
            print(f"  ✗ {t.__name__} CRASHED: {e}")

    print("\n" + "=" * 60)
    print(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
    if FAIL:
        print("\nFailures:")
        for f in FAIL:
            print(f"  - {f}")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
