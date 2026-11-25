"""AI Command Center."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..ai import assistant
from ..auth import current_user
from ..config import get_settings
from ..db import WRITE_LOCK, get_db
from ..services.common import sim_now

router = APIRouter(prefix="/api/ai")


class ChatIn(BaseModel):
    message: str
    factory: str = "ALL"
    history: list[dict] = []


@router.post("/chat")
def chat(body: ChatIn, db: Session = Depends(get_db), user=Depends(current_user)):
    with WRITE_LOCK:  # the optimizer tool records a proposal
        res = assistant.answer(db, body.message.strip()[:1000], body.factory, sim_now(db), body.history)
        db.commit()
    return res


@router.get("/config")
def config(user=Depends(current_user)):
    s = get_settings()
    return {"provider": s.llm_provider, "model": s.llm_model if s.llm_provider != "mock" else None,
            "examples": assistant.EXAMPLES,
            "tools": [{"name": k, "description": v[1]} for k, v in assistant.T.TOOLS.items()]}
