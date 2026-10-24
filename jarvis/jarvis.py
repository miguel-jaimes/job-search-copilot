# Jarvis — your personal interview coach (Phase 1: type to talk, Jarvis can speak back)
#
# A mock interviewer and speaking coach that knows your real experience bank.
# Say "interview me for Toyota" and it plays the hiring manager, using the job
# posting your copilot saved. Say "coach me" for feedback on your last answer,
# and "debrief" for scores it remembers next time.
#
# RUN (from the project folder, ~/Job_App_Project/job-copilot-phase1):
#   python jarvis/jarvis.py                       # type your answers
#   python jarvis/jarvis.py --speak               # Jarvis also says its replies out loud
#   python jarvis/jarvis.py --speak --company Toyota   # jump straight into a Toyota mock interview
#
# HOW IT WORKS (one loop, three files):
#   coach_prompts.py  the rules: modes, how to interview, how to coach, how to score
#   coach_tools.py    lookups Claude can ask for (job posting, saved answers, past debriefs)
#   voice.py          speaking out loud with the Mac's built-in voice
#   this file         reads what you type, sends it to Claude, runs any tools Claude
#                     asks for, prints and speaks the reply, and keeps track of cost.
#
# Uses the same .env API key and experience_bank.json as the rest of the copilot.

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime

# Make the copilot's modules (job_copilot_server, tracker, ...) importable from here.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

try:
    import anthropic
    from dotenv import load_dotenv
except ImportError:
    sys.exit("Missing dependency. Run:  pip install -r requirements.txt")

# override=True: the key in the project's .env file WINS over any ANTHROPIC_API_KEY
# already set in your terminal (for example an old key exported in ~/.zshrc).
# Without it, load_dotenv quietly keeps the terminal's value, which can be stale.
load_dotenv(os.path.join(ROOT, ".env"), override=True)

KEY_HELP = """Your API key was rejected (401: invalid x-api-key). To fix it:
  1. Go to console.anthropic.com -> API Keys and check that the key is still active.
  2. If it isn't (or you're not sure), create a new key and copy it.
  3. Open the .env file in the project folder and replace the old key:
       ANTHROPIC_API_KEY=sk-ant-...      (no quotes, no spaces)
  4. Save the file and run Jarvis again."""

import coach_tools                      # noqa: E402  (also loads the copilot server's functions)
import voice                            # noqa: E402
from coach_prompts import build_system  # noqa: E402

# The copilot server turns on detailed logging when it's imported; keep the
# HTTP request chatter out of your practice session.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)

MODEL = "claude-sonnet-5-5"   # strong at coaching and writing; try claude-haiku-5-5 to save money
MAX_TOKENS = 2000
# Sonnet 5.5 prices, $ per million tokens. Used only to show you what a session cost.
PRICES = {"input": 2.00, "cache_write": 2.50, "cache_read": 0.10, "output": 10.00}
SESSIONS_DIR = os.path.join(HERE, "sessions")   # transcripts; personal, kept out of git

# Typing shortcuts. They just become plain sentences, so Claude handles every mode
# switch the same way whether you type "/coach" or say "coach me".
SHORTCUTS = {
    "/coach": "Coach me on my last answer.",
    "/pause": "Pause the interview and coach me on my last answer.",
    "/resume": "Let's pick the interview back up where we left off.",
    "/debrief": "Let's debrief. How did I do?",
}

HELP = """Talk normally, or use a shortcut:
  /interview Toyota     start a mock interview (add a role after the company if you like)
  /interview            general analyst mock interview
  /coach or /pause      feedback on your last answer
  /resume               back to the interview
  /debrief              scores + fixes, saved for next time
  /quit                 save the transcript and exit"""


class ReplyPrinter:
    """Prints Claude's reply as it streams in, swapping the leading [MODE] tag for a
    friendly label like 'Jarvis (coach):'. The tag has to arrive before we know the
    label, so the first few characters wait in a small buffer."""

    TAG = re.compile(r"^\s*\[([A-Z]+)\]\s*")

    def __init__(self, mode):
        self.mode = mode
        self.buf = ""
        self.started = False
        self.skip_space = False   # drop the line break that follows the tag
        self.text = ""

    def feed(self, chunk):
        self.text += chunk
        if self.started:
            if self.skip_space:
                chunk = chunk.lstrip()
                if not chunk:
                    return
                self.skip_space = False
            print(chunk, end="", flush=True)
            return
        self.buf += chunk
        head = self.buf.lstrip()
        if not head or (head.startswith("[") and "]" not in head and len(head) < 15):
            return                                  # still waiting for the whole tag
        self._start()

    def _start(self):
        m = self.TAG.match(self.buf)
        if m:
            self.mode = m.group(1).lower()
            rest = self.buf[m.end():]
        else:
            rest = self.buf.lstrip()
        print(f"\nJarvis ({self.mode}): {rest}", end="", flush=True)
        self.started = True
        self.skip_space = not rest

    def finish(self):
        if not self.started and self.buf.strip():
            self._start()
        if self.started:
            print()


class StreamError(Exception):
    """An error the API reported partway through a reply (for example 'overloaded')."""


def stream_turn(client, on_text, **params):
    """Ask Claude for one reply and stream it in. Calls on_text(chunk) for each piece
    of visible text as it arrives. Returns (content_blocks, stop_reason, usage).

    WHY THIS IS HAND-BUILT: the copilot pins an older anthropic SDK (0.40.0) to dodge
    an httpx bug, and that version predates "thinking" blocks. Newer models like
    Sonnet 5.5 think before replying and send a thinking block with a "signature"
    that must be sent back UNCHANGED on the next request. The old SDK mangles those
    blocks (that was the 400 error). So we read the raw event stream ourselves and
    keep every block as the plain JSON the API sent. Any block type we don't know
    about is still passed back exactly as received, so newer API features can't
    break this the same way."""
    blocks, stop_reason, usage = [], None, {}
    buffers = {}   # block index -> partial JSON text for a tool call's inputs
    with client.messages.with_streaming_response.create(stream=True, **params) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue                       # skip "event:" lines and blank keep-alives
            event = json.loads(line[5:])
            kind = event.get("type")
            if kind == "message_start":
                usage.update(event["message"].get("usage") or {})
            elif kind == "content_block_start":
                blocks.append(dict(event["content_block"]))
            elif kind == "content_block_delta":
                block, delta = blocks[event["index"]], event["delta"]
                dtype = delta.get("type")
                if dtype == "text_delta":
                    block["text"] = block.get("text", "") + delta["text"]
                    on_text(delta["text"])
                elif dtype == "input_json_delta":
                    buffers[event["index"]] = buffers.get(event["index"], "") + delta["partial_json"]
                elif dtype == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + delta["thinking"]
                elif dtype == "signature_delta":
                    block["signature"] = block.get("signature", "") + delta["signature"]
            elif kind == "content_block_stop":
                block = blocks[event["index"]]
                if block.get("type") == "tool_use":
                    raw = buffers.pop(event["index"], "")
                    block["input"] = json.loads(raw) if raw else {}
            elif kind == "message_delta":
                stop_reason = event["delta"].get("stop_reason")
                usage.update({k: v for k, v in (event.get("usage") or {}).items() if v is not None})
            elif kind == "error":
                raise StreamError(event.get("error", {}).get("message", "the API reported an error"))
    return blocks, stop_reason, usage


class Session:
    def __init__(self, client, system, model):
        self.client, self.system, self.model = client, system, model
        self.messages = []       # the conversation as the API sees it
        self.transcript = []     # the conversation as you'd read it later
        self.mode = "chat"
        self.usage = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}

    def add_usage(self, u):
        self.usage["input"] += u.get("input_tokens") or 0
        self.usage["output"] += u.get("output_tokens") or 0
        self.usage["cache_write"] += u.get("cache_creation_input_tokens") or 0
        self.usage["cache_read"] += u.get("cache_read_input_tokens") or 0

    def cost(self):
        return sum(self.usage[k] * PRICES[k] for k in PRICES) / 1_000_000

    def send(self, user_text):
        """One turn: your message in, Jarvis's full reply out (running tools in between)."""
        start = len(self.messages)              # so a failed turn can be undone cleanly
        self.messages.append({"role": "user", "content": user_text})
        self.transcript.append(("You", user_text))
        spoken = []
        try:
            while True:
                printer = ReplyPrinter(self.mode)
                content, stop_reason, usage = stream_turn(
                    self.client, printer.feed,
                    model=self.model, max_tokens=MAX_TOKENS, system=self.system,
                    tools=coach_tools.TOOLS, messages=self.messages,
                )
                printer.finish()
                self.mode = printer.mode
                self.add_usage(usage)
                if printer.text.strip():
                    spoken.append(printer.text)
                # Send Claude's turn back EXACTLY as it came, thinking blocks included.
                self.messages.append({"role": "assistant", "content": content})

                if stop_reason == "max_tokens":
                    print("   (that reply hit the length limit and was cut off)")
                elif stop_reason == "refusal":
                    print("   (Claude declined that request; try rephrasing)")
                if stop_reason != "tool_use":
                    break
                # Claude asked for one or more tools: run them and send the results back.
                results = []
                for block in content:
                    if block.get("type") != "tool_use":
                        continue
                    shown = ", ".join(f"{k}={v}" for k, v in block["input"].items()
                                      if isinstance(v, str) and len(v) < 40)
                    print(f"   (checking {block['name']}{': ' + shown if shown else ''})")
                    text, is_error = coach_tools.run_tool(block["name"], block["input"])
                    results.append({"type": "tool_result", "tool_use_id": block["id"],
                                    "content": text, "is_error": is_error})
                self.messages.append({"role": "user", "content": results})
        except KeyboardInterrupt:
            print("\n   (stopped — that turn was discarded)")
            del self.messages[start:]
            self.transcript.pop()
            return ""
        except anthropic.AuthenticationError:
            # A bad key will fail on every turn, so stop and explain instead of retrying.
            voice.stop()
            sys.exit("\n" + KEY_HELP)
        except (anthropic.APIError, StreamError) as e:
            print(f"\n   (API problem: {str(e).rstrip('.')}. That turn was discarded; try again.)")
            del self.messages[start:]
            self.transcript.pop()
            return ""

        reply = re.sub(r"\[(CHAT|INTERVIEW|COACH|DEBRIEF)\]\s*", "", "\n".join(spoken)).strip()
        self.transcript.append((f"Jarvis ({self.mode})", reply))
        return reply

    def save_transcript(self):
        if len(self.transcript) < 2:
            return None
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        path = os.path.join(SESSIONS_DIR, datetime.now().strftime("%Y-%m-%d_%H%M") + ".txt")
        with open(path, "w", encoding="utf-8") as f:
            for who, text in self.transcript:
                f.write(f"{who}:\n{text.strip()}\n\n")
            f.write(f"--- model {self.model}, about ${self.cost():.2f}, tokens {self.usage}\n")
        return path


def to_message(line):
    """Turn a /shortcut into the sentence it stands for. Anything else passes through."""
    cmd, _, rest = line.partition(" ")
    cmd = cmd.lower()
    if cmd == "/interview":
        return f"Interview me for {rest.strip()}." if rest.strip() else "Give me a general analyst mock interview."
    return SHORTCUTS.get(cmd, line)


def main():
    ap = argparse.ArgumentParser(description="Jarvis: practice interviews with feedback from your experience bank.")
    ap.add_argument("--speak", action="store_true", help="say replies out loud (macOS)")
    ap.add_argument("--voice", help="macOS voice name, e.g. 'Samantha' (list them with: say -v '?')")
    ap.add_argument("--rate", type=int, help="speaking speed in words per minute (default ~180)")
    ap.add_argument("--company", help="start straight into a mock interview for this company")
    ap.add_argument("--model", default=MODEL, help=f"Claude model (default {MODEL})")
    args = ap.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("No ANTHROPIC_API_KEY found. Check the .env file in the project folder.")
    bank_path = os.path.join(ROOT, coach_tools.copilot.BANK_FILE)
    try:
        with open(bank_path, encoding="utf-8") as f:
            bank = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Can't find {bank_path}. Jarvis needs your experience bank.")

    speak = args.speak and voice.available()
    if args.speak and not speak:
        print("(--speak needs the macOS `say` command; continuing with text only.)")

    session = Session(anthropic.Anthropic(api_key=api_key),
                      build_system(json.dumps(bank, indent=1)), args.model)
    print("Jarvis is ready. Type /help for shortcuts, /quit to exit.")

    pending = to_message(f"/interview {args.company}") if args.company else None
    while True:
        if pending:
            line, pending = pending, None
            print(f"\nYou: {line}")
        else:
            try:
                line = input("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                line = "/quit"
        voice.stop()                                    # you started talking: Jarvis stops
        if not line:
            continue
        if line.lower() in ("/quit", "/exit", "quit", "exit"):
            break
        if line.lower() == "/help":
            print(HELP)
            continue
        reply = session.send(to_message(line))
        if speak and reply:
            voice.speak(reply, args.voice, args.rate)

    voice.stop()
    path = session.save_transcript()
    print(f"\nSession cost: about ${session.cost():.2f}.")
    if path:
        print(f"Transcript saved: {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
