"""
FinEdge LinkedIn Content Microservice (AutoGen-style multi-agent pipeline)

Endpoints
  GET  /health     -> liveness check + current mode (llm or mock)
  POST /linkedin   -> ideas, selected idea, draft, confidence score, hashtags
  POST /log        -> append one run record to logs/run_log.csv (called by n8n)
  GET  /log        -> return the most recent log rows (for review / screenshots)

Agent chain (same pattern as Lesson 02 Demo 01: one AssistantAgent per role,
called in sequence by a Python orchestrator):
  1. Ideator      -> N post ideas grounded in brand pillars and today's context
  2. Writer       -> full LinkedIn draft for the strongest idea
  3. Reviewer     -> scores the draft against a rubric (0.0 - 1.0)
  4. Hashtagger   -> 3-5 relevant hashtags
Then deterministic guardrails (banned terms, risky financial claims, length)
adjust the confidence score. n8n uses that score in its Approval Gate.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
Mock mode (no Azure calls): set MOCK_MODE=true in .env
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import json
import logging
import os
import pathlib
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

try:  # optional: load .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# ---------------------------------------------------------------------------
# Configuration (all values come from the environment / .env)
# ---------------------------------------------------------------------------
AZURE_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
AZURE_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
AZURE_MODEL = os.environ.get("AZURE_OPENAI_MODEL", AZURE_DEPLOYMENT)

MOCK_MODE = os.environ.get("MOCK_MODE", "false").strip().lower() in {"1", "true", "yes"}
LLM_TIMEOUT_SEC = float(os.environ.get("LLM_TIMEOUT_SEC", "120"))
SLEEP_BETWEEN_CALLS_SEC = float(os.environ.get("RATE_SLEEP_SEC", "0.5"))

LOG_DIR = pathlib.Path(os.environ.get("LOG_DIR", "logs"))
RUNS_JSONL = LOG_DIR / "runs.jsonl"   # full detail of every /linkedin call
RUN_LOG_CSV = LOG_DIR / "run_log.csv"  # one row per n8n run (spreadsheet-friendly)
CSV_FIELDS = [
    "timestamp", "run_id", "route", "status", "confidence", "threshold",
    "dry_run", "mode", "topic", "hashtags", "draft_preview", "error",
]

LINKEDIN_MAX_CHARS = 3000  # LinkedIn post text limit; verify against current LinkedIn docs

# Phrases a fintech compliance reviewer would normally reject in marketing copy.
RISKY_FINANCIAL_PATTERNS = [
    r"guaranteed\s+returns?", r"risk[-\s]?free", r"double\s+your\s+money",
    r"assured\s+returns?", r"get\s+rich", r"no\s+risk", r"100%\s+safe",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("linkedin-service")


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def append_jsonl(path: pathlib.Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# API schemas
# ---------------------------------------------------------------------------
class Brand(BaseModel):
    name: str = "FinEdge"
    voice: str = "Authoritative, clear, optimistic, never hype-driven"
    audience: str = "Indian retail investors, SME founders, fintech professionals"
    pillars: List[str] = Field(default_factory=lambda: ["Financial literacy", "Digital payments", "Responsible investing"])
    banned_terms: List[str] = Field(default_factory=lambda: ["guaranteed returns", "risk-free"])
    call_to_action: str = "Follow FinEdge for practical money insights."


class Context(BaseModel):
    topic: str = "How UPI is changing small-business cash flow"
    notes: str = ""
    audience_segment: Optional[str] = None


class LinkedInRequest(BaseModel):
    brand: Brand = Field(default_factory=Brand)
    context: Context = Field(default_factory=Context)
    num_ideas: int = Field(default=3, ge=1, le=5)
    max_hashtags: int = Field(default=5, ge=1, le=10)


class LogRecord(BaseModel):
    run_id: str = ""
    route: str = ""               # e.g. "slack_review", "linkedin_published", "error"
    status: str = "ok"
    confidence: Optional[float] = None
    threshold: Optional[float] = None
    dry_run: Optional[bool] = None
    mode: str = ""
    topic: str = ""
    hashtags: Any = None          # list or string
    draft: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# JSON helpers (LLM output is text; we ask for JSON and parse defensively)
# ---------------------------------------------------------------------------
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Parse JSON from a fenced block, the raw text, or the first {...}/[...] span."""
    text = (text or "").strip()
    candidates = []
    m = JSON_BLOCK_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(text)
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
    raise ValueError(f"Could not parse JSON from model output: {text[:200]!r}")


def normalize_hashtags(raw: Any, limit: int) -> List[str]:
    if isinstance(raw, dict):
        raw = raw.get("hashtags", [])
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    tags, seen = [], set()
    for t in raw or []:
        t = re.sub(r"[^\w]", "", str(t).lstrip("#"))  # letters/digits/underscore only
        if t and t.lower() not in seen:
            seen.add(t.lower())
            tags.append("#" + t)
    return tags[:limit]


# ---------------------------------------------------------------------------
# Guardrails: deterministic checks that adjust the reviewer's score
# ---------------------------------------------------------------------------
def apply_guardrails(draft: str, hashtags: List[str], reviewer_score: float, brand: Brand) -> Dict[str, Any]:
    flags: List[str] = []
    penalty = 0.0
    lower = draft.lower()

    hits = [t for t in brand.banned_terms if t and t.lower() in lower]
    if hits:
        flags.append(f"banned_terms:{', '.join(hits)}")
        penalty += 0.30

    risky = [p for p in RISKY_FINANCIAL_PATTERNS if re.search(p, lower)]
    if risky:
        flags.append("risky_financial_claim")
        penalty += 0.30

    full_len = len(draft) + 2 + len(" ".join(hashtags))
    if full_len > LINKEDIN_MAX_CHARS:
        flags.append(f"too_long:{full_len}_chars")
        penalty += 0.20

    if len(draft.strip()) < 200:
        flags.append("too_short")
        penalty += 0.10

    if not hashtags:
        flags.append("no_hashtags")
        penalty += 0.05

    reviewer_score = max(0.0, min(1.0, float(reviewer_score)))
    final = round(max(0.0, min(1.0, reviewer_score - penalty)), 2)
    return {"reviewer_score": round(reviewer_score, 2), "penalty": round(penalty, 2),
            "flags": flags, "confidence": final}


# ---------------------------------------------------------------------------
# Agent prompts
# ---------------------------------------------------------------------------
def brand_brief(req: LinkedInRequest) -> str:
    b, c = req.brand, req.context
    return (
        f"BRAND: {b.name}\nVOICE: {b.voice}\nAUDIENCE: {c.audience_segment or b.audience}\n"
        f"CONTENT PILLARS: {', '.join(b.pillars)}\nNEVER USE: {', '.join(b.banned_terms)}\n"
        f"CALL TO ACTION: {b.call_to_action}\nTOPIC / CONTEXT: {c.topic}\nNOTES: {c.notes or 'none'}"
    )


SYSTEM_IDEATOR = (
    "You are Ideator, a B2B fintech content strategist. Propose distinct LinkedIn post ideas "
    "that fit the brand pillars and the given topic. Reply with ONLY a JSON object: "
    '{"ideas": [{"title": str, "angle": str, "pillar": str}]}'
)
SYSTEM_WRITER = (
    "You are Writer, a LinkedIn copywriter for a regulated fintech company. Write ONE post "
    "for the chosen idea: a strong first-line hook, 3-5 short paragraphs, plain language, "
    "one concrete takeaway, end with the call to action. 120-250 words. No hashtags, no emojis "
    "overload, no promises of returns, no invented statistics. Reply with ONLY a JSON object: "
    '{"selected_idea": str, "draft": str}'
)
SYSTEM_REVIEWER = (
    "You are Reviewer, a brand and compliance editor. Score the draft from 0.0 to 1.0 using "
    "this rubric (equal weight): brand voice fit, clarity, factual caution (no unverifiable "
    "numbers, no return promises), audience relevance, hook strength. Reply with ONLY a JSON "
    'object: {"score": float, "reasons": [str], "suggested_fix": str}'
)
SYSTEM_HASHTAGGER = (
    "You are Hashtagger. Suggest relevant, commonly used LinkedIn hashtags for the post. "
    "Mix 1-2 broad tags with specific ones. Reply with ONLY a JSON object: "
    '{"hashtags": ["#Example"]}'
)


# ---------------------------------------------------------------------------
# LLM pipeline (AutoGen AgentChat)
# ---------------------------------------------------------------------------
_model_client = None


def get_model_client():
    """Create the Azure OpenAI client once; it is stateless and safe to reuse."""
    global _model_client
    if _model_client is None:
        from autogen_ext.models.openai import AzureOpenAIChatCompletionClient

        _model_client = AzureOpenAIChatCompletionClient(
            azure_endpoint=AZURE_ENDPOINT,
            api_key=AZURE_API_KEY,
            api_version=AZURE_API_VERSION,
            model=AZURE_MODEL,
            azure_deployment=AZURE_DEPLOYMENT,
        )
    return _model_client


async def ask_agent(name: str, system_message: str, task: str, run_id: str) -> Any:
    """Run one agent turn and return parsed JSON. Retries once if JSON is malformed.

    A fresh AssistantAgent is created per call because AssistantAgent keeps
    conversation history; reusing one across HTTP requests would leak earlier
    posts into later prompts.
    """
    from autogen_agentchat.agents import AssistantAgent

    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        agent = AssistantAgent(name=name, system_message=system_message, model_client=get_model_client())
        prompt = task if attempt == 1 else task + "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON object."
        await asyncio.sleep(SLEEP_BETWEEN_CALLS_SEC)
        result = await agent.run(task=prompt)
        content = getattr(result.messages[-1], "content", "") if result.messages else ""
        append_jsonl(RUNS_JSONL, {"ts": utc_now(), "run_id": run_id, "kind": "agent_reply",
                                  "agent": name, "attempt": attempt, "content": content})
        try:
            return extract_json(content if isinstance(content, str) else str(content))
        except ValueError as e:
            last_err = e
    raise RuntimeError(f"{name} did not return valid JSON: {last_err}")


async def run_llm_pipeline(req: LinkedInRequest, run_id: str) -> Dict[str, Any]:
    brief = brand_brief(req)

    ideas_obj = await ask_agent("Ideator", SYSTEM_IDEATOR,
                                f"{brief}\n\nPropose exactly {req.num_ideas} ideas.", run_id)
    ideas = ideas_obj.get("ideas", []) if isinstance(ideas_obj, dict) else ideas_obj
    if not ideas:
        raise RuntimeError("Ideator returned no ideas")

    ideas_text = "\n".join(f"{i + 1}. {x.get('title', x) if isinstance(x, dict) else x}" for i, x in enumerate(ideas))
    draft_obj = await ask_agent("Writer", SYSTEM_WRITER,
                                f"{brief}\n\nIDEAS:\n{ideas_text}\n\nPick the strongest idea and write the post.", run_id)
    draft = str(draft_obj.get("draft", "")).strip()
    if not draft:
        raise RuntimeError("Writer returned an empty draft")

    review = await ask_agent("Reviewer", SYSTEM_REVIEWER, f"{brief}\n\nDRAFT:\n{draft}", run_id)
    tags_obj = await ask_agent("Hashtagger", SYSTEM_HASHTAGGER,
                               f"{brief}\n\nPOST:\n{draft}\n\nReturn up to {req.max_hashtags} hashtags.", run_id)

    return {
        "ideas": ideas,
        "selected_idea": draft_obj.get("selected_idea", ""),
        "draft": draft,
        "review": {"score": review.get("score", 0.0), "reasons": review.get("reasons", []),
                   "suggested_fix": review.get("suggested_fix", "")},
        "hashtags_raw": tags_obj,
    }


# ---------------------------------------------------------------------------
# Mock pipeline (no network): lets you wire and test n8n without Azure calls
# ---------------------------------------------------------------------------
def run_mock_pipeline(req: LinkedInRequest) -> Dict[str, Any]:
    b, c = req.brand, req.context
    pillars = b.pillars or ["Financial literacy"]
    ideas = [
        {"title": f"{c.topic}: what it means for {p.lower()}", "angle": "practical explainer", "pillar": p}
        for p in (pillars * req.num_ideas)[: req.num_ideas]
    ]
    draft = (
        f"{c.topic} is no longer a future trend. It is shaping decisions today.\n\n"
        f"At {b.name}, we see three practical shifts for {c.audience_segment or b.audience}: faster "
        "settlement, clearer visibility of cash flow, and fewer manual reconciliations.\n\n"
        "The opportunity is not in chasing every new tool. It is in choosing the few that remove "
        "friction from how money moves through your business.\n\n"
        "One takeaway: review last month's payments and note where delays or manual steps cost you time.\n\n"
        f"{b.call_to_action}"
    )
    return {
        "ideas": ideas,
        "selected_idea": ideas[0]["title"],
        "draft": draft,
        "review": {"score": 0.86, "reasons": ["MOCK MODE: fixed score for pipeline testing"], "suggested_fix": ""},
        "hashtags_raw": ["#FinEdge", "#Fintech", "#DigitalPayments", "#SmallBusiness", "#FinancialLiteracy"],
    }


async def generate_post(req: LinkedInRequest) -> Dict[str, Any]:
    run_id = uuid.uuid4().hex[:12]
    mode = "mock" if (MOCK_MODE or not AZURE_API_KEY) else "llm"
    started = utc_now()

    if mode == "mock":
        raw = run_mock_pipeline(req)
    else:
        raw = await asyncio.wait_for(run_llm_pipeline(req, run_id), timeout=LLM_TIMEOUT_SEC)

    hashtags = normalize_hashtags(raw["hashtags_raw"], req.max_hashtags)
    guard = apply_guardrails(raw["draft"], hashtags, raw["review"]["score"], req.brand)

    response = {
        "run_id": run_id,
        "mode": mode,
        "model": AZURE_DEPLOYMENT if mode == "llm" else "mock",
        "created_at": started,
        "ideas": raw["ideas"],
        "selected_idea": raw["selected_idea"],
        "draft": raw["draft"],
        "hashtags": hashtags,
        "confidence": guard["confidence"],
        "confidence_breakdown": {
            "reviewer_score": guard["reviewer_score"],
            "guardrail_penalty": guard["penalty"],
            "flags": guard["flags"],
            "reviewer_reasons": raw["review"]["reasons"],
            "suggested_fix": raw["review"]["suggested_fix"],
        },
        "char_count": len(raw["draft"]),
    }
    append_jsonl(RUNS_JSONL, {"ts": utc_now(), "run_id": run_id, "kind": "result",
                              "request": req.model_dump(), "response": response})
    return response


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    mode = "mock" if (MOCK_MODE or not AZURE_API_KEY) else "llm"
    log.info("Starting LinkedIn service in %s mode (deployment=%s)", mode, AZURE_DEPLOYMENT)
    yield
    if _model_client is not None:
        await _model_client.close()


app = FastAPI(title="FinEdge LinkedIn AutoGen Microservice", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "mode": "mock" if (MOCK_MODE or not AZURE_API_KEY) else "llm",
            "deployment": AZURE_DEPLOYMENT, "time": utc_now()}


@app.post("/linkedin")
async def linkedin(req: LinkedInRequest) -> Dict[str, Any]:
    try:
        return await generate_post(req)
    except asyncio.TimeoutError:
        log.error("LLM pipeline timed out after %ss", LLM_TIMEOUT_SEC)
        append_jsonl(RUNS_JSONL, {"ts": utc_now(), "kind": "error", "error": "timeout"})
        raise HTTPException(status_code=504, detail=f"Agent pipeline timed out after {LLM_TIMEOUT_SEC}s")
    except Exception as e:  # surface a clean JSON error so n8n's error branch can log it
        log.exception("Pipeline failed")
        append_jsonl(RUNS_JSONL, {"ts": utc_now(), "kind": "error", "error": str(e)})
        raise HTTPException(status_code=502, detail=f"Agent pipeline failed: {e}")


@app.post("/log")
async def write_log(rec: LogRecord) -> Dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tags = rec.hashtags if isinstance(rec.hashtags, str) else " ".join(rec.hashtags or [])
    row = {
        "timestamp": utc_now(), "run_id": rec.run_id, "route": rec.route, "status": rec.status,
        "confidence": rec.confidence, "threshold": rec.threshold, "dry_run": rec.dry_run,
        "mode": rec.mode, "topic": rec.topic, "hashtags": tags,
        "draft_preview": (rec.draft or "").replace("\n", " ")[:200], "error": rec.error,
    }
    new_file = not RUN_LOG_CSV.exists()
    with RUN_LOG_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)
    return {"logged": True, "file": str(RUN_LOG_CSV), "row": row}


@app.get("/log")
async def read_log(limit: int = 20) -> Dict[str, Any]:
    if not RUN_LOG_CSV.exists():
        return {"rows": []}
    with RUN_LOG_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {"count": len(rows), "rows": rows[-limit:]}
