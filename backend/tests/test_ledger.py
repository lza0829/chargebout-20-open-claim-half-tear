"""
落账正确性测试（全局口径：同笔多写原子落账）。

覆盖：
1. 开充同笔多写：会话/桩占/枪占/事件同成败，正常后无漂移且可重放；
2. 并发双开同一桩：恰一成一败，绝不一成一半；
3. 多写中途被人为打断：整笔回滚到开充前快照；
4. 并发开充与急停：不出现假绿（桩空闲却有未终结会话），最终态自洽；
5. 读路径占用可由选定落账次序（事件流水）重放复现。

五个开充夹具（test_09 起）：
F1 双钉只成一侧 —— 枪钉失败整笔回滚，不留半截占用；
F2 会话行有双钉空 —— 漂移被显式标红，不伪装已对齐；
F3 事件成功总览未加 —— 回执/会话行/双钉/事件/总览桶同命运；
F4 开充急停交错半截 —— 见 test_04，任意交错后终态自洽；
F5 连点开充一笔幽灵占用 —— 见 test_02 并发版与 test_12 顺序版；
另含：脉冲入口误触不加脏电量、旁路改占用钉被拒、假多写落账层收口清零。
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

# 测试必须在导入 app 之前固定一个独立临时库
_tmp = tempfile.mkdtemp(prefix="chargebout_test_")
os.environ["CHARGEBOUT_DB"] = os.path.join(_tmp, "test.db")

from fastapi import HTTPException  # noqa: E402

from app.db import init_db, tx  # noqa: E402
from app.services import sessions as svc  # noqa: E402
from app.services.core import overview  # noqa: E402
from app.services.ledger import drift_counts, replay_occupancy  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402


def _first_pile_gun(conn):
    pile = conn.execute(
        "SELECT * FROM piles WHERE status='空闲' ORDER BY id LIMIT 1"
    ).fetchone()
    gun = conn.execute(
        "SELECT * FROM guns WHERE pile_id=? AND status='可用' ORDER BY id LIMIT 1",
        (pile["id"],),
    ).fetchone()
    return pile["id"], gun["id"]


def _open(pile_id):
    with tx() as conn:
        return svc.open_session(conn, pile_id)


def _abort(sid):
    with tx() as conn:
        return svc.abort_session(conn, sid)


class LedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        seed_if_empty()

    def _snapshot(self):
        with tx() as conn:
            return {
                "piles": {
                    r["id"]: r["status"]
                    for r in conn.execute("SELECT id,status FROM piles").fetchall()
                },
                "guns": {
                    r["id"]: r["status"]
                    for r in conn.execute("SELECT id,status FROM guns").fetchall()
                },
                "sessions": conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()[
                    "c"
                ],
                "events": conn.execute("SELECT COUNT(*) AS c FROM session_events").fetchone()[
                    "c"
                ],
            }

    def test_01_open_is_atomic_multiwrite_and_replays(self):
        with tx() as conn:
            pile_id, gun_id = _first_pile_gun(conn)
        before = self._snapshot()
        r = _open(pile_id)
        self.assertEqual(r["状态"], "充电中")
        with tx() as conn:
            pile = conn.execute("SELECT status FROM piles WHERE id=?", (pile_id,)).fetchone()
            gun = conn.execute("SELECT status FROM guns WHERE id=?", (gun_id,)).fetchone()
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (r["会话编号"],)).fetchone()
            ev = conn.execute(
                "SELECT * FROM session_events WHERE session_id=? AND action='开会话'",
                (r["会话编号"],),
            ).fetchone()
        self.assertEqual(pile["status"], "占用")
        self.assertEqual(gun["status"], "占用")
        self.assertEqual(se["status"], "充电中")
        self.assertIsNotNone(ev)
        with tx() as conn:
            self.assertEqual(drift_counts(conn), {"桩位漂移数": 0, "枪线漂移数": 0})
            rep = replay_occupancy(conn)
            self.assertTrue(rep["一致"])
        self.assertGreater(self._snapshot()["sessions"], before["sessions"])
        # 收尾释放，保持后续用例有空闲桩
        with tx() as conn:
            svc.abort_session(conn, r["会话编号"], "测试回收")

    def test_02_concurrent_double_open_one_wins_one_conflicts(self):
        with tx() as conn:
            pile_id, _ = _first_pile_gun(conn)
        errors = []
        results = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            try:
                results.append(_open(pile_id))
            except HTTPException as e:
                errors.append(e.status_code)

        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(lambda _: worker(), range(2)))

        self.assertEqual(len(results), 1, "恰有一笔开充成功")
        self.assertEqual(errors, [409], "另一笔必须 409 冲突而非 500 或半截")
        with tx() as conn:
            open_n = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中','已超时')",
                (pile_id,),
            ).fetchone()["c"]
            pile = conn.execute("SELECT status FROM piles WHERE id=?", (pile_id,)).fetchone()
        self.assertEqual(open_n, 1, "占用桩只对应一笔未终结会话，绝不一成一半")
        self.assertEqual(pile["status"], "占用")
        with tx() as conn:
            self.assertEqual(drift_counts(conn), {"桩位漂移数": 0, "枪线漂移数": 0})
        # 回收
        with tx() as conn:
            sid = conn.execute(
                "SELECT id FROM sessions WHERE pile_id=? AND status='充电中'", (pile_id,)
            ).fetchone()["id"]
            svc.abort_session(conn, sid, "测试回收")

    def test_03_interrupted_multiwrite_rolls_back_entirely(self):
        with tx() as conn:
            pile_id, gun_id = _first_pile_gun(conn)
        before = self._snapshot()

        with self.assertRaises(RuntimeError):
            with tx() as conn:
                svc.open_session(conn, pile_id)
                # 人为打断：模拟事件写入阶段崩溃
                raise RuntimeError("模拟写事件后崩溃")

        after = self._snapshot()
        self.assertEqual(after["piles"][pile_id], "空闲", "桩占用必须随整笔回滚")
        self.assertEqual(after["guns"][gun_id], "可用", "枪占用必须随整笔回滚")
        self.assertEqual(after["sessions"], before["sessions"], "不得残留会话")
        self.assertEqual(after["events"], before["events"], "不得残留半截事件")
        with tx() as conn:
            self.assertEqual(drift_counts(conn), {"桩位漂移数": 0, "枪线漂移数": 0})

    def test_04_concurrent_open_and_abort_never_false_green(self):
        with tx() as conn:
            pile_id, _ = _first_pile_gun(conn)

        def race():
            # 先开一笔，再与第二笔开充并发急停
            r1 = _open(pile_id)
            barrier = threading.Barrier(2)
            out = {}

            def opener():
                barrier.wait()
                try:
                    out["open"] = _open(pile_id)
                except HTTPException as e:
                    out["open_err"] = e.status_code

            def aborter():
                barrier.wait()
                out["abort"] = _abort(r1["会话编号"])

            t1 = threading.Thread(target=opener)
            t2 = threading.Thread(target=aborter)
            t1.start(); t2.start(); t1.join(); t2.join()
            return out

        out = race()
        with tx() as conn:
            holders = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中','已超时')",
                (pile_id,),
            ).fetchone()["c"]
            pile = conn.execute("SELECT status FROM piles WHERE id=?", (pile_id,)).fetchone()
            rep = replay_occupancy(conn)

        if holders == 0:
            self.assertEqual(pile["status"], "空闲", "无持有者时桩必须空闲，不得假占")
        else:
            self.assertEqual(pile["status"], "占用", "有持有者时桩必须占用，不得假绿")
        self.assertTrue(rep["一致"], "任意交错后占用仍须可由事件流水重放")
        # 若第二笔开充成功，回收
        if "open" in out:
            with tx() as conn:
                sid = conn.execute(
                    "SELECT id FROM sessions WHERE pile_id=? AND status='充电中'", (pile_id,)
                ).fetchone()["id"]
                svc.abort_session(conn, sid, "测试回收")

    def test_05_settle_and_abort_guard_release(self):
        with tx() as conn:
            pile_id, _ = _first_pile_gun(conn)
        r = _open(pile_id)
        with tx() as conn:
            svc.settle(conn, r["会话编号"])
            pile = conn.execute("SELECT status FROM piles WHERE id=?", (pile_id,)).fetchone()
            se = conn.execute("SELECT status FROM sessions WHERE id=?", (r["会话编号"],)).fetchone()
        self.assertEqual(pile["status"], "空闲")
        self.assertEqual(se["status"], "已关账")

        with self.assertRaises(HTTPException) as cm:
            with tx() as conn:
                svc.abort_session(conn, r["会话编号"])
        self.assertEqual(cm.exception.status_code, 400)
        with tx() as conn:
            self.assertTrue(replay_occupancy(conn)["一致"])

    def test_06_overview_reports_zero_drift_in_healthy_store(self):
        with tx() as conn:
            o = overview(conn)
            self.assertEqual(o["桩位漂移数"], 0)
            self.assertEqual(o["枪线漂移数"], 0)
            self.assertEqual(o["落账口径"], "同笔多写原子落账")

    def test_07_injected_drift_is_surfaced_not_hidden(self):
        with tx() as conn:
            pile_id, gun_id = _first_pile_gun(conn)
            r = svc.open_session(conn, pile_id)

            conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (pile_id,))
            drift = drift_counts(conn)
            rep = replay_occupancy(conn)
            o = overview(conn)
        self.assertGreaterEqual(drift["桩位漂移数"], 1)
        self.assertIn(pile_id, rep["桩位不一致编号"])
        self.assertFalse(rep["一致"], "重放必须识别出占用与流水不符")
        self.assertGreaterEqual(o["桩位漂移数"], 1, "总览须显式标红，不得伪装已对齐")
        # 健康读路径下枪线仍一致
        self.assertEqual(drift["枪线漂移数"], 0)
        # 回收：先把桩恢复占用再正常急停，避免污染其它用例
        with tx() as conn:
            conn.execute("UPDATE piles SET status='占用' WHERE id=?", (pile_id,))
            svc.abort_session(conn, r["会话编号"], "测试回收")
        with tx() as conn:
            self.assertEqual(drift_counts(conn), {"桩位漂移数": 0, "枪线漂移数": 0})

    def test_08_lock_retry_happens_only_at_enter(self):
        import sqlite3 as _sq
        from unittest import mock
        from app import db as dbmod

        real_connect = dbmod.connect

        def make_patched(fail_first: bool):
            state = {"begin": 0}

            class FakeConn:
                def __init__(self, real):
                    self._real = real

                def __getattr__(self, name):
                    return getattr(self._real, name)

                def execute(self, sql, *a, **k):
                    if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                        state["begin"] += 1
                        if fail_first and state["begin"] == 1:
                            raise _sq.OperationalError("database is locked")
                    return self._real.execute(sql, *a, **k)

            def fake_connect():
                real = real_connect()
                real.isolation_level = None
                return FakeConn(real)

            return fake_connect, state

        # 首次取锁撞锁：必须在 __enter__ 安全重试并成功（旧生成器写法会抛 RuntimeError）
        fake_connect, state = make_patched(fail_first=True)
        with mock.patch.object(dbmod, "connect", fake_connect):
            with dbmod.tx() as conn:
                conn.execute("SELECT 1")
        self.assertEqual(state["begin"], 2, "首次取锁失败应重试一次")

        # with 体内业务异常：只回滚不重试（BEGIN 只出现一次）
        fake_connect2, state2 = make_patched(fail_first=False)
        with mock.patch.object(dbmod, "connect", fake_connect2):
            with self.assertRaises(RuntimeError):
                with dbmod.tx() as conn:
                    conn.execute("SELECT 1")
                    raise RuntimeError("体内失败")
        self.assertEqual(state2["begin"], 1, "体内失败不得触发取锁重试")

    # —— 夹具 F1：双钉只成一侧 —— 枪钉失败必须整笔回滚，不留半截占用 ——
    def test_09_fixture_one_sided_pin_rolls_back_entirely(self):
        with tx() as conn:
            pile_id, gun_id = _first_pile_gun(conn)
            # 让该桩全部枪线进入故障态：钉桩可成、钉枪必败
            conn.execute(
                "UPDATE guns SET status='故障' WHERE pile_id=?", (pile_id,)
            )
        before = self._snapshot()

        # 显式枪线开充：钉枪条件不满足 → 409，整笔回滚
        with self.assertRaises(HTTPException) as cm:
            with tx() as conn:
                svc.open_session(conn, pile_id, gun_id)
        self.assertEqual(cm.exception.status_code, 409)

        # 自动选枪开充：无可用枪线 → 400，同样整笔回滚
        with self.assertRaises(HTTPException) as cm2:
            with tx() as conn:
                svc.open_session(conn, pile_id)
        self.assertEqual(cm2.exception.status_code, 400)

        after = self._snapshot()
        self.assertEqual(after["piles"][pile_id], "空闲", "桩钉必须随整笔回滚，不得只成一侧")
        self.assertEqual(after["guns"][gun_id], "故障", "枪态须回到开充前快照")
        self.assertEqual(after["sessions"], before["sessions"], "不得残留会话行")
        self.assertEqual(after["events"], before["events"], "不得残留半截开充事件")
        with tx() as conn:
            self.assertEqual(drift_counts(conn), {"桩位漂移数": 0, "枪线漂移数": 0})
            # 恢复枪线可用，供后续用例使用
            conn.execute(
                "UPDATE guns SET status='可用' WHERE pile_id=?", (pile_id,)
            )

    # —— 夹具 F2：会话行有双钉空 —— 漂移必须显式标红，不得伪装已对齐 ——
    def test_10_fixture_session_row_with_empty_pins_is_flagged(self):
        with tx() as conn:
            pile_id, gun_id = _first_pile_gun(conn)
        r = _open(pile_id)
        # 注入漂移：会话行仍是充电中，双钉却被掏空
        with tx() as conn:
            conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (pile_id,))
            conn.execute("UPDATE guns SET status='可用' WHERE id=?", (gun_id,))
        with tx() as conn:
            drift = drift_counts(conn)
            rep = replay_occupancy(conn)
            o = overview(conn)
        self.assertGreaterEqual(drift["桩位漂移数"], 1, "桩钉空必须计入漂移")
        self.assertGreaterEqual(drift["枪线漂移数"], 1, "枪钉空必须计入漂移")
        self.assertIn(pile_id, rep["桩位不一致编号"])
        self.assertIn(gun_id, rep["枪线不一致编号"])
        self.assertFalse(rep["一致"], "重放必须识别双钉空")
        self.assertGreaterEqual(o["桩位漂移数"], 1, "总览须显式标红桩漂移")
        self.assertGreaterEqual(o["枪线漂移数"], 1, "总览须显式标红枪漂移")
        # 恢复双钉后正常急停回收，不得污染其它用例
        with tx() as conn:
            conn.execute("UPDATE piles SET status='占用' WHERE id=?", (pile_id,))
            conn.execute("UPDATE guns SET status='占用' WHERE id=?", (gun_id,))
            svc.abort_session(conn, r["会话编号"], "测试回收")
        with tx() as conn:
            self.assertEqual(drift_counts(conn), {"桩位漂移数": 0, "枪线漂移数": 0})

    # —— 夹具 F3：事件成功总览未加 —— 回执/会话行/双钉/事件/总览桶须同命运 ——
    def test_11_fixture_event_and_overview_bucket_share_fate(self):
        with tx() as conn:
            pile_id, gun_id = _first_pile_gun(conn)
            before = overview(conn)
        r = _open(pile_id)
        self.assertEqual(r["状态"], "充电中")
        self.assertFalse(any(k.startswith("surface") for k in r), "回执不得夹带刷漆层字段")
        with tx() as conn:
            se = conn.execute(
                "SELECT status FROM sessions WHERE id=?", (r["会话编号"],)
            ).fetchone()
            ev = conn.execute(
                "SELECT id FROM session_events WHERE session_id=? AND action='开会话'",
                (r["会话编号"],),
            ).fetchone()
            pile = conn.execute("SELECT status FROM piles WHERE id=?", (pile_id,)).fetchone()
            gun = conn.execute("SELECT status FROM guns WHERE id=?", (gun_id,)).fetchone()
            after = overview(conn)
            real_open = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE status='充电中'"
            ).fetchone()["c"]
        # 同命运：回执绿灯 ⟺ 会话行充电中 ⟺ 双钉占用 ⟺ 开充事件在册
        self.assertEqual(se["status"], "充电中")
        self.assertIsNotNone(ev, "开充事件必须与回执同命运")
        self.assertEqual(pile["status"], "占用")
        self.assertEqual(gun["status"], "占用")
        # 总览充电中桶必须随落账加一，且恒等于真实会话计数（非刷漆常数）
        self.assertEqual(after["充电中"], before["充电中"] + 1, "事件成功总览桶必须同加")
        self.assertEqual(after["充电中"], real_open)
        self.assertEqual(after["桩位占用"], before["桩位占用"] + 1)
        self.assertFalse(any(k.startswith("surface") for k in after), "总览不得夹带刷漆层字段")
        # 急停回退后桶必须同减，对账可重放
        _abort(r["会话编号"])
        with tx() as conn:
            back = overview(conn)
            self.assertEqual(back["充电中"], before["充电中"])
            self.assertEqual(back["桩位占用"], before["桩位占用"])
            self.assertTrue(replay_occupancy(conn)["一致"])

    # —— 夹具 F5（顺序版）：连点开充 —— 第二笔 409，不得留幽灵占用 ——
    def test_12_fixture_rapid_double_click_no_ghost_occupancy(self):
        with tx() as conn:
            pile_id, _ = _first_pile_gun(conn)
        r = _open(pile_id)
        # 连点第二笔：同一桩立即再开 → 必须 409 而非半截成功
        with self.assertRaises(HTTPException) as cm:
            _open(pile_id)
        self.assertEqual(cm.exception.status_code, 409)
        with tx() as conn:
            holders = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中','已超时')",
                (pile_id,),
            ).fetchone()["c"]
            opens = conn.execute(
                "SELECT COUNT(*) AS c FROM session_events WHERE session_id=? AND action='开会话'",
                (r["会话编号"],),
            ).fetchone()["c"]
            pile = conn.execute("SELECT status FROM piles WHERE id=?", (pile_id,)).fetchone()
            self.assertEqual(drift_counts(conn), {"桩位漂移数": 0, "枪线漂移数": 0})
        self.assertEqual(holders, 1, "连点只能成交一笔，不得双终态挂账")
        self.assertEqual(opens, 1, "开充事件只能有一条，不得幽灵复写")
        self.assertEqual(pile["status"], "占用")
        _abort(r["会话编号"])
        with tx() as conn:
            self.assertEqual(drift_counts(conn), {"桩位漂移数": 0, "枪线漂移数": 0})

    # —— 脉冲入口误触：已终结会话不得继续加脏电量 ——
    def test_13_pulse_on_terminated_session_adds_no_dirty_kwh(self):
        with tx() as conn:
            pile_id, _ = _first_pile_gun(conn)
        r = _open(pile_id)
        _abort(r["会话编号"])  # 急停终结
        with self.assertRaises(HTTPException) as cm:
            with tx() as conn:
                svc.add_pulse(conn, r["会话编号"], 2.5, 7)
        self.assertEqual(cm.exception.status_code, 400)
        with tx() as conn:
            se = conn.execute(
                "SELECT kwh, amount FROM sessions WHERE id=?", (r["会话编号"],)
            ).fetchone()
            pulses = conn.execute(
                "SELECT COUNT(*) AS c FROM pulses WHERE session_id=?", (r["会话编号"],)
            ).fetchone()["c"]
            pulse_events = conn.execute(
                "SELECT COUNT(*) AS c FROM session_events WHERE session_id=? AND action='脉冲'",
                (r["会话编号"],),
            ).fetchone()["c"]
        self.assertEqual(float(se["kwh"]), 0.0, "误触不得加脏电量")
        self.assertEqual(pulses, 0)
        self.assertEqual(pulse_events, 0, "误触不得留脉冲事件")

        # 已关账同样拒绝误触
        with tx() as conn:
            pile_id2, _ = _first_pile_gun(conn)
        r2 = _open(pile_id2)
        with tx() as conn:
            svc.settle(conn, r2["会话编号"])
        with self.assertRaises(HTTPException) as cm2:
            with tx() as conn:
                svc.add_pulse(conn, r2["会话编号"], 1.0, 7)
        self.assertEqual(cm2.exception.status_code, 400)
        with tx() as conn:
            self.assertTrue(replay_occupancy(conn)["一致"])

    # —— 旁路改占用钉字：占用钉只能由开充/结算/急停事务改写 ——
    def test_14_manual_occupancy_pin_is_forbidden(self):
        with tx() as conn:
            pile_id, _ = _first_pile_gun(conn)
            with self.assertRaises(HTTPException) as cm:
                svc.set_pile_status(conn, pile_id, "占用")
            self.assertEqual(cm.exception.status_code, 400)
            with self.assertRaises(HTTPException):
                svc.set_pile_status(conn, pile_id, "爆炸")
        with tx() as conn:
            pile = conn.execute("SELECT status FROM piles WHERE id=?", (pile_id,)).fetchone()
            self.assertEqual(pile["status"], "空闲", "旁路改钉不得生效")
            self.assertEqual(drift_counts(conn), {"桩位漂移数": 0, "枪线漂移数": 0})

    # —— 假多写落账收口：刷漆层清零，历史残漆启动即除，全链路只认原子多写 ——
    def test_15_fake_multiwrite_surface_is_closed_out(self):
        import importlib.util

        self.assertIsNone(
            importlib.util.find_spec("app.services.claim_surface"),
            "刷漆层模块必须整体移除，全局只留同笔多写原子落账",
        )
        # 模拟历史库已被刷漆：残漆须在 init_db 时被幂等清除
        with tx() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('surface_last','{\"ok\": true, \"充电中\": 1}') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('surface_ok','1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
        init_db()
        with tx() as conn:
            residue = conn.execute(
                "SELECT COUNT(*) AS c FROM settings WHERE key LIKE 'surface%'"
            ).fetchone()["c"]
        self.assertEqual(residue, 0, "历史残漆必须被清除，不得再覆盖总览")
        # 开充→脉冲→结算全链路走完后，回执/总览/settings 均无刷漆痕迹
        with tx() as conn:
            pile_id, _ = _first_pile_gun(conn)
        r = _open(pile_id)
        self.assertFalse(any(k.startswith("surface") for k in r))
        with tx() as conn:
            svc.add_pulse(conn, r["会话编号"], 1.5, 7)
            svc.settle(conn, r["会话编号"])
            o = overview(conn)
            residue = conn.execute(
                "SELECT COUNT(*) AS c FROM settings WHERE key LIKE 'surface%'"
            ).fetchone()["c"]
        self.assertFalse(any(k.startswith("surface") for k in o))
        self.assertEqual(residue, 0)
        with tx() as conn:
            self.assertEqual(drift_counts(conn), {"桩位漂移数": 0, "枪线漂移数": 0})
            self.assertTrue(replay_occupancy(conn)["一致"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
