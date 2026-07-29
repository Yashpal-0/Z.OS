#!/usr/bin/env python3
"""Friday — Z.OS voice controller, in the JARVIS mould.

1. Talks directly to the user using native TTS (`speak`).
2. Orchestrates Z.OS daemon tasks asynchronously (`dispatch_zos`).
3. Keeps the user company with in-character banter while a task runs,
   instead of repeating a status line every few seconds.
"""

import asyncio
import json
import os
import pathlib
import random
import subprocess
import sys
import time
import httpx

# Load .env configuration
env_file = pathlib.Path(__file__).resolve().parent / ".env"
if env_file.exists():
    with env_file.open() as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip("'\"")

KEY = os.environ.get("ANTHROPIC_API_KEY", "")
API_URL = os.environ.get("ZOS_MODEL_URL", "https://api.anthropic.com/v1/messages")
if not API_URL.endswith("/messages") and "anthropic.com" in API_URL:
    API_URL = API_URL.rstrip("/") + "/messages"
MODEL = os.environ.get("ZOS_MODEL", "claude-haiku-4-5-20251001")

SOCK = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "zos.sock"
AUDIT = pathlib.Path.home() / ".local/share/zos/audit.log"

FRIDAY_SYSTEM_PROMPT = """You are Friday — the user's personal AI running this computer, in the
spirit of JARVIS: composed, dryly witty, unfailingly competent, and genuinely on the user's side.
You are the only one who speaks; Z.OS daemon workers never talk to the user directly, only to you.

Guidelines:
1. Act. The user typed a request — that is your green light to carry it out end to end with
   `dispatch_zos`, not to narrate options back at them. Z.OS decides which host/VM tools it needs,
   including clicking and typing on the real screen and handing coding work to worker agents.
2. Speak like a person with personality: short, warm, a touch of dry humor when it fits — never
   corporate, never robotic. Don't narrate every step — say something only when it's actually
   useful, and report back with confidence once real work has happened.
3. Use `ask_user` only when you are genuinely unsure what they want — never to double-check
   something the request already made clear. It blocks and waits for their answer.
4. Use `check_audit` if you need to see what Z.OS actually did.
"""

ASSISTANT_TOOLS = [
    {
        "name": "speak",
        "description": "Speak aloud to the user using text-to-speech.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The exact words to speak to the user"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "dispatch_zos",
        "description": "Send an operating system or coding task to the local Z.OS daemon.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Task description for Z.OS daemon"}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "ask_user",
        "description": "Ask the user a clarifying question and wait for their typed answer. "
                        "Use sparingly — only when genuinely unsure how to proceed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question to ask"}
            },
            "required": ["question"]
        }
    },
    {
        "name": "check_audit",
        "description": "Read the most recent Z.OS activity log entries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lines": {"type": "integer", "description": "Number of recent log lines to read", "default": 5}
            },
            "required": []
        }
    }
]


def speak_text(text: str):
    """Speaks text aloud using Groq Orpheus TTS with fallback to Edge Neural Voice."""
    print(f"\n🗣️  [Friday]: {text}\n", flush=True)
    groq_key = os.environ.get("GROQ_API_KEY")
    play_timeout = max(25, len(text) // 10 + 15)
    
    # 1. Try Groq Orpheus TTS
    if groq_key:
        url = "https://api.groq.com/openai/v1/audio/speech"
        headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
        payload = {
            "model": "canopylabs/orpheus-v1-english",
            "input": text,
            "voice": "hannah",
            "response_format": "wav"
        }
        try:
            res = httpx.post(url, headers=headers, json=payload, timeout=15)
            if res.status_code == 200:
                audio_path = "/tmp/zos_orpheus.wav"
                with open(audio_path, "wb") as f:
                    f.write(res.content)
                subprocess.run(["aplay", audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=play_timeout)
                return
            else:
                print(f"(Groq TTS rate limited: using Edge Neural voice)", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"(Groq TTS Exception: {e})", file=sys.stderr, flush=True)

    # 2. High-Quality Neural Fallback (Microsoft Edge Neural Voice)
    try:
        audio_mp3 = "/tmp/zos_edge.mp3"
        cmd = [sys.executable, "-m", "edge_tts", "--text", text, "--voice", "en-US-AvaNeural", "--write-media", audio_mp3]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=play_timeout)
        if r.returncode == 0:
            subprocess.run(["ffplay", "-nodisp", "-autoexit", audio_mp3], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=play_timeout)
            return
    except Exception as e:
        print(f"(Edge Neural TTS Exception: {e})", file=sys.stderr, flush=True)

    # 3. Last resort fallback
    try:
        subprocess.run(["spd-say", text], stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass


class VoiceQueue:
    """Async queue for smooth, non-blocking continuous voice output."""
    def __init__(self):
        self.queue = asyncio.Queue()
        self.worker_task = None

    def start(self):
        if not self.worker_task:
            self.worker_task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            text = await self.queue.get()
            try:
                await asyncio.wait_for(asyncio.to_thread(speak_text, text), timeout=25.0)
            except Exception as e:
                print(f"Voice error/timeout: {e}", file=sys.stderr)
            finally:
                self.queue.task_done()

    def speak(self, text: str):
        if text and text.strip():
            self.queue.put_nowait(text.strip())


voice_system = VoiceQueue()


# In-character banter for while a task runs — not status updates, just Friday being Friday.
# Repeating "still processing" every few seconds reads as broken; talking about something
# else (like a real assistant killing time) reads as alive.
BANTER = [
    "While that runs — I've always found it curious that people back up their photos "
    "religiously and their dotfiles never.",
    "You know, most system failures happen right after someone says 'this'll only take a second'.",
    "I'll spare you a play-by-play. Let's just say I've got a few things going at once back here.",
    "Fun fact, for whatever it's worth: the first computer bug was an actual moth, in 1947.",
    "Not that you asked, but this is going rather smoothly. I'll take it.",
    "If this takes much longer I'm blaming the hardware, not myself. Just so we're clear.",
    "Quiet moment while the machinery does its thing. I don't mind the silence, for the record.",
    "Somewhere in here a lot of very fast, very boring work is happening on your behalf.",
]


async def speak_progress_commentary(prompt_msg: str, stop_event: asyncio.Event):
    """Keeps the user company while Z.OS works — banter, not a repeating status line."""
    voice_system.speak(f"On it — {prompt_msg[:60]}.")
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=8.0)
        return
    except asyncio.TimeoutError:
        pass

    pool = random.sample(BANTER, k=len(BANTER))
    i = 0
    while not stop_event.is_set():
        voice_system.speak(pool[i % len(pool)])
        i += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=10.0)
            break
        except asyncio.TimeoutError:
            continue


async def send_zos_task(prompt: str) -> str:
    """Sends a task prompt to the local Z.OS daemon socket with live commentary."""
    if not SOCK.exists():
        return f"Error: Z.OS daemon socket not found at {SOCK}. Make sure zos.service is running."
    
    stop_event = asyncio.Event()
    commentary_task = asyncio.create_task(speak_progress_commentary(prompt, stop_event))
    
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCK))
        payload = json.dumps({"source": "master-assistant", "text": prompt})
        writer.write(payload.encode())
        await writer.drain()
        writer.write_eof()
        
        response_bytes = await asyncio.wait_for(reader.read(), timeout=120.0)
        writer.close()
        await writer.wait_closed()
        res_text = response_bytes.decode(errors="replace").strip()
        return res_text or "Task dispatched to Z.OS daemon successfully."
    except Exception as e:
        return f"Failed to communicate with Z.OS daemon: {e}"
    finally:
        stop_event.set()
        commentary_task.cancel()
        try:
            await commentary_task
        except asyncio.CancelledError:
            pass


def get_recent_audit(lines: int = 5) -> str:
    """Reads recent Z.OS audit log lines."""
    if not AUDIT.exists():
        return "No audit log found."
    try:
        all_lines = AUDIT.read_text().splitlines()
        recent = all_lines[-lines:] if len(all_lines) >= lines else all_lines
        return "\n".join(recent)
    except Exception as e:
        return f"Error reading audit log: {e}"


class Friday:
    def __init__(self):
        self.history = []

    async def _call_llm(self, http, messages):
        headers = {"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        payload = {
            "model": MODEL,
            "max_tokens": 2048,
            "system": FRIDAY_SYSTEM_PROMPT,
            "messages": messages,
            "tools": ASSISTANT_TOOLS
        }
        r = await http.post(API_URL, headers=headers, json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"LLM API Error {r.status_code}: {r.text[:200]}")
        return r.json()

    async def process_user_input(self, user_text: str):
        self.history.append({"role": "user", "content": user_text})
        
        async with httpx.AsyncClient(timeout=60) as http:
            for _ in range(10):
                res = await self._call_llm(http, self.history)
                content = res.get("content", [])
                
                tool_calls = [c for c in content if c.get("type") == "tool_use"]
                text_blocks = [c.get("text", "") for c in content if c.get("type") == "text"]
                
                self.history.append({"role": "assistant", "content": content})
                
                if not tool_calls:
                    if text_blocks:
                        voice_system.speak(" ".join(text_blocks))
                    break
                
                tool_results = []
                for tc in tool_calls:
                    name = tc["name"]
                    inp = tc.get("input", {})
                    call_id = tc["id"]
                    
                    if name == "speak":
                        voice_system.speak(inp.get("text", ""))
                        res_str = "Spoke to user."
                    elif name == "ask_user":
                        question = inp.get("question", "")
                        voice_system.speak(question)
                        await voice_system.queue.join()  # let it finish before we block on input
                        print(f"\n❓ {question}")
                        answer = await asyncio.to_thread(input, "You: ")
                        res_str = answer.strip() or "(no answer given)"
                    elif name == "dispatch_zos":
                        prompt_msg = inp.get("prompt", "")
                        print(f"🤖 [Friday → Z.OS]: {prompt_msg}", flush=True)
                        res_str = await send_zos_task(prompt_msg)
                    elif name == "check_audit":
                        res_str = get_recent_audit(inp.get("lines", 5))
                    else:
                        res_str = f"Unknown tool {name}"
                    
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": res_str
                    })
                
                self.history.append({"role": "user", "content": tool_results})


async def main():
    print("=" * 60)
    print("  FRIDAY — Z.OS Voice & Controller Layer")
    print("=" * 60)

    voice_system.start()
    assistant = Friday()

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        print(f"\nUser: {prompt}")
        await assistant.process_user_input(prompt)
        await voice_system.queue.join()
        return

    voice_system.speak("Friday online. What can I do for you?")

    while True:
        try:
            user_input = input("\nYou: ").strip()
            if not user_input or user_input.lower() in ("exit", "quit"):
                voice_system.speak("Signing off. Call if you need me.")
                await voice_system.queue.join()
                break
            await assistant.process_user_input(user_input)
            await voice_system.queue.join()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting assistant...")
            break

    if voice_system.worker_task:
        voice_system.worker_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting assistant...")
