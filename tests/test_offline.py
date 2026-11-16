"""Offline unit checks for main.py. Stubs fastapi/pydantic/autogen so it runs with plain Python:  python3 tests/test_offline.py"""
import sys, types, asyncio, json, os, tempfile, importlib
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

# --- pydantic stub ---
pyd = types.ModuleType("pydantic")
class _F:
    def __init__(self, default=None, default_factory=None, **kw): self.default, self.default_factory = default, default_factory
def Field(default=None, default_factory=None, **kw): return _F(default, default_factory)
class BaseModel:
    def __init__(self, **kw):
        for cls in reversed(type(self).__mro__):
            for k in getattr(cls, "__annotations__", {}):
                if k in kw: v = kw[k]
                else:
                    d = cls.__dict__.get(k)
                    v = (d.default_factory() if d.default_factory else d.default) if isinstance(d, _F) else d
                setattr(self, k, v)
    def model_dump(self):
        out = {}
        for cls in type(self).__mro__:
            for k in getattr(cls, "__annotations__", {}):
                v = getattr(self, k); out[k] = v.model_dump() if isinstance(v, BaseModel) else v
        return out
pyd.BaseModel, pyd.Field = BaseModel, Field
sys.modules["pydantic"] = pyd

# --- fastapi stub ---
fa = types.ModuleType("fastapi")
class HTTPException(Exception):
    def __init__(self, status_code, detail): self.status_code, self.detail = status_code, detail
class FastAPI:
    def __init__(self, **kw): pass
    def get(self, *a, **k): return lambda f: f
    def post(self, *a, **k): return lambda f: f
fa.FastAPI, fa.HTTPException = FastAPI, HTTPException
sys.modules["fastapi"] = fa

# --- autogen stubs (scripted replies) ---
SCRIPT = {}
CALLS = []
class Msg:
    def __init__(self, c): self.content = c
class Result:
    def __init__(self, c): self.messages = [Msg("task"), Msg(c)]
class AssistantAgent:
    def __init__(self, name, system_message, model_client): self.name = name
    async def run(self, task):
        CALLS.append(self.name); return Result(SCRIPT[self.name].pop(0))
m1 = types.ModuleType("autogen_agentchat"); m2 = types.ModuleType("autogen_agentchat.agents"); m2.AssistantAgent = AssistantAgent
m3 = types.ModuleType("autogen_ext"); m4 = types.ModuleType("autogen_ext.models"); m5 = types.ModuleType("autogen_ext.models.openai")
class AzureOpenAIChatCompletionClient:
    def __init__(self, **kw): self.kw = kw
    async def close(self): pass
m5.AzureOpenAIChatCompletionClient = AzureOpenAIChatCompletionClient
for n, m in [("autogen_agentchat", m1), ("autogen_agentchat.agents", m2), ("autogen_ext", m3), ("autogen_ext.models", m4), ("autogen_ext.models.openai", m5)]:
    sys.modules[n] = m

tmp = tempfile.mkdtemp()
os.environ.update({"LOG_DIR": tmp, "RATE_SLEEP_SEC": "0", "AZURE_OPENAI_API_KEY": "", "MOCK_MODE": "true"})
import main

def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond: sys.exit(1)

async def run():
    # 1) mock mode end-to-end
    r = await main.linkedin(main.LinkedInRequest())
    check(r["mode"] == "mock", "mock mode selected")
    check(len(r["ideas"]) == 3, "3 ideas returned")
    check(r["draft"] and r["hashtags"][0].startswith("#"), "draft and hashtags present")
    check(0 <= r["confidence"] <= 1, f"confidence in range ({r['confidence']})")
    print(json.dumps({k: r[k] for k in ("confidence", "confidence_breakdown", "hashtags", "char_count")}, indent=1))

    # 2) LLM path with scripted agents (incl. a malformed first Writer reply -> retry)
    main.MOCK_MODE = False; main.AZURE_API_KEY = "x"
    long_draft = "A strong hook about UPI.\n\n" + ("Cash flow gets clearer when payments settle faster. " * 6) + "\n\nFollow FinEdge."
    SCRIPT.update({
        "Ideator": ['```json\n{"ideas":[{"title":"UPI for SMEs","angle":"a","pillar":"p"},{"title":"B","angle":"b","pillar":"p"}]}\n```'],
        "Writer": ["Sure! here is the post (not json)", json.dumps({"selected_idea": "UPI for SMEs", "draft": long_draft})],
        "Reviewer": ['{"score": 0.9, "reasons": ["clear"], "suggested_fix": ""}'],
        "Hashtagger": ['{"hashtags": ["Fintech", "#UPI", "#upi", "small business", "#Fintech"]}'],
    })
    r = await main.linkedin(main.LinkedInRequest(num_ideas=2))
    check(r["mode"] == "llm", "llm mode selected")
    check(CALLS == ["Ideator", "Writer", "Writer", "Reviewer", "Hashtagger"], f"agent order with retry {CALLS}")
    check(r["hashtags"] == ["#Fintech", "#UPI", "#smallbusiness"], f"hashtags normalized/deduped {r['hashtags']}")
    check(r["confidence"] == 0.9, f"clean draft keeps reviewer score ({r['confidence']})")

    # 3) guardrails
    g = main.apply_guardrails("Enjoy guaranteed returns, totally risk-free!", [], 0.9, main.Brand())
    check(g["confidence"] < 0.5 and "risky_financial_claim" in g["flags"], f"guardrails penalize risky copy {g}")

    # 4) error path -> HTTP 502
    SCRIPT.update({"Ideator": ['{"ideas": []}']})
    try:
        await main.linkedin(main.LinkedInRequest()); check(False, "should raise")
    except fa.HTTPException as e:
        check(e.status_code == 502, f"pipeline error -> 502 ({e.detail})")

    # 5) timeout path -> 504
    async def slow(*a, **k): await asyncio.sleep(1)
    orig = main.run_llm_pipeline; main.run_llm_pipeline = slow; main.LLM_TIMEOUT_SEC = 0.05
    try:
        await main.linkedin(main.LinkedInRequest()); check(False, "should raise")
    except fa.HTTPException as e:
        check(e.status_code == 504, "timeout -> 504")
    main.run_llm_pipeline = orig

    # 6) /log write + read
    await main.write_log(main.LogRecord(run_id="abc", route="slack_review", confidence=0.86, threshold=0.75, dry_run=True, hashtags=["#A", "#B"], draft="line1\nline2"))
    await main.write_log(main.LogRecord(run_id="", route="error", status="error", error="boom"))
    rows = (await main.read_log())["rows"]
    check(len(rows) == 2 and rows[0]["hashtags"] == "#A #B" and rows[0]["draft_preview"] == "line1 line2", "CSV log rows written")
    print(open(main.RUN_LOG_CSV).read())
    check(sum(1 for _ in open(main.RUNS_JSONL)) >= 6, "JSONL audit log written")

asyncio.run(run())
print("ALL CHECKS PASSED")
