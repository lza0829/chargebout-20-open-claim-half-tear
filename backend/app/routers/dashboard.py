from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services import core
from app.services.ledger import drift_counts, replay_occupancy

router = APIRouter()


@router.get("/总览")
def get_overview():
    with db() as conn:
        return core.overview(conn)


@router.get("/一致性")
def get_consistency():
    """读路径占用按选定落账次序重放，并给出漂移计数；只暴露不掩盖。"""
    with db() as conn:
        return {**replay_occupancy(conn), **drift_counts(conn)}

