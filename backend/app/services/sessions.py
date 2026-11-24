from __future__ import annotations
from fastapi import HTTPException

from app.config import HOLDER_STATUSES
from app.services.core import current_band_price, get_setting, is_day_sealed, now_str, today


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


def _claim_pile(conn, pile_id: int) -> None:
    """条件钉桩：仅空闲可占；并发双开时只有一笔能拿到，另一笔 409 回滚。"""
    cur = conn.execute(
        "UPDATE piles SET status='占用' WHERE id=? AND status='空闲'", (pile_id,)
    )
    if cur.rowcount == 1:
        return
    row = conn.execute("SELECT status FROM piles WHERE id=?", (pile_id,)).fetchone()
    state = row["status"] if row else "不存在"
    raise HTTPException(409, detail=f"桩位当前为{state}，开充冲突，本笔未落账")


def _claim_gun(conn, gun_id: int, pile_id: int) -> None:
    """条件钉枪：仅可用可占；失败则整笔回滚（含已钉的桩），不留半截占用。"""
    cur = conn.execute(
        "UPDATE guns SET status='占用' WHERE id=? AND pile_id=? AND status='可用'",
        (gun_id, pile_id),
    )
    if cur.rowcount == 1:
        return
    row = conn.execute(
        "SELECT status FROM guns WHERE id=? AND pile_id=?", (gun_id, pile_id)
    ).fetchone()
    state = row["status"] if row else "不存在"
    raise HTTPException(409, detail=f"枪线当前为{state}，开充冲突，本笔回滚未占桩")


def _release_guarded(conn, table: str, id_: int) -> None:

    free_status = "空闲" if table == "piles" else "可用"
    label = "桩位" if table == "piles" else "枪线"
    cur = conn.execute(
        f"UPDATE {table} SET status=? WHERE id=? AND status='占用'",
        (free_status, id_),
    )
    if cur.rowcount != 1:
        row = conn.execute(f"SELECT status FROM {table} WHERE id=?", (id_,)).fetchone()
        state = row["status"] if row else "不存在"
        raise HTTPException(
            409,
            detail=f"{label}{id_}当前为{state}，与占用账不一致，"
            "本笔回滚以暴露漂移，禁止伪装释放成功",
        )


def open_session(conn, pile_id: int, gun_id: int | None = None) -> dict:
    # —— 纯校验段（不落任何写），封印/桩存在性/站点 ——
    if is_day_sealed(conn):
        raise HTTPException(400, detail="充日已封印，禁止新开充")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    site = conn.execute("SELECT * FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    if not site or not site["active"]:
        raise HTTPException(400, detail="站点未启用")

    # —— 原子多写段：钉桩 → 钉枪 → 会话 → 开充事件，同成败 ——
    # 1) 条件钉桩作为权威并发闸门：并发双开同一桩在此分胜负，一成一败（409），
    #    绝不一成一半；拿到写锁后后续选枪不再受并发扰动。
    _claim_pile(conn, pile_id)
    try:
        # 2) 已持锁内选枪，再条件钉枪；失败抛错由外层 tx 整笔回滚（含已钉的桩）
        if gun_id is None:
            gun = conn.execute(
                "SELECT * FROM guns WHERE pile_id=? AND status='可用' ORDER BY id LIMIT 1",
                (pile_id,),
            ).fetchone()
            if not gun:
                raise HTTPException(400, detail="无可用枪线")
        else:
            gun = conn.execute(
                "SELECT * FROM guns WHERE id=? AND pile_id=?", (gun_id, pile_id)
            ).fetchone()
            if not gun:
                raise HTTPException(400, detail="枪线不存在或不属于该桩")
        _claim_gun(conn, gun["id"], pile_id)

        # 3) 会话落「充电中」
        price = current_band_price(conn, pile["rate_plan_id"])
        slip = _next_slip(conn)
        ts = now_str()
        cur = conn.execute(
            """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,kwh,amount,price_locked,started_at,note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                slip,
                pile["site_id"],
                pile_id,
                gun["id"],
                pile["rate_plan_id"],
                today(),
                "充电中",
                0,
                0,
                price,
                ts,
                "",
            ),
        )
        sid = cur.lastrowid

        # 4) 开充事件流水；若此步失败同样整笔回滚（含桩/枪占用与会话）
        conn.execute(
            "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
            (sid, "开会话", ts, f"锁定电价{price}；同笔多写原子落账"),
        )
    except Exception:
        # 显式回滚到开充前快照后再抛，保证不留下半截漂移
        conn.rollback()
        raise

    return {
        "会话号": slip,
        "会话编号": sid,
        "锁定电价": price,
        "状态": "充电中",
        "桩位编号": pile_id,
        "桩位状态": "占用",
        "枪线编号": gun["id"],
        "枪线状态": "占用",
        "落账口径": "同笔多写原子落账",
    }


def add_pulse(conn, session_id: int, delta_kwh: float, power_kw: float = 0) -> dict:
    if delta_kwh <= 0:
        raise HTTPException(400, detail="脉冲电量须大于零")
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可记脉冲")
    ts = now_str()
    conn.execute(
        "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(?,?,?,?)",
        (session_id, delta_kwh, power_kw, ts),
    )
    new_kwh = float(se["kwh"]) + float(delta_kwh)
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(new_kwh * price, 2)
    conn.execute(
        "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
        (round(new_kwh, 3), amount, session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "脉冲", ts, f"+{delta_kwh}"),
    )
    return {"会话编号": session_id, "累计电量": round(new_kwh, 3), "预估金额": amount}


def settle(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in HOLDER_STATUSES:
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")
    ts = now_str()
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(float(se["kwh"]) * price, 2)
    conn.execute(
        "UPDATE sessions SET status='已关账', amount=?, closed_at=?, price_locked=? WHERE id=?",
        (amount, ts, price, session_id),
    )
    # 守卫释放：只在占用态释放；漂移即回滚，桩/枪/会话/事件同成败
    _release_guarded(conn, "piles", se["pile_id"])
    _release_guarded(conn, "guns", se["gun_id"])
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "结算关账", ts, f"金额{amount}；同笔释放桩枪"),
    )
    return {
        "会话编号": session_id,
        "电量": float(se["kwh"]),
        "金额": amount,
        "状态": "已关账",
        "桩位编号": se["pile_id"],
        "桩位状态": "空闲",
        "枪线编号": se["gun_id"],
        "枪线状态": "可用",
    }


def mark_timeout(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
    # 已超时仅是业务标记，不释放桩/枪占用，占用账仍由该会话持有
    minutes = int(get_setting(conn, "超时分钟", "120"))
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已超时', note=? WHERE id=?",
        (f"超过{minutes}分钟未关", session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "超时", ts, f"阈值{minutes}分钟；占用不释放"),
    )
    return {"会话编号": session_id, "状态": "已超时"}


def abort_session(conn, session_id: int, reason: str = "急停") -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in HOLDER_STATUSES:
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可中断")
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已中断', closed_at=?, note=? WHERE id=?",
        (ts, reason, session_id),
    )
    # 急停与开充并发时由 BEGIN IMMEDIATE 串行：要么先于开充执行（开充随后 409），

    _release_guarded(conn, "piles", se["pile_id"])
    _release_guarded(conn, "guns", se["gun_id"])
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "异常中断", ts, f"{reason}；同笔释放桩枪"),
    )
    return {
        "会话编号": session_id,
        "状态": "已中断",
        "原因": reason,
        "桩位编号": se["pile_id"],
        "桩位状态": "空闲",
        "枪线编号": se["gun_id"],
        "枪线状态": "可用",
    }


def set_pile_status(conn, pile_id: int, status: str) -> dict:
    if status == "占用":
        # 占用钉由开充事务同笔钉、由结算/急停同笔拔，禁止旁路改占用钉字
        raise HTTPException(400, detail="占用钉只能由开充/结算/急停事务改写，禁止旁路改钉")
    if status not in ("空闲", "故障", "维护"):
        raise HTTPException(400, detail="非法桩位状态")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    holder = conn.execute(
        "SELECT id FROM sessions WHERE pile_id=? AND status IN (?,?,?)",
        (pile_id, *HOLDER_STATUSES),
    ).fetchone()
    if holder and status in ("故障", "维护"):
        # 仍有占用持有者：禁止手工改故障/维护，避免桩态与占用账脱钩
        raise HTTPException(400, detail="仍有未终结会话占用，不可直接改故障或维护")
    if status == "空闲" and holder:
        raise HTTPException(400, detail="仍有未终结会话占用，不可手工改空闲")
    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))
    return {"桩位编号": pile_id, "状态": status}

