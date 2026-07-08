# Waypoint — Autonomous Browser Agent

An agent that drives a real Chromium browser to complete web tasks on its own:
navigate, read the page, click, type, scroll, and extract information — looping
until the task is done. A light, colorful live dashboard shows the browser view
and a step-by-step "waypoint log" of every decision the agent makes, in real time.

## How it works

```
┌─────────────┐    numbered elements + screenshot     ┌──────────────┐
│   Browser    │ ─────────────────────────────────────▶│  OpenRouter   │
│ (Playwright) │                                        │  LLM ("brain")│
│              │◀───────────────────────────────────────│               │
└─────────────┘     one JSON action (click/type/...)    └──────────────┘
       ▲                                                        
       │  every step streamed live over WebSocket               
       ▼                                                        
┌─────────────────────────────────────────────────────────┐
│  Dashboard UI  — live screenshot + colour-coded action log │
└─────────────────────────────────────────────────────────┘
```

1. **Perceive** — a small injected script (`dom_extractor.js`) walks the live
   DOM and labels every visible clickable/typeable element with a stable
   `data-agent-id`, returning a clean numbered list (tag, role, text,
   placeholder...) instead of raw HTML. This is far more reliable for an LLM
   than parsing markup.
2. **Decide** — that list, plus the task and a short history, is sent to any
   model on [OpenRouter](https://openrouter.ai) (you bring your own API key,
   any tool-capable model works). The model replies with exactly one JSON
   action: `click`, `type`, `navigate`, `scroll`, `select`, `press_key`,
   `wait`, or `finish`.
3. **Act** — `browser_controller.py` executes that action with Playwright.
4. **Repeat** until the model calls `finish`, the same action repeats 3x in a
   row (stuck-loop guard), or a step limit is hit.
5. Every step (screenshot, thought, action, errors) is pushed to the browser
   dashboard over a WebSocket the instant it happens.

## Project layout

```
backend/
  server.py             FastAPI app — /api/run, /api/ws/{id}, /api/stop/{id}
  agent.py               Agent loop + OpenRouter client + system prompt
  browser_controller.py  Playwright wrapper (perceive + act)
  dom_extractor.js        Injected page script that labels interactive elements
  requirements.txt
frontend/
  index.html             Dashboard UI (control panel, live viewport, waypoint log)
```

## Setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

Get a free/pay-as-you-go API key at **https://openrouter.ai/keys**.

## Run

```bash
cd backend
uvicorn server:app --reload --port 8000
```

Open **http://localhost:8000** — paste your OpenRouter key, pick a model
(default `openai/gpt-4o-mini` — any tool-capable model from
[openrouter.ai/models](https://openrouter.ai/models) works), enter a task, and
hit **Run agent**.

> The agent runs Chromium **headless** by default (fastest, most reliable for
> a server). If you'd rather record a visible browser window popping up and
> acting on screen, set `"headless": false` — either edit the default in
> `frontend/index.html`'s `runAgent()` fetch body, or change the default in
> `RunRequest` in `server.py`.

## Demo tasks (built-in presets in the UI)

These use public, automation-friendly practice sites — safe to demo, no real
data or accounts involved:

| Task | Target | What it proves |
|---|---|---|
| 🔎 Web search & extraction | DuckDuckGo | Navigate, type a query, read results, report an answer |
| 📝 Form filling | [demoqa.com/automation-practice-form](https://demoqa.com/automation-practice-form) | Fill multiple fields, dropdowns, submit |
| 🏨 Dummy appointment/booking | [automationintesting.online](https://automationintesting.online/) | Multi-step flow: pick a room, fill guest details, confirm a reservation |

Click a preset button in the left panel to auto-fill the start URL + task,
then just add your API key and run. Feel free to swap in your own URL/task —
the agent isn't hardcoded to these sites.

## Recording the demo

1. Start the server, open the dashboard, arrange your screen recorder.
2. Click a preset (or write your own task), hit **Run agent**.
3. Record the live screenshot updating in the center panel and the
   color-coded waypoint log filling in on the right as the agent thinks and
   acts.
4. Let it reach the green "Task completed" banner at the bottom, then stop
   the recording.
5. Repeat for the other 1–2 tasks in the same or separate clips.

## Design notes for the write-up

- **Reliability**: grounding every action in a freshly-computed, numbered
  element list (rather than letting the model guess selectors) is what keeps
  the agent from hallucinating clicks on elements that don't exist.
- **Efficiency**: screenshots are JPEG-compressed and the element list is
  capped to visible/interactive nodes only, keeping each LLM call's context
  small and each step fast.
- **Safety**: the system prompt explicitly forbids entering real personal
  data into any form and instructs the model to use obvious test data instead.
- **Extensibility**: adding a new action (e.g. `hover`, `upload_file`) only
  needs one method on `BrowserController` + one branch in `Agent.run`'s
  action dispatch — the perception layer and UI need no changes.