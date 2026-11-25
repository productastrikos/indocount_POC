"""AI Command Center orchestration.

Providers
  * mock      - local intent router: picks data tools from the question,
                runs them against the database and composes the answer.
                No network, always available (default).
  * anthropic - Claude with tool use: the model chooses which data tools to
                call; every figure in the answer comes from tool results.
                Falls back to the mock provider if the API is unavailable.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import get_settings
from . import tools as T

log = logging.getLogger("indocount.ai")

FACTORY_WORDS = {"KNV": ("kernersville", "knv", "north carolina"), "PHX": ("phoenix", "phx", "arizona"),
                 "CMH": ("columbus", "cmh", "ohio")}

INTENTS = [  # (tool, patterns) — first matching rules win; several tools can match
    ("order_status", [r"\bord-?\s?\d{3}\b"]),
    ("material_runout", [r"run(ning)? out", r"runout", r"stock.?out", r"shortage", r"days? of (stock|cover)", r"material"]),
    ("supplier_delays", [r"supplier", r"vendor", r"late deliver", r"inbound delay"]),
    ("line_allocation", [r"allocat", r"best line", r"optimi[sz]", r"sequence", r"schedul(e|ing) (today|orders)", r"which line should"]),
    ("production_gap", [r"why .*(behind|below|short|miss)", r"behind (target|plan|schedule)", r"below target", r"gap"]),
    ("line_downtime", [r"downtime", r"stopp", r"breakdown"]),
    ("labour_cost", [r"labou?r cost", r"cost per pillow", r"wage", r"payroll"]),
    ("idle_time", [r"idle"]),
    ("workforce_productivity", [r"productiv", r"employee", r"worker", r"operator", r"benchmark", r"manpower", r"staff"]),
    ("carton_failures", [r"fail(ed)? inspection", r"carton.*fail", r"defect", r"reject", r"blocked carton", r"mismatch"]),
    ("packaging_errors_by_customer", [r"error rate", r"packaging error", r"customer.*(error|accuracy)", r"(error|accuracy).*customer"]),
    ("orders_waiting_for_picking", [r"pick(ing)?", r"waiting for pick"]),
    ("locate_sku", [r"where is", r"stored", r"locat(e|ion) of", r"find sku"]),
    ("trucks_at_risk", [r"truck", r"appointment", r"at risk", r"miss(ing)? (the|their)? ?(appointment|truck|shipment)"]),
    ("detention_cost", [r"detention", r"waiting charge", r"demurrage"]),
    ("machine_health", [r"machine", r"vibration", r"maintenance", r"health", r"motor", r"predict"]),
    ("inventory_status", [r"inventory", r"finished goods", r"warehouse", r"stock level"]),
    ("kpi_snapshot", [r"\boee\b", r"kpi", r"overview", r"how (are|is) (we|production) doing", r"status", r"summary", r"output", r"efficien"]),
]

EXAMPLES = [
    "Which materials may run out in the next five days?", "Which suppliers are causing delays?",
    "Which production line has the highest downtime?", "Why is production behind target?",
    "What is the best line allocation for today's orders?", "What is the labour cost per pillow?",
    "Which line has the highest idle time?", "How many cartons failed inspection today?",
    "Which customer has the highest packaging error rate?", "Which orders are waiting for picking?",
    "Where is SKU WMT-MS-Q stored?", "Which trucks are at risk of missing their appointments?",
    "What is our estimated detention cost this week?",
]


def detect_factory(text: str, default: str) -> str:
    t = text.lower()
    found = [code for code, words in FACTORY_WORDS.items() if any(w in t for w in words)]
    if len(found) == 1:
        return found[0]
    if "all factories" in t or "across" in t or len(found) > 1:
        return "ALL"
    return default or "ALL"


def detect_args(tool: str, text: str) -> dict:
    t = text.lower()
    args = {}
    if tool in ("material_runout",):
        mm = re.search(r"(\d+|one|two|three|four|five|six|seven|ten)\s*days?", t)
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10}
        if mm:
            args["days"] = float(words.get(mm.group(1), mm.group(1)))
        elif "tomorrow" in t:
            args["days"] = 1.6
        elif "week" in t:
            args["days"] = 7
    if tool in ("detention_cost", "packaging_errors_by_customer"):
        if "today" in t:
            args["days"] = 1
        elif "month" in t:
            args["days"] = 30
        elif "week" in t:
            args["days"] = 7
    if tool == "locate_sku":
        mm = re.search(r"sku\s+([a-z0-9\-]+)", t) or re.search(r"\b([a-z]{3}-[a-z0-9]+(?:-[a-z0-9]+)?)\b", t)
        args["sku"] = mm.group(1).upper() if mm else text.split()[-1].strip("?.").upper()
    if tool == "order_status":
        mm = re.search(r"ord-?\s?(\d{3})", t)
        args["order"] = f"ORD-{mm.group(1)}" if mm else ""
    return args


def route(text: str) -> list[str]:
    t = text.lower()
    hits = []
    for tool, pats in INTENTS:
        if any(re.search(p, t) for p in pats):
            hits.append(tool)
    # resolve common overlaps
    if "locate_sku" in hits:
        return ["locate_sku"]
    if "order_status" in hits:
        return ["order_status"]
    if "detention_cost" in hits:
        hits = [h for h in hits if h != "trucks_at_risk"] if "risk" not in t else hits
    if "production_gap" in hits:
        return ["production_gap"]
    if "line_allocation" in hits:
        return ["line_allocation"]
    if "packaging_errors_by_customer" in hits:
        hits = [h for h in hits if h != "carton_failures"]
    if "idle_time" in hits:
        hits = [h for h in hits if h != "workforce_productivity"]
    if "labour_cost" in hits:
        hits = [h for h in hits if h not in ("workforce_productivity", "kpi_snapshot")]
    if "line_downtime" in hits and "machine_health" in hits and "breakdown" not in t:
        hits = [h for h in hits if h != "machine_health"]
    if len(hits) > 1 and "kpi_snapshot" in hits:
        hits.remove("kpi_snapshot")
    return hits[:2]


def _package(results: list[tuple[str, dict]], answer: str, mode: str, note: str | None = None) -> dict:
    charts = [r["chart"] for _, r in results if r.get("chart")]
    tables = [r["table"] for _, r in results if r.get("table")]
    actions = [a for _, r in results for a in r.get("actions", [])]
    links = [lk for _, r in results for lk in r.get("links", [])]
    return {"answer": answer, "charts": charts[:2], "tables": tables[:2], "recommendations": actions[:5],
            "links": links[:4], "sources": [name for name, _ in results], "mode": mode, "note": note}


def answer_mock(db: Session, question: str, factory: str, now: datetime, note: str | None = None) -> dict:
    fac = detect_factory(question, factory)
    tools = route(question)
    if not tools:
        return {"answer": "I answer questions from the factory's operational data — procurement, production, workforce, "
                          "quality, warehouse and dispatch. I couldn't map that question to the available data. Try one of the "
                          "suggested questions.", "charts": [], "tables": [], "recommendations": [], "links": [],
                "sources": [], "mode": "mock", "suggestions": EXAMPLES[:6], "note": note}
    results = []
    for name in tools:
        try:
            results.append((name, T.run_tool(db, name, fac, now, detect_args(name, question))))
        except Exception as exc:  # report missing data rather than inventing it
            log.exception("tool %s failed", name)
            results.append((name, {"summary": f"Data for {name.replace('_', ' ')} is unavailable ({exc.__class__.__name__}).",
                                   "actions": [], "links": []}))
    answer = "\n\n".join(r["summary"] for _, r in results)
    return _package(results, answer, "mock", note)


SYSTEM = """You are the operations assistant inside Indocount's Smart Factory Intelligence platform (pillow manufacturing, US plants: KNV Kernersville NC, PHX Phoenix AZ, CMH Columbus OH).

Answer only from the data returned by the tools; never invent figures. Call the tools you need (several if the question spans stages), then answer in 2–5 short sentences or a compact list with the key numbers, the cause, and the recommended action. If the tools do not contain the data needed, say plainly that the data is unavailable. All data is simulated demo data; detention figures are illustrative estimates. Use the factory the user names; otherwise use the factory in the context message."""


def answer_llm(db: Session, question: str, factory: str, now: datetime, history: list[dict]) -> dict:
    import anthropic

    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key) if s.anthropic_api_key else anthropic.Anthropic()
    msgs = [{"role": h["role"], "content": h["content"]} for h in history[-6:] if h.get("role") in ("user", "assistant") and h.get("content")]
    context = f"[Context] Selected factory: {factory or 'ALL'}. Simulated time: {now:%A %Y-%m-%d %H:%M}."
    msgs.append({"role": "user", "content": f"{context}\n\n{question}"})
    tool_specs = T.anthropic_tool_specs()
    results: list[tuple[str, dict]] = []
    final_text = ""
    for _ in range(6):
        resp = client.beta.messages.create(
            model=s.llm_model, max_tokens=16000, system=SYSTEM, tools=tool_specs, messages=msgs,
            output_config={"effort": s.llm_effort}, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            final_text = "I can't help with that request. Please ask about factory operations data."
            break
        msgs.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue
        uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not uses:
            final_text = "".join(b.text for b in resp.content if b.type == "text")
            break
        tool_results = []
        for u in uses:
            args = dict(u.input or {})
            if u.name not in T.TOOLS:
                tool_results.append({"type": "tool_result", "tool_use_id": u.id, "content": f"Unknown tool {u.name}", "is_error": True})
                continue
            try:
                r = T.run_tool(db, u.name, args.pop("factory", None) or factory, now, args)
                results.append((u.name, r))
                payload = {"summary": r["summary"], "data": r.get("data")}
                tool_results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(payload, default=str)[:12000]})
            except Exception as exc:
                tool_results.append({"type": "tool_result", "tool_use_id": u.id, "content": f"Data unavailable: {exc}", "is_error": True})
        msgs.append({"role": "user", "content": tool_results})
    if not final_text:
        final_text = "\n\n".join(r["summary"] for _, r in results) or "The data needed to answer this is unavailable."
    return _package(results, final_text.strip(), "anthropic")


def answer(db: Session, question: str, factory: str, now: datetime, history: list[dict] | None = None) -> dict:
    s = get_settings()
    if s.llm_provider == "anthropic":
        try:
            return answer_llm(db, question, factory, now, history or [])
        except Exception as exc:
            log.warning("LLM provider failed, using local fallback: %s", exc)
            return answer_mock(db, question, factory, now, note=f"LLM unavailable ({exc.__class__.__name__}); answered by the local engine.")
    return answer_mock(db, question, factory, now)
