from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "充桩条"
ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("CHARGEBOUT_DB", str(ROOT / "data" / "chargebout.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

PILE_STATUSES = ("空闲", "占用", "故障", "维护")
GUN_STATUSES = ("可用", "占用", "故障")
SESSION_STATUSES = ("充电中", "结算中", "已关账", "已超时", "已中断")
OPEN_SESSIONS = ("充电中", "结算中")
# 占用持有者：未终结会话仍钉着桩/枪（已超时仅业务标记，未释放占用）
HOLDER_STATUSES = ("充电中", "结算中", "已超时")
# 全局唯一落账架构：同笔多写、同成败、整笔回滚；禁止与「先占后补」混用
LEDGER_POLICY = "同笔多写原子落账"
