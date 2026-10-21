# Reflection: Design Decisions, Challenges and Trade-offs

## Problem framing
FinEdge's content team reports spending 15+ hours a week drafting posts, with inconsistent posting and no unified executive voice (figures from the project brief). I treated this as a product problem with two goals that pull against each other. The first is **speed and consistency**: automate drafting on a fixed cadence. The second is **brand and regulatory safety**: a fintech cannot publish unreviewed financial claims. The design keeps a human in the loop by default and lets automation earn autonomy through measured confidence.

## Key design decisions

**1. Separate the "thinking" from the "plumbing."** The AutoGen microservice owns content generation; n8n owns scheduling, routing, integrations and logging. Each part could be tested on its own: curl for the service, the Execute workflow button for n8n. Either can be replaced without rebuilding the other, for example swapping the model or moving from Slack to Teams. This separation also made debugging much easier on the VM: when n8n failed, I could confirm with curl that the service itself was healthy, which pointed the problem to the connection between the two.

**2. Specialised agents instead of one large prompt.** Following the CodeGen → Tester → Debugger pattern from the course demo, I split the work into Ideator, Writer, Reviewer and Hashtagger. Each agent has a narrow job and a strict JSON reply format, which makes outputs easier to parse, debug and improve individually. Having the Reviewer act as a separate "critic" is what makes a confidence score possible.

**3. Confidence = LLM judgment + deterministic guardrails.** An LLM grading its own team's work can be too generous, so the Reviewer's score is reduced by rule-based penalties for banned terms, risky financial phrases ("guaranteed returns", "risk-free"), length and missing hashtags. Rules are transparent and auditable, which matters in a regulated industry. The response returns the breakdown so reviewers can see why a score is low.

**4. Safe by default.** `dry_run = true` and a 0.75 threshold mean nothing reaches LinkedIn until a person has reviewed drafts in Slack and deliberately switches to live mode. Both conditions must pass for auto-publishing.

**5. Logging for tuning, not just debugging.** Every run writes a CSV row with route, status, confidence, threshold, topic, hashtags and a draft preview. Over time this shows how often human reviewers agree with high-confidence drafts. That is the evidence needed to raise or lower the threshold, or to decide when dry-run can be switched off.

## Results
I ran the system on the course Ubuntu VM with Azure OpenAI `gpt-5-mini`, Node.js 20 and n8n 2.8.4.

- **Microservice:** all curl tests passed in mock mode, then with the real model. Each call returned ideas, a draft, a confidence score and hashtags.
- **Full workflow, three dry-run executions:**
  - Run 1: confidence **0.92**. Correctly routed to Slack review; delivery failed only because the Slack webhook was still a placeholder (Slack returned `404 "no_team"`). The log captured this as `slack_failed`.
  - Run 2: confidence **0.84**. Delivered to the `#linkedin-review` Slack channel and logged as `sent_to_slack`.
  - Run 3 (after the error test): confidence **0.90**. Delivered to Slack and logged as `sent_to_slack`.
- **Error handling:** with the microservice stopped, the workflow switched to the error route and posted a warning to Slack instead of failing silently.
- **Content:** all three drafts addressed the week's topic ("What SMEs should know about digital lending") with a hook first line, for example *"A loan can help your business grow — or quietly squeeze it."*, *"If debt keeps you awake at night, you're borrowing the wrong way."* and *"Don't let a fast loan become a costly mistake."* Each came with five relevant hashtags (for example #Fintech, #DigitalLending and #ResponsibleLending, plus tags such as #SmallBusiness, #SMEFinance, #WorkingCapital or #BusinessFinance). All three scores were above the 0.75 threshold, so in live mode all three would have qualified for auto-publishing. With dry-run on, all three correctly went to human review.
- **My assessment of the drafts:** In my view, the drafts match FinEdge's voice and could be posted with few edits.

Three runs are not enough to calibrate the threshold. Even so, the scores show a spread (0.84–0.92) that the gate can act on.

## Challenges
- **Environment setup.** Most of the time went into the environment rather than the code: working in the right folder, remembering that every new terminal tab needs the virtual environment re-activated, and keeping two services (uvicorn and n8n) running in separate tabs at the same time. n8n was not installed on the VM. I checked the Node.js version (v20.20.2) against n8n's requirement and ran it with `npx` instead of a global install.
- **The connection error.** The workflow first took the error route with `ECONNREFUSED`, even though curl to the service worked. Because the microservice and n8n could be tested separately, I could narrow the problem to the address n8n used. Changing `localhost` to `127.0.0.1` fixed it. The most likely cause is that `localhost` resolved to the IPv6 address while the service listened only on IPv4.
- **"Executed successfully" is not the same as "worked."** Because the Slack and logging steps are set to continue on error, n8n reported success even when Slack had not received anything. I learned to check the route taken and the log's `status` field rather than trusting the success banner.
- **Editing in the n8n interface.** I could not edit the Brand Config node at first (I was likely viewing a past execution, which is read-only). I changed the value in the workflow file with a command and re-imported it.
- **Agent memory and model output** (found during development). AutoGen agents keep conversation history, so they are created fresh per request to stop earlier posts bleeding into later ones. Model replies are not always clean JSON, so the service parses tolerantly and retries once.

## Trade-offs
| Choice | Benefit | Cost |
|---|---|---|
| Sequential agents | Simple, traceable, cheap to debug | Slower than parallel calls; one failure stops the chain |
| Human review by default | Brand and compliance safety | Less automation until trust is built |
| CSV log via the service | No extra credentials; opens in Excel | Not multi-user, and errors cannot be logged when the service itself is down (seen in the error test) |
| Slack webhook vs OAuth | Fast setup, no app scopes | One-way only; no approve/reject buttons in Slack |
| Continue-on-error for Slack and logging | A Slack outage never blocks the run or the log | n8n shows "success" even when a step failed, so monitoring must check the log status |
| No authentication on the service | Simple for a single VM | Must stay on a private network; add an API key before any shared deployment |

## What I would do next
- **Make reviews two-way:** add approve/reject buttons in Slack (a Slack app or n8n's Wait node), so reviewer decisions are logged automatically and can be compared with confidence scores.
- **Log independently of the service:** write errors to a database or Google Sheet, so failures are recorded even when the microservice is down.
- **Improve the error alert:** put the failure reason first, so a reviewer sees immediately why a run failed.
- **Test the remaining paths:** run the low-confidence and live LinkedIn scenarios once a LinkedIn credential is available.
- **Close the loop:** feed post engagement back into topic selection, and add a source-check step before any statistic is allowed into a draft.
