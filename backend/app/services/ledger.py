"""
落账一致性（全局唯一口径：同笔多写原子落账）。

不变式：每个被未终结会话（充电中/结算中/已超时）持有的桩、枪，其资源状态必须为
「占用」；反之占用资源必须能找到唯一持有会话。任何违反都视为漂移，读路径只暴露、
不掩盖；原子多写下正常业务永远不会产生漂移，漂移只可能来自外部篡改或历史脏数据。
"""
from __future__ import annotations

from app.config import HOLDER_STATUSES, LEDGER_POLICY

# 事件动作 → 对占用账的影响；按事件 id 升序重放即恢复选定落账次序
_OPEN_ACTIONS = {"开会话": +1}
_CLOSE_ACTIONS = {"结算关账": -1, "异常中断": -1}


def drift_counts(conn) -> dict:
    holders = ",".join("?" for _ in HOLDER_STATUSES)
    pile_drift = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM (
          SELECT p.id FROM piles p
          LEFT JOIN sessions s ON s.pile_id = p.id AND s.status IN ({holders})
          WHERE (p.status = '占用' AND s.id IS NULL)
             OR (p.status <> '占用' AND s.id IS NOT NULL)
        )
        """,
        HOLDER_STATUSES,
    ).fetchone()["n"]
    gun_drift = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM (
          SELECT g.id FROM guns g
          LEFT JOIN sessions s ON s.gun_id = g.id AND s.status IN ({holders})
          WHERE (g.status = '占用' AND s.id IS NULL)
             OR (g.status <> '占用' AND s.id IS NOT NULL)
        )
        """,
        HOLDER_STATUSES,
    ).fetchone()["n"]
    return {"桩位漂移数": int(pile_drift), "枪线漂移数": int(gun_drift)}


def replay_occupancy(conn) -> dict:
    """
    按选定落账次序（事件流水 id 升序）重放开充/关账/中断事件，重建每张桩、每把枪的
    期望占用态，再与当前真实状态逐行比对，证明读路径占用可由落账次序复现。
    """
    events = conn.execute(
        """
        SELECT e.id, e.action, e.session_id, s.pile_id, s.gun_id
        FROM session_events e JOIN sessions s ON s.id = e.session_id
        WHERE e.action IN ('开会话','结算关账','异常中断')
        ORDER BY e.id
        """
    ).fetchall()

    pile_delta: dict[int, int] = {}
    gun_delta: dict[int, int] = {}
    pile_of_session: dict[int, int] = {}
    gun_of_session: dict[int, int] = {}
    for e in events:
        pile_of_session[e["session_id"]] = e["pile_id"]
        gun_of_session[e["session_id"]] = e["gun_id"]
        if e["action"] in _OPEN_ACTIONS:
            pile_delta[e["pile_id"]] = pile_delta.get(e["pile_id"], 0) + 1
            gun_delta[e["gun_id"]] = gun_delta.get(e["gun_id"], 0) + 1
        elif e["action"] in _CLOSE_ACTIONS:
            pile_delta[e["pile_id"]] = pile_delta.get(e["pile_id"], 0) - 1
            gun_delta[e["gun_id"]] = gun_delta.get(e["gun_id"], 0) - 1

    def _expected(delta: dict[int, int]) -> set[int]:
        return {rid for rid, net in delta.items() if net > 0}

    expected_piles = _expected(pile_delta)
    expected_guns = _expected(gun_delta)

    actual_piles = {
        r["id"]
        for r in conn.execute("SELECT id FROM piles WHERE status='占用'").fetchall()
    }
    actual_guns = {
        r["id"]
        for r in conn.execute("SELECT id FROM guns WHERE status='占用'").fetchall()
    }

    pile_mismatch = sorted(expected_piles ^ actual_piles)
    gun_mismatch = sorted(expected_guns ^ actual_guns)
    return {
        "落账口径": LEDGER_POLICY,
        "重放依据": "session_events(id 升序)：开会话+1 / 结算关账-1 / 异常中断-1",
        "桩位不一致编号": pile_mismatch,
        "枪线不一致编号": gun_mismatch,
        "一致": not pile_mismatch and not gun_mismatch,
    }
