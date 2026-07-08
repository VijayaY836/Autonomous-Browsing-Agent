"""
agent.py

The agent's "brain". On every step it:
  1. Observes the page (numbered interactive elements + screenshot)
  2. Sends the task + history + observation to an LLM on OpenRouter
  3. Parses a single JSON action out of the reply
  4. Executes that action through BrowserController
  5. Repeats until the model calls `finish`, an error repeats too often,
     or max_steps is hit.

Every step is reported through an async `on_step` callback so the server
can stream it to the frontend over a WebSocket in real time.
"""

import json
import re
import time
from typing import Any, Callable, Optional

import httpx

from browser_controller import BrowserController

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Common phrases that indicate an action already succeeded (form submitted, booking
# confirmed, etc). Detecting these in code — rather than hoping the model notices them
# buried in a long page — makes success-recognition reliable instead of a coin flip.
CONFIRMATION_KEYWORDS = [
    "thank you for", "thanks for submitting", "thanks for your", "successfully submitted",
    "submission successful", "booking confirmed", "reservation confirmed", "order confirmed",
    "your booking has been confirmed", "confirmation number", "confirmation code",
    "has been confirmed", "registration successful", "successfully registered",
]

SYSTEM_PROMPT = """You are an autonomous web browsing agent. You control a real Chromium \
browser one action at a time to accomplish the user's TASK.

On every turn you receive:
- The TASK you must complete
- The current page URL and title
- The page's visible text content — this is what's actually on screen, use it to read/verify/extract \
any information the task asks for (e.g. an answer to a question, a price, a confirmation message)
- A numbered list of visible interactive elements on the page (id, tag, role, text, \
placeholder, value, href). Only elements in this list can be interacted with.
- A short history of your previous actions and their results

You must reply with ONLY a single JSON object (no markdown fences, no prose before or \
after it) with this exact shape:

{
  "thought": "one short sentence: what you see and why you're choosing this action",
  "action": "click | type | navigate | scroll | select | press_key | wait | finish",
  "target_id": <integer element id, required for click/type/select, else null>,
  "text": "<string to type, required for type, else null>",
  "submit": <true|false, only for type — whether to press Enter after typing>,
  "url": "<string, required for navigate, else null>",
  "direction": "up|down, only for scroll, else null>",
  "key": "<key name, only for press_key, e.g. Enter, Escape, else null>",
  "result": "<final answer / summary text, required only when action=finish, else null>"
}

Rules:
- Ground every action in the numbered element list you were just given — never \
invent an id that isn't listed.
- Prefer the most direct path to the goal. Don't click around aimlessly.
- If a text field must be filled, use "type" with the exact target_id of that field.
- Use "navigate" only to go to a brand-new URL (e.g. to start a search or open a site \
mentioned in the task). Otherwise interact with elements already on the page.
- If the page looks like it hasn't finished loading or an action had no visible \
effect, you may "wait" once before retrying.
- Call "finish" as soon as the task is genuinely complete, or if it becomes clear \
after reasonable effort that it cannot be completed — either way, put a clear, \
concrete summary of what was found or done in "result".
- If the task asks a question or asks you to find/report something, check the visible page text \
FIRST before clicking or scrolling further — if the answer is already there, call "finish" with it \
immediately rather than waiting or exploring more.
- After clicking any submit/confirm/book/reserve button, ALWAYS check the current visible page text \
for a success or confirmation signal (e.g. "Thank you", "Submitted", "Confirmed", "Booking confirmed", \
a summary/receipt of what was entered, a modal/dialog with your data in it) BEFORE trying to click \
anything again. If a confirmation is present, the action already succeeded — call "finish" immediately \
with those confirmation details, even if a follow-up click seems to fail or time out.
- Never fill in real personal data. If a form asks for personal details, use clearly \
fake placeholder data (e.g. name "Test User", email "test.user@example.com").
- If an element is marked [DISABLED], do NOT click or type into it — clicking a disabled \
element does nothing and repeating it wastes steps. Figure out what's blocking it instead: \
usually a required field elsewhere on the page is empty or invalid. Look at other visible \
fields, fill in whatever seems required, then check the element list again.
- If the page shows a genuine error banner (e.g. "unexpected error", "please try again", \
a CAPTCHA, or a block page), don't keep retrying the same action — try navigating to the \
same URL again once, or pick a different concrete path to the goal, or finish and clearly \
report what happened.
"""


def _extract_json(raw: str) -> dict:
    """LLMs sometimes wrap JSON in markdown fences or add stray text - salvage it."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"Could not parse JSON from model output: {raw[:300]}")


class OpenRouterClient:
    def __init__(self, api_key: str, model: str):
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()

    async def decide(self, messages: list[dict]) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost",
            "X-Title": "Browser Agent",
        }
        for name, value in headers.items():
            try:
                value.encode("ascii")
            except UnicodeEncodeError:
                raise RuntimeError(
                    f"Your API key or model name contains a non-standard character (likely a smart "
                    f"dash '—' or '–' instead of a plain '-', or a curly quote). This usually happens "
                    f"when copy-pasting through an app like Notion or Word. Paste it into Notepad first "
                    f"to check for stray characters, then copy it fresh from there. (Problem was in the "
                    f"'{name}' field.)"
                )
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 500,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(OPENROUTER_URL, headers=headers, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"OpenRouter error {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _extract_json(content)


def _format_elements(elements: list[dict]) -> str:
    lines = []
    for el in elements:
        bits = [f'#{el["id"]} <{el["tag"]}']
        if el.get("type"):
            bits.append(f'type={el["type"]}')
        if el.get("role"):
            bits.append(f'role={el["role"]}')
        bits.append(">")
        line = " ".join(bits)
        if el.get("text"):
            line += f' text="{el["text"]}"'
        if el.get("placeholder"):
            line += f' placeholder="{el["placeholder"]}"'
        if el.get("value"):
            line += f' value="{el["value"]}"'
        if el.get("href"):
            line += f' href="{el["href"][:80]}"'
        if el.get("checked"):
            line += " checked"
        if el.get("disabled"):
            line += " [DISABLED - cannot be clicked/typed into right now]"
        if not el.get("inViewport"):
            line += " [off-screen - scroll to reach]"
        lines.append(line)
    return "\n".join(lines) if lines else "(no interactive elements found)"


class Agent:
    def __init__(
        self,
        controller: BrowserController,
        llm: OpenRouterClient,
        on_step: Callable[[dict], Any],
        max_steps: int = 15,
    ):
        self.controller = controller
        self.llm = llm
        self.on_step = on_step
        self.max_steps = max_steps
        self.history: list[str] = []

    async def _emit(self, **kwargs):
        payload = {"ts": time.time(), **kwargs}
        result = self.on_step(payload)
        if hasattr(result, "__await__"):
            await result

    async def run(self, task: str, start_url: Optional[str] = None) -> dict:
        if start_url:
            await self._emit(type="status", message=f"Opening {start_url}")
            await self.controller.navigate(start_url)

        state_fingerprints: list[tuple] = []

        for step in range(1, self.max_steps + 1):
            obs = await self.controller.observe()
            screenshot = await self.controller.screenshot_b64()

            # Fingerprint the actual page state (not just the action name). Only
            # flag "stuck" if the page genuinely hasn't changed across several
            # steps — repeating an action name (e.g. scrolling 3x through a long
            # page) is normal and must NOT be flagged just because the label repeats.
            page_text = await self.controller.get_visible_text(max_chars=4000)
            elements_sig = json.dumps(
                [
                    {
                        "id": el["id"],
                        "text": el.get("text"),
                        "value": el.get("value"),
                        "checked": el.get("checked"),
                        "disabled": el.get("disabled"),
                    }
                    for el in obs["elements"]
                ],
                sort_keys=True,
            )
            fingerprint = (obs["url"], obs["scrollY"], hash(page_text), hash(elements_sig))
            state_fingerprints.append(fingerprint)
            if len(state_fingerprints) >= 5 and len(set(state_fingerprints[-5:])) == 1:
                await self._emit(
                    type="error", step=step,
                    message="Page state hasn't changed for several steps — stopping to avoid an infinite loop."
                )
                return {"success": False, "result": "Agent stuck: the page stopped responding to actions.", "steps": step}

            await self._emit(
                type="observation",
                step=step,
                url=obs["url"],
                title=obs["title"],
                screenshot=screenshot,
                element_count=len(obs["elements"]),
            )

            matched_keyword = next(
                (kw for kw in CONFIRMATION_KEYWORDS if kw in page_text.lower()), None
            )
            confirmation_flag = ""
            if matched_keyword:
                confirmation_flag = (
                    f"🔔 AUTOMATED CHECK: this page's text appears to contain a success/confirmation "
                    f"signal (matched phrase: \"{matched_keyword}\"). Read the visible page text below "
                    f"carefully — if this confirms the task is done, call \"finish\" RIGHT NOW with the "
                    f"relevant details, instead of clicking or waiting again.\n\n"
                )

            user_msg = (
                f"TASK: {task}\n\n"
                f"{confirmation_flag}"
                f"Current URL: {obs['url']}\n"
                f"Page title: {obs['title']}\n"
                f"Scroll position: {obs['scrollY']} / max {obs['scrollMaxY']}\n\n"
                f"Visible page text (read this to answer/verify anything the task asks about — "
                f"this is the actual content on screen, not just clickable elements):\n"
                f"{page_text or '(no visible text)'}\n\n"
                f"Visible interactive elements:\n{_format_elements(obs['elements'])}\n\n"
                f"History so far:\n" + ("\n".join(self.history[-8:]) or "(none yet)")
            )

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ]

            try:
                decision = await self.llm.decide(messages)
            except Exception as e:
                await self._emit(type="error", step=step, message=f"LLM error: {e}")
                return {"success": False, "result": f"Agent stopped: LLM error — {e}", "steps": step}

            action = decision.get("action")
            thought = decision.get("thought", "")

            await self._emit(type="decision", step=step, thought=thought, action=action, raw=decision)

            try:
                if action == "finish":
                    result_text = decision.get("result", "Task finished.")
                    await self._emit(type="finish", step=step, result=result_text)
                    return {"success": True, "result": result_text, "steps": step}

                elif action == "click":
                    await self.controller.click(int(decision["target_id"]))
                    self.history.append(f"Step {step}: clicked #{decision['target_id']} ({thought})")

                elif action == "type":
                    await self.controller.type_text(
                        int(decision["target_id"]), decision.get("text", ""), bool(decision.get("submit", False))
                    )
                    self.history.append(
                        f"Step {step}: typed \"{decision.get('text','')}\" into #{decision['target_id']} ({thought})"
                    )

                elif action == "select":
                    await self.controller.select_option(int(decision["target_id"]), decision.get("text", ""))
                    self.history.append(f"Step {step}: selected \"{decision.get('text','')}\" in #{decision['target_id']}")

                elif action == "navigate":
                    await self.controller.navigate(decision["url"])
                    self.history.append(f"Step {step}: navigated to {decision['url']} ({thought})")

                elif action == "scroll":
                    await self.controller.scroll(decision.get("direction", "down"))
                    self.history.append(f"Step {step}: scrolled {decision.get('direction','down')}")

                elif action == "press_key":
                    await self.controller.press_key(decision.get("key", "Enter"))
                    self.history.append(f"Step {step}: pressed key {decision.get('key','Enter')}")

                elif action == "wait":
                    await self.controller.page.wait_for_timeout(2000)
                    self.history.append(f"Step {step}: waited")

                else:
                    self.history.append(f"Step {step}: unknown action '{action}' — ignored")

            except Exception as e:
                err = str(e)[:300]
                await self._emit(type="error", step=step, message=f"Action failed: {err}")
                self.history.append(f"Step {step}: action {action} FAILED — {err}")

        await self._emit(type="finish", step=self.max_steps, result="Reached max steps without finishing.")
        return {"success": False, "result": "Reached max steps without finishing.", "steps": self.max_steps}