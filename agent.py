import argparse
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import oncall
import send_test

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, ".env")
PROMPT_FILE = os.path.join(HERE, "system_prompt.txt")
# on a server DATA_DIR points at a persistent volume; without one the file is lost on every redeploy
STATE_FILE = os.path.join(os.environ.get("DATA_DIR", HERE), "triage_state.json")
MODEL = "claude-haiku-5-5"  # a yes/no/unclear call per message; the small model is enough and cheaper
MAX_HISTORY = 20  # messages kept per sender
ALERT_RETRY_SECONDS = 30
# an emergency that the on-call group has not received after this long is also sent to the backup numbers,
# and /health?strict=1 starts to fail so an uptime monitor can warn you
ESCALATE_AFTER_SECONDS = 300
STARTED_AT = time.time()

# after the handover the model is never called again, but a caller who keeps writing must not be met with silence:
# every message gets this fixed text (no tokens); the on-call group was told once, when the caller was handed over
FOLLOWUP_REPLY = (
    "קיבלנו את ההודעה שלך והעברנו אותה לנציג. הוא יחזור אליך בהקדם האפשרי. / "
    "We got your message and passed it on to the representative. They will get back to you as soon as possible."
)

YES_ID, NO_ID = "emergency_yes", "emergency_no"
DEFAULT_HANDOFF = (
    "קיבלנו. נציג שלנו יחזור אליך בהקדם האפשרי כדי לברר פרטים נוספים. / "
    "A representative will get back to you as soon as possible to ask for more details."
)
# used when the model is unreachable or answers garbage: failing towards asking beats staying silent
FALLBACK_QUESTION = {
    "assessment": "unclear",
    "message": "היי, כאן חברים מחלצים. רק כדי לוודא, זה מצב חירום עכשיו? / Just to make sure, is this an emergency right now?",
    "yes_label": "כן, זה חירום",
    "no_label": "לא, זה לא חירום",
    "handoff": DEFAULT_HANDOFF,
    "summary": "אין פרטים",
}

HISTORY = {}
SEEN_IDS = []
LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
DRY_RUN = False


ENV_KEYS = (
    "whatsapp_api_permanent_token",
    "anthropic_api_key",
    "whatsapp_verify_token",
    "meta_app_secret",
    "whatsapp_business_account_id",
    "meta_app_id",
    "twilio_account_sid",
    "twilio_auth_token",
    "twilio_number",
    "whatsapp_template_name",
    "whatsapp_template_language",
    "incident_agent_url",
    "incident_agent_secret",
    "backup_alert_numbers",
    "admin_secret",
    "test_numbers",
)


def load_env():
    # locally the values live in .env; on a server there is no such file, they are environment variables
    values = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key.strip() and not key.startswith("#"):
                    values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({k: os.environ[k].strip() for k in ENV_KEYS if os.environ.get(k)})
    return values


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# per sender: {"status": "emergency", "incident_id", "summary", "alert": "pending"|"sent"} once handed
# over, or {"handoff", "summary"} while the question is open. On disk so a restart does not wake the
# agent up again on a conversation that was already handed to a person.
STATE = load_state()


def save_state():
    if DRY_RUN:
        return
    with STATE_LOCK:
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(STATE, f, ensure_ascii=False, indent=2)
        except OSError as e:
            # never let a storage problem stop a caller from being helped or an emergency from being reported;
            # the state stays in memory, but it will be lost on a restart until the folder is writable
            print(f"[state error] could not save {STATE_FILE}: {e}")


def alert_age(record):
    # seconds since the caller was handed over (older records only carry the time inside the incident id)
    since = record.get("since")
    if since is None:
        try:
            since = int(str(record.get("incident_id", "")).rsplit("-", 1)[1])
        except (IndexError, ValueError):
            since = time.time()
    return time.time() - since


def state_dir_writable():
    folder = os.path.dirname(STATE_FILE)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return False
    return os.access(folder, os.W_OK)


def health():
    # numbers and flags only, never a caller's number or text: this page is public
    pending = [r for r in STATE.values() if r.get("alert") == "pending"]
    oldest = int(max((alert_age(r) for r in pending), default=0))
    data_dir = os.environ.get("DATA_DIR")
    warnings = []
    if data_dir and not os.path.ismount(data_dir):
        warnings.append("DATA_DIR is not a mounted volume: the handover state is lost on every restart")
    problems = []
    if not state_dir_writable():
        problems.append("the state folder is not writable")
    if oldest > ESCALATE_AFTER_SECONDS:
        problems.append("an emergency alert has not reached the on-call group for too long")
    return {
        "status": "problem" if problems else "ok",
        "problems": problems,
        "warnings": warnings,
        "uptime_seconds": int(time.time() - STARTED_AT),
        "pending_alerts": len(pending),
        "oldest_pending_seconds": oldest,
    }


def digits(number):
    return "".join(ch for ch in str(number) if ch.isdigit())


def reset_test_number(number, key):
    # testing helper: forget one number so it can be tried again from the beginning. It only works when
    # admin_secret is set, the key matches, and the number is listed in test_numbers, so a real caller
    # can never be reset. Returns (http code, message).
    env = load_env()
    secret = env.get("admin_secret", "")
    if not secret:
        return 404, "not found"  # switched off until a secret is configured
    if not key or not hmac.compare_digest(str(key), secret):
        print("[admin reset] refused: wrong key")
        return 403, "forbidden"
    number = digits(number)
    allowed = [digits(n) for n in env.get("test_numbers", "").split(",") if digits(n)]
    if not number or number not in allowed:
        print("[admin reset] refused: number is not a test number")
        return 403, "not a test number"
    with LOCK:  # the same lock as incoming messages, so a message cannot be half-processed during the reset
        STATE.pop(number, None)
        HISTORY.pop(number, None)
        save_state()
    print(f"[admin reset] {number} was reset")
    return 200, f"reset {number}: it will start from the beginning"


def startup_checks():
    # called once when the server starts, so a missing volume shows up in the logs right away
    info = health()
    for text in info["problems"] + info["warnings"]:
        print(f"[startup warning] {text}", flush=True)
    print(f"[startup] state file: {STATE_FILE}, {len(STATE)} saved conversations", flush=True)


def system_prompt():
    with open(PROMPT_FILE, encoding="utf-8") as f:
        return f.read().strip()


def ask_claude(history):
    api_key = load_env().get("anthropic_api_key")
    if not api_key:
        raise RuntimeError("anthropic_api_key is empty in .env")
    body = json.dumps(
        {"model": MODEL, "max_tokens": 500, "system": system_prompt(), "messages": history}
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude API error {e.code}: {e.read().decode('utf-8', 'replace')}")
    return "".join(block.get("text", "") for block in data["content"]).strip()


def parse_verdict(raw):
    verdict = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    if verdict.get("assessment") not in ("emergency", "not_emergency", "unclear"):
        raise ValueError("unknown assessment")
    if not str(verdict.get("message", "")).strip():
        raise ValueError("empty message")
    return verdict


def send_text(to, text):
    print(f"[reply to {to}] {text}")
    if DRY_RUN:
        return
    result = send_test.send_text(to, text)
    if "error" in result:
        print(f"[send error] {result['error']}")


def send_question(to, text, yes_label, no_label):
    # two tap buttons; they only work inside the 24h window, which is always open after a message
    print(f"[question to {to}] {text}  [{yes_label}] [{no_label}]")
    if DRY_RUN:
        return
    buttons = [
        {"type": "reply", "reply": {"id": YES_ID, "title": yes_label[:20]}},
        {"type": "reply", "reply": {"id": NO_ID, "title": no_label[:20]}},
    ]
    result = send_test.send(
        to,
        {
            "type": "interactive",
            "interactive": {"type": "button", "body": {"text": text[:1024]}, "action": {"buttons": buttons}},
        },
    )
    if "error" in result:
        print(f"[send error] {result['error']}")
        send_test.send_text(to, text)


def try_alert(sender):
    # tell the on-call group; safe to repeat, the incident id is the same on every attempt
    record = STATE.get(sender, {})
    if record.get("alert") != "pending":
        return True
    text = oncall.build_text(sender, record.get("summary") or "אין פרטים")
    if DRY_RUN:
        print(f"[dry run] would post to the on-call group:\n{text}")
        ok = True
    else:
        ok = oncall.send_report(record["incident_id"], text)
    if ok:
        record["alert"] = "sent"
        save_state()
    else:
        print(f"[ALERT NOT SENT] {sender} is waiting for the on-call group, will retry")
    return ok


def retry_pending_alerts():
    for sender in [s for s, r in STATE.items() if r.get("alert") == "pending"]:
        record = STATE[sender]
        try:
            if try_alert(sender):
                continue
            # the group still does not have it: after a while, tell the backup numbers too (once)
            if alert_age(record) > ESCALATE_AFTER_SECONDS and not record.get("escalated"):
                import alert  # imported here because alert imports agent

                if alert.escalate(sender, record.get("summary")):
                    record["escalated"] = True
                    save_state()
        except Exception as e:
            print(f"[alert retry error] {e}")


def alert_retry_loop():
    while True:
        time.sleep(ALERT_RETRY_SECONDS)
        retry_pending_alerts()


def declare_emergency(sender, handoff, summary):
    # from here on the conversation belongs to a person: the agent never answers it again
    STATE[sender] = {
        "status": "emergency",
        "incident_id": f"wa-{sender}-{int(time.time())}",
        "summary": summary or "אין פרטים",
        "alert": "pending",
        "since": int(time.time()),
    }
    save_state()
    HISTORY.pop(sender, None)
    try_alert(sender)
    send_text(sender, handoff or DEFAULT_HANDOFF)


def reply_to(sender, text, button=None):
    if STATE.get(sender, {}).get("status") == "emergency":
        # no model call and no new alert to the group, ever; only the fixed text
        print(f"[fixed reply] {sender} was already handed to a representative")
        send_text(sender, FOLLOWUP_REPLY)
        return
    if button == YES_ID:
        record = STATE.get(sender, {})
        declare_emergency(sender, record.get("handoff"), record.get("summary"))
        return
    if button == NO_ID:
        text = f'[The person tapped "{text}": this is NOT an emergency]'
    history = HISTORY.setdefault(sender, [])
    history.append({"role": "user", "content": text})
    del history[:-MAX_HISTORY]
    while history and history[0]["role"] != "user":
        history.pop(0)
    try:
        raw = ask_claude(history)
    except Exception as e:
        print(f"[error] {e}")
        history.pop()
        verdict = FALLBACK_QUESTION
    else:
        try:
            verdict = parse_verdict(raw)
        except ValueError as e:
            print(f"[error] unreadable answer from the model ({e})")
            verdict = FALLBACK_QUESTION
            raw = json.dumps(verdict, ensure_ascii=False)
        history.append({"role": "assistant", "content": raw})
    print(f"[assessment {sender}] {verdict['assessment']}")

    if verdict["assessment"] == "emergency":
        declare_emergency(sender, verdict.get("handoff") or verdict["message"], verdict.get("summary"))
    elif verdict["assessment"] == "unclear":
        STATE[sender] = {"handoff": verdict.get("handoff"), "summary": verdict.get("summary")}
        save_state()
        send_question(
            sender,
            verdict["message"],
            verdict.get("yes_label") or FALLBACK_QUESTION["yes_label"],
            verdict.get("no_label") or FALLBACK_QUESTION["no_label"],
        )
    else:
        send_text(sender, verdict["message"])


def describe(message):
    # (text for the model, tapped button id or None); a caller in trouble may send a pin or a voice
    # note instead of words, and that still has to reach the model
    kind = message.get("type")
    if kind == "text":
        return message["text"]["body"], None
    if kind == "interactive":
        reply = message["interactive"].get("button_reply") or {}
        return reply.get("title", ""), reply.get("id")
    if kind == "location":
        place = message["location"]
        return f"[shared a location pin: {place.get('latitude')}, {place.get('longitude')} {place.get('name', '')}]", None
    return f"[sent a {kind} message that cannot be read automatically]", None


def handle_message(message):
    sender, message_id = message.get("from"), message.get("id")
    with LOCK:
        if message_id in SEEN_IDS:
            return
        SEEN_IDS.append(message_id)
        del SEEN_IDS[:-500]
        text, button = describe(message)
        print(f"[message from {sender}] {text}")
        reply_to(sender, text, button)


def handle_payload(payload):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                handle_message(message)
            for status in value.get("statuses", []):
                errors = status.get("errors")
                print(f"[status] {status.get('recipient_id')} {status.get('status')}" + (f" errors={errors}" if errors else ""))
                import alert  # imported here because alert imports agent

                alert.on_status(status)


class Webhook(BaseHTTPRequestHandler):
    def health_response(self, send_body):
        # /health always answers 200 while the process is alive (Railway's health check uses it);
        # /health?strict=1 answers 503 when something needs a person, which is what an uptime monitor should watch
        from urllib.parse import parse_qs, urlparse

        strict = parse_qs(urlparse(self.path).query).get("strict") == ["1"]
        info = health()
        code = 503 if strict and info["status"] != "ok" else 200
        self.respond(code, json.dumps(info), "application/json", send_body)

    def do_HEAD(self):
        # some uptime monitors check with HEAD
        from urllib.parse import urlparse

        if urlparse(self.path).path == "/health":
            self.health_response(False)
        else:
            self.respond(403, "forbidden", send_body=False)

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse

        if urlparse(self.path).path == "/health":
            self.health_response(True)
            return
        query = parse_qs(urlparse(self.path).query)
        if urlparse(self.path).path == "/admin/reset":
            bearer = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            code, message = reset_test_number(
                (query.get("number") or [""])[0], (query.get("key") or [""])[0] or bearer
            )
            self.respond(code, message)
            return
        # Meta's one-time verification handshake
        token = load_env().get("whatsapp_verify_token")
        if query.get("hub.mode") == ["subscribe"] and token and query.get("hub.verify_token") == [token]:
            self.respond(200, query["hub.challenge"][0])
        else:
            self.respond(403, "forbidden")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        secret = load_env().get("meta_app_secret")
        if secret:
            expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, self.headers.get("X-Hub-Signature-256", "")):
                self.respond(403, "bad signature")
                return
        self.respond(200, "ok")  # answer Meta fast, work in the background
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        threading.Thread(target=handle_payload, args=(payload,), daemon=True).start()

    def respond(self, code, text, content_type="text/plain", send_body=True):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, *args):
        pass


def chat():
    # talk to the agent in the terminal, nothing is sent to WhatsApp or to the on-call group
    print("Chatting with the agent (Ctrl+C to quit). Type /yes or /no to tap the answer buttons.")
    while True:
        text = input("you: ")
        if text == "/yes":
            reply_to("terminal", "Yes", YES_ID)
        elif text == "/no":
            reply_to("terminal", "No", NO_ID)
        else:
            reply_to("terminal", text)


# alerts that failed (or were cut short by a restart) keep being retried until the group has them
threading.Thread(target=alert_retry_loop, daemon=True).start()


if __name__ == "__main__":
    # usage: python agent.py              -> run the webhook server on port 8080
    #        python agent.py --dry-run    -> same, but print replies instead of sending them
    #        python agent.py --chat       -> test the agent in the terminal
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--chat", action="store_true")
    args = parser.parse_args()
    DRY_RUN = args.dry_run or args.chat
    if args.chat:
        chat()
    else:
        print(f"Webhook listening on port {args.port}" + (" (dry run)" if DRY_RUN else ""))
        startup_checks()
        ThreadingHTTPServer(("", args.port), Webhook).serve_forever()
