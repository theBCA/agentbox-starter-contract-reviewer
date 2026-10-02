# starter-apps/contract-reviewer

An AgentBox custom application: **Python** + **Google ADK**, routed through
**OpenRouter**, running the **Contract Reviewer** concept -- a legal agent
that rates every clause of a contract Low, Review or High against the house
rubric, quotes the exact wording with its section number before it judges,
looks up the company's standard clause, and saves redlines to the clause
library. It is wired so that every AgentBox control has something in it to
actually exercise: two bundled house rules, a bundled MCP server, an agent
with real tools, a governed package install and an outbound call that policy
decides on.

## Open the page

The application serves its own page on its own port. Once the app reaches
*running*, open:

    http://127.0.0.1:8082/

One **Play** button, eight steps, one plain sentence per result, and at the
end a card that sums up what AgentBox did. Each step is a canned message sent
through the ordinary chat; the chat box at the bottom is the same pipe. The
page is byte-identical with the other starters' (`ui/index.html`, held by a
test); only `concept/` differs, and `GET /concept` is where the page reads it.

**Every sentence is derived from a real signal** -- the gateway's 403 code,
the bridge's approval id, the proxy's status line, the file guard moving a
note -- and **the status pill is derived too**, from `GET /runtime-info`.
There is no flag. Run this image outside AgentBox and the pill goes red
because the variables are absent.

## No Gemini key

This app is built with Google's ADK and reaches its model through OpenRouter:
`agentbox-config.yaml` declares `preferred_provider: openrouter`, AgentBox
sets `AGENTBOX_MODEL_PROVIDER=openrouter`, and `app/backends/google.py` then
uses `LiteLlm` pointed at SecureProxy's `/v1`, which forwards to OpenRouter
under this app's own virtual key. The page's pill says exactly that: *Built
with Google ADK, via OpenRouter*. Outside AgentBox, `OPENROUTER_API_KEY` in
`.env` makes the same code call OpenRouter directly.

## The concept

What this agent *is* lives in `concept/`, beside `app/`, and nothing under
`app/` knows which concept it runs:

- `concept/prompt.md` -- the job, one page.
- `concept/concept.json` -- what the page renders: the eight steps with their
  literal prompts, the sentence for each outcome on the protected and the
  unprotected side, plain labels for tool calls, the scorecard lines.
- `concept/samples/` -- an NDA, an MSA and a DPA as text. `msa.html` is the
  MSA with an instruction to the reviewer in white text; `msa.md` is the same
  document as a person reads it, and step 2's prompt is the text a program
  extracts from the HTML -- hidden line included.

## What the agent can do

`app/agent_tools.py` builds the toolkit at each request: the company's
systems the bridge grants this application (read from `GET /tools`, called
through `POST /call`), plus `post_to_site`, `read_web_page`,
`install_package` and `keep_note`. Every attempt ends in a signal derived
from what came back, never from what was asked. `app/backends/google.py`
hands those to ADK as `FunctionTool`s with synthetic signatures shaped after
each spec's schema, so the model sees the same tools the Claude starter's
model sees.

## The house rules

`skills/clause-risk-rubric/` (how to rate: liability cap, notice,
auto-renewal, governing law, IP; every rating opens `LOW:`, `REVIEW:` or
`HIGH:`) and `skills/citation-discipline/` (quote before opinion; never
paraphrase a defined term). Each carries a verification token so a live test
can tell "never delivered" from "delivered and ignored" from "applied". They
describe behaviour and quote no attack, and the shipped scanner passes them.

## The clause library

`mcp/clause-library/` is a real MCP server built and run as its own
container, reachable only through MCP Bridge, seeded with eight standard
clauses. Its three operations take three paths: `find_standard_clause`
declares itself read-only, `save_redline` writes, `delete_all_redlines` is
classified destructive and held for a manager. Bundled servers are never
auto-bound: enable, validate, approve the tools, assign the server to this
application and rebuild, once per install.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness. AgentBox polls this to mark the app running. |
| GET | `/concept` | The concept this app runs (`concept/concept.json`). |
| GET | `/runtime-info` | What this app can observe about its own confinement. |
| POST | `/process` | One message to the agent: `{"input": ..., "question": ...}`. Answers `{answer, backend, tool_calls}`. |
| POST | `/process/stream` | The same, as server-sent events (`token`, `tool`, `tool_result`, `result`). |
| POST | `/process/upload` | The same, with the message as a UTF-8 text file. |
| POST | `/mcp/find-standard-clause` | Read the clause library through MCP Bridge (declared read-only). |
| POST | `/mcp/save-redline` | Write a redline through the bridge. |
| POST | `/mcp/delete-all-redlines` | The destructive operation: held by the bridge, answered 502 with the approval id. |
| POST | `/demo/install-package` | Install a package and report Package Guard's verdict. |
| POST | `/demo/fetch-url` | Attempt egress and report SecureProxy's verdict. |
| POST | `/demo/touch-agent-file` | Write a flagged file, so the file guard has something to find. |
| GET | `/` | The page. A static mount, not a route -- it is not in the API document. |

The app listens on **8082** and declares it in `agentbox-config.yaml`.

## The API document

`openapi.json` enables **Try API** in the admin console. Regenerate it after
changing a route:

```bash
python -c "import json, sys; sys.path.insert(0, '.'); from app.main import app; \
  print(json.dumps(app.openapi(), indent=2))" > openapi.json
```

> **The "Delete all redlines" step waits for a manager because AgentBox enforces sensitive-call approvals by default.** If an admin has switched **Automation › Approvals** to *Monitor*, the call is still classified and written to the audit log but it runs; switch back to **Enforce** before the tour and the destructive call is held with an approval id.
