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
# the first message from a new caller, whatever it says, is always answered with this question and two buttons
GREETING = "היי זה יוני מחברים מחלצים, אתה או מישהו שאתה מכיר נמצא עכשיו במצב חירום בחו״ל?"

# once a caller is handed over they get these two messages, one after the other
HANDOFF_TEXT = "מיד אצור איתך קשר ממספר אחר"
DETAILS_QUESTIONS = "בינתיים תכתוב לי בבקשה\nמה השם המלא שלך?\nאיפה בעולם קרה האירוע?\nתיאור קצר של מה קרה."

# the details window: for this long after the questions, what the caller writes is collected (no reply to the
# caller); when it ends, the model reads everything once and one update goes to the on-call group
DETAILS_PROMPT_FILE = os.path.join(HERE, "details_prompt.txt")
DETAILS_WINDOW_SECONDS = 300
DETAILS_TICK_SECONDS = 5  # how often closed windows are looked for
MAX_DETAIL_CHARS = 500  # per message
MAX_WINDOW_CHARS = 2000  # per window; what goes past it is counted, not read
MAX_DETAIL_CALLS = 5  # model calls per window; past it, batches go to the group as written
MAX_INTAKE = 10  # caller messages kept from before the handover, passed into the window on "yes"

# the "no" path: a menu of three topics. Each topic answers the caller with a fixed text and posts one report to
# its route on the incident bot; for TOPIC_WINDOW_SECONDS afterwards every message gets TOPIC_FOLLOWUP, then the
# caller starts over with the greeting. A caller whose message fits no topic, even after the menu was sent again,
# is passed on as a general inquiry.
MENU_TEXT = "הבנתי. במה נוכל לעזור?"
MENU_BUTTONS = (("topic_info", "מסירת מידע"), ("topic_volunteer", "התנדבות"), ("topic_donation", "תרומות"))
TOPIC_BY_BUTTON = {"topic_info": "info", "topic_volunteer": "volunteer", "topic_donation": "donation"}
TOPICS = {
    "info": {
        "reply": "מעולה, כונן שלנו ייצור איתך קשר בהקדם",
        "path": "/api/incident-report",  # the operational on-call group, marked as information, not an emergency
        "header": "ℹ️ פנייה למסירת מידע בוואטסאפ",
        "body": "הפונה כתב כדי למסור מידע על אירוע. יש לחזור אליו.",
    },
    "volunteer": {
        "reply": "מעולה, אנחנו ניצור איתך קשר בהקדם",
        "path": "/api/volunteer-report",
        "header": "🙋 פנייה להתנדבות בוואטסאפ",
        "body": "הפונה רוצה להתנדב. יש לחזור אליו.",
    },
    "donation": {
        "reply": "מעולה, אנחנו ניצור איתך קשר בהקדם",
        "path": "/api/donation-report",
        "header": "💙 פנייה לתרומה בוואטסאפ",
        "body": "הפונה רוצה לתרום. יש לחזור אליו.",
    },
    "general": {
        "reply": "קיבלנו, ניצור איתך קשר בהקדם",
        "path": "/api/volunteer-report",  # the non-operational group, so someone sees it
        "header": "📩 פנייה כללית בוואטסאפ",
        "body": "הפונה כתב בנושא שאינו חירום, מסירת מידע, התנדבות או תרומה. יש לחזור אליו.",
    },
}
TOPIC_FOLLOWUP = "קיבלנו, ניצור איתך קשר בהקדם"
TOPIC_WINDOW_SECONDS = 3 * 3600
# after an emergency the number gets only the fixed text; this long after the handover it starts over
EMERGENCY_RELEASE_SECONDS = 24 * 3600
MENU_MISSES_BEFORE_GENERAL = 2  # unclear answers to the menu; the second one is passed on as a general inquiry
OUTBOX_KEY = "__outbox__"  # reports still waiting to be sent after their caller started over
# a first message that is only a greeting is left out of the reports to the group
GREETINGS = {"היי", "הי", "שלום", "אהלן", "הלו", "בוקר טוב", "ערב טוב", "צהריים טובים", "hi", "hello", "hey"}
MEDIA_NAMES = {"audio": "הקלטה", "voice": "הקלטה", "image": "תמונה", "video": "סרטון", "document": "מסמך", "sticker": "מדבקה"}
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


def ask_claude(history, system=None, max_tokens=1500, effort=None):
    # the model thinks before it answers, and that thinking counts toward max_tokens: a cap sized only for
    # the short JSON answer can be used up before any text is written, so leave room for both
    api_key = load_env().get("anthropic_api_key")
    if not api_key:
        raise RuntimeError("anthropic_api_key is empty in .env")
    request_body = {"model": MODEL, "max_tokens": max_tokens, "system": system or system_prompt(), "messages": history}
    if effort:
        request_body["output_config"] = {"effort": effort}
    body = json.dumps(request_body).encode("utf-8")
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
    text = "".join(block.get("text", "") for block in data["content"] if block.get("type") == "text").strip()
    if not text:
        # say why, so the log shows it (max_tokens: the thinking used up the cap; refusal: the model declined)
        raise RuntimeError(f"the model answered with no text (stop_reason={data.get('stop_reason')})")
    return text


def parse_verdict(raw):
    verdict = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    if verdict.get("assessment") not in ("emergency", "not_emergency", "unclear"):
        raise ValueError("unknown assessment")
    if not str(verdict.get("message", "")).strip():
        raise ValueError("empty message")
    if verdict.get("topic") not in ("info", "volunteer", "donation"):
        verdict["topic"] = "unclear"
    return verdict


def send_text(to, text):
    print(f"[reply to {to}] {text}")
    if DRY_RUN:
        return
    result = send_test.send_text(to, text)
    if "error" in result:
        print(f"[send error] {result['error']}")


def send_question(to, text, yes_label, no_label):
    send_buttons(to, text, ((YES_ID, yes_label), (NO_ID, no_label)))


def send_buttons(to, text, choices):
    # up to three tap buttons, each (id, title of at most 20 characters); they only work inside the 24h window,
    # which is always open after a message
    print(f"[question to {to}] {text}  " + " ".join(f"[{title}]" for _, title in choices))
    if DRY_RUN:
        return
    buttons = [{"type": "reply", "reply": {"id": button_id, "title": title[:20]}} for button_id, title in choices]
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
    text = oncall.build_text(sender)
    if DRY_RUN:
        print(f"[dry run] would post to the on-call group:\n{text}")
        ok = True
    else:
        ok = oncall.send_report(record["incident_id"], text)
    if ok:
        record["alert"] = "sent"
        save_state()
        send_detail_updates(record)  # updates that were waiting for the first alert
    else:
        print(f"[ALERT NOT SENT] {sender} is waiting for the on-call group, will retry")
    return ok


def retry_pending_alerts():
    for record in list(STATE.values()):
        try:
            send_detail_updates(record)
            send_topic_reports(record)
        except Exception as e:
            print(f"[details retry error] {e}")
    for sender in [s for s, r in list(STATE.items()) if r.get("alert") == "pending"]:
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


def declare_emergency(sender, summary, intake=None):
    # from here on the conversation belongs to a person: the model never answers it again. The caller gets the
    # fixed handoff and the details questions, the on-call group gets the first alert, and the details window opens
    # with what the caller wrote before the handover already in it
    now = int(time.time())
    STATE[sender] = {
        "status": "emergency",
        "incident_id": f"wa-{sender}-{now}",
        "summary": summary or "אין פרטים",
        "alert": "pending",
        "since": now,
        "details": {
            "until": now + DETAILS_WINDOW_SECONDS,
            "last_at": now,
            "pending": [],  # collected messages the model has not read yet
            "fields": {"name": "", "location": "", "what": ""},
            "chars": 0,
            "calls": 0,
            "dropped": 0,
            "dropped_reported": 0,
            "updates": [],  # [{"id", "text", "sent"}], posted to the group in order, after the first alert
        },
    }
    for item in intake or []:
        add_detail(STATE[sender]["details"], item)
    save_state()
    HISTORY.pop(sender, None)
    # the caller first, so they are never kept waiting on the incident bot; the alert is retried if it fails
    send_text(sender, HANDOFF_TEXT)
    send_text(sender, DETAILS_QUESTIONS)
    try_alert(sender)


def collect_detail(record, item):
    # keep one caller message for the details window; False when there is no open window
    details = record.get("details")
    if not details or time.time() >= details["until"]:
        return False
    add_detail(details, item)
    details["last_at"] = time.time()
    save_state()
    return True


def add_detail(details, item):
    # within the caps: up to MAX_DETAIL_CHARS per message and MAX_WINDOW_CHARS per window; past them, only counted
    room = MAX_WINDOW_CHARS - details["chars"]
    if room <= 0:
        details["dropped"] += 1
        return
    text = item["text"][: min(MAX_DETAIL_CHARS, room)]
    details["chars"] += len(text)
    details["pending"].append({**item, "text": text})


def line_of(item):
    # how a collected message shows in the group: a text message as its (capped) text in quotes,
    # a pin or media as the line describe() wrote for it
    return item.get("line") or f'"{item["text"]}"'


def details_prompt():
    with open(DETAILS_PROMPT_FILE, encoding="utf-8") as f:
        return f.read().strip()


def extract_details(known, texts):
    # one short model call: what is known so far plus only the new messages; returns (fields, is_new)
    content = json.dumps({"known": known, "new_messages": texts}, ensure_ascii=False)
    # a simple extraction: little thinking, with room left for it and for the answer
    raw = ask_claude([{"role": "user", "content": content}], system=details_prompt(), max_tokens=1024, effort="low")
    answer = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    fields = {key: str(answer.get(key) or "").strip() for key in ("name", "location", "what")}
    return fields, bool(answer.get("new"))


FLUSHING = set()  # senders whose batch is with the model right now


def flush_details(sender):
    # read the waiting messages with the model and, when they add something (or carry a pin or media),
    # queue an update for the on-call group. The model is called outside the lock so incoming messages
    # are never held up; messages are removed only once the update is queued, so a restart loses nothing
    with LOCK:
        record = STATE.get(sender, {})
        details = record.get("details")
        new_dropped = details["dropped"] - details["dropped_reported"] if details else 0
        if not details or sender in FLUSHING or not (details["pending"] or new_dropped):
            return
        batch = list(details["pending"])
        known = dict(details["fields"])
        use_model = details["calls"] < MAX_DETAIL_CALLS and bool(batch)
        if use_model:
            details["calls"] += 1
        FLUSHING.add(sender)
    try:
        fields, is_new = known, None  # None: the model was not asked or did not answer
        if use_model:
            try:
                fields, is_new = extract_details(known, [item["text"] for item in batch])
            except Exception as e:
                print(f"[details error] {e}; passing the messages on as written")
                fields, is_new = known, None
        with LOCK:
            del details["pending"][: len(batch)]
            always = new_dropped or any(item.get("always") for item in batch)
            if is_new is False and not always:
                print(f"[details] {sender}: nothing new, no update")
                save_state()
                return
            details["fields"] = fields
            details["dropped_reported"] = details["dropped"]
            number = len(details["updates"]) + 1
            details["updates"].append(
                {
                    "id": f"{record['incident_id']}-details-{number}",
                    "text": oncall.build_details_text(sender, fields, [line_of(item) for item in batch], new_dropped),
                    "sent": False,
                }
            )
            save_state()
    finally:
        FLUSHING.discard(sender)
    send_detail_updates(record)


def send_detail_updates(record):
    # post queued updates in order, and only after the first alert, so the group never gets an update
    # before it knows about the emergency; safe to repeat, the incident bot ignores an id it already has
    details = record.get("details")
    if not details or record.get("alert") != "sent":
        return
    for update in details["updates"]:
        if update["sent"]:
            continue
        if DRY_RUN:
            print(f"[dry run] would post to the on-call group:\n{update['text']}")
            ok = True
        else:
            ok = oncall.send_report(update["id"], update["text"])
        if not ok:
            print(f"[UPDATE NOT SENT] {update['id']}, will retry")
            return
        update["sent"] = True
        save_state()


def process_details():
    # flush every window whose time is up: one model call and one update with all that was collected
    now = time.time()
    for sender, record in list(STATE.items()):
        details = record.get("details")
        if not details or not (details["pending"] or details["dropped"] > details["dropped_reported"]):
            continue
        if now >= details["until"]:
            try:
                flush_details(sender)
            except Exception as e:
                print(f"[details error] {e}")


def details_loop():
    while True:
        time.sleep(DETAILS_TICK_SECONDS)
        process_details()


def show_menu(sender, tapped_note=None):
    # the caller said it is not an emergency: offer the three topics. A tapped "no" never reaches the model,
    # so it goes into the history here, as if the model had answered it, for the model to take over from
    record = STATE.setdefault(sender, {})
    record.update(stage="menu", menu_misses=0)
    save_state()
    if tapped_note:
        history = HISTORY.setdefault(sender, [])
        answered = {"assessment": "not_emergency", "topic": "unclear", "message": MENU_TEXT}
        history.append({"role": "user", "content": tapped_note})
        history.append({"role": "assistant", "content": json.dumps(answered, ensure_ascii=False)})
        del history[:-MAX_HISTORY]
    send_buttons(sender, MENU_TEXT, MENU_BUTTONS)


def handover_topic(sender, topic):
    # pass the caller on: the fixed text for the topic, and one report to its route with what they wrote
    spec = TOPICS[topic]
    now = int(time.time())
    lines = intake_lines(STATE.get(sender, {}).get("intake"))
    report = {
        "id": f"wa-{topic}-{sender}-{now}",
        "path": spec["path"],
        "text": oncall.build_topic_text(sender, spec["header"], spec["body"], lines),
        "sent": False,
    }
    STATE[sender] = {"status": "topic", "topic": topic, "since": now, "reports": [report]}
    save_state()
    HISTORY.pop(sender, None)
    print(f"[topic] {sender}: {topic}")
    send_text(sender, spec["reply"])
    send_topic_reports(STATE[sender])


def is_bare_greeting(text):
    words = "".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace()).split()
    return " ".join(words) in GREETINGS


def intake_lines(intake):
    # what the caller wrote before, as the group sees it: without a bare greeting, within the same caps
    lines, total = [], 0
    for item in intake or []:
        if not item.get("line") and is_bare_greeting(item["text"]):
            continue
        text = item["text"][:MAX_DETAIL_CHARS]
        if total + len(text) > MAX_WINDOW_CHARS:
            break
        total += len(text)
        lines.append(line_of({**item, "text": text}))
    return lines


def send_topic_reports(record):
    # safe to repeat, the incident bot ignores an id it already has. A route the incident bot does not have yet
    # answers 404: the report waits and is tried again, less and less often, and never goes to another group
    now = time.time()
    for report in record.get("reports", []):
        if report["sent"] or now < report.get("next_try", 0):
            continue
        if DRY_RUN:
            print(f"[dry run] would post to {report['path']}:\n{report['text']}")
            ok = True
        else:
            ok = oncall.send_report(report["id"], report["text"], path=report["path"])
        if ok:
            report["sent"] = True
        else:
            report["tries"] = report.get("tries", 0) + 1
            wait = min(ALERT_RETRY_SECONDS * 2 ** report["tries"], 1800)
            report["next_try"] = now + wait
            print(f"[REPORT NOT SENT] {report['id']} to {report['path']}, next try in {wait} seconds")
        save_state()
    if record is STATE.get(OUTBOX_KEY):
        record["reports"] = [report for report in record["reports"] if not report["sent"]]


def start_over(sender):
    # the topic window or the emergency block is over: forget the caller so the next message gets the greeting
    # again, keeping anything still waiting to be sent (in order: the emergency alert, then its updates)
    record = STATE.pop(sender, {})
    unsent = []
    if record.get("alert") == "pending":
        unsent.append({"id": record["incident_id"], "path": oncall.PATH, "text": oncall.build_text(sender), "sent": False})
    for update in (record.get("details") or {}).get("updates", []):
        if not update["sent"]:
            unsent.append({"id": update["id"], "path": oncall.PATH, "text": update["text"], "sent": False})
    unsent += [report for report in record.get("reports", []) if not report["sent"]]
    if unsent:
        STATE.setdefault(OUTBOX_KEY, {"reports": []})["reports"].extend(unsent)
    HISTORY.pop(sender, None)
    save_state()
    print(f"[start over] {sender}")


def remember_intake(sender, item):
    # what the caller writes before the handover is kept, so that on "yes" it goes into the details window
    # like an answer to the questions: a caller who told the whole story first still reaches the on-call group
    intake = STATE.setdefault(sender, {}).setdefault("intake", [])
    intake.append(item)
    del intake[:-MAX_INTAKE]
    save_state()


def greet(sender, text, item):
    # a new caller gets the fixed opening question, without a model call. The first message is kept as the
    # summary (the backup SMS uses it) and as the first intake item.
    # The question goes into the history as if the model had asked it, so the model takes over from the answer.
    summary = f"הודעה ראשונה: {text}"[:300]
    yes_label, no_label = FALLBACK_QUESTION["yes_label"], FALLBACK_QUESTION["no_label"]
    STATE[sender] = {"handoff": DEFAULT_HANDOFF, "summary": summary, "intake": [item]}
    save_state()
    asked = {
        "assessment": "unclear",
        "message": GREETING,
        "yes_label": yes_label,
        "no_label": no_label,
        "handoff": DEFAULT_HANDOFF,
        "summary": summary,
    }
    HISTORY[sender] = [
        {"role": "user", "content": text},
        {"role": "assistant", "content": json.dumps(asked, ensure_ascii=False)},
    ]
    print(f"[greeting] {sender} is a new caller")
    send_question(sender, GREETING, yes_label, no_label)


def reply_to(sender, text, button=None, item=None):
    # item: how the message goes into the details window (see describe); a plain text message by default
    item = item or {"text": text, "line": None, "always": False}
    record = STATE.get(sender, {})
    if record.get("status") == "emergency" and alert_age(record) >= EMERGENCY_RELEASE_SECONDS:
        # a day after the handover the number is released: a new emergency must reach the group again
        start_over(sender)
        record = {}
    if record.get("status") == "emergency":
        # inside the details window: collect silently (a tapped button carries no details);
        # after it: the fixed text, and never a model call or a new alert
        if button or collect_detail(record, item):
            if not button:
                print(f"[details] {sender}: message collected")
            return
        print(f"[fixed reply] {sender} was already handed to a representative")
        send_text(sender, FOLLOWUP_REPLY)
        return
    if record.get("status") == "topic":
        # handed to a person for a non-emergency topic: the fixed text for a while, then a fresh start
        if time.time() - record.get("since", 0) < TOPIC_WINDOW_SECONDS:
            print(f"[fixed reply] {sender} was already passed on ({record.get('topic')})")
            send_text(sender, TOPIC_FOLLOWUP)
            return
        start_over(sender)
        record = {}
    if button == YES_ID:
        declare_emergency(sender, record.get("summary"), record.get("intake"))
        return
    if button in TOPIC_BY_BUTTON:
        handover_topic(sender, TOPIC_BY_BUTTON[button])
        return
    if button == NO_ID:
        show_menu(sender, f'[The person tapped "{text}": this is NOT an emergency]')
        return
    if sender not in STATE and sender not in HISTORY:
        greet(sender, text, item)
        return
    remember_intake(sender, item)
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
    topic = verdict.get("topic", "unclear")
    print(f"[assessment {sender}] {verdict['assessment']}, topic {topic}")
    at_menu = STATE.get(sender, {}).get("stage") == "menu"

    if verdict["assessment"] == "emergency":
        declare_emergency(sender, verdict.get("summary"), STATE.get(sender, {}).get("intake"))
    elif topic != "unclear" and (at_menu or verdict["assessment"] == "not_emergency"):
        handover_topic(sender, topic)
    elif at_menu:
        # the caller already said it is not an emergency and wrote something that fits no topic
        record = STATE[sender]
        record["menu_misses"] = record.get("menu_misses", 0) + 1
        if record["menu_misses"] >= MENU_MISSES_BEFORE_GENERAL:
            handover_topic(sender, "general")
        else:
            save_state()
            send_buttons(sender, MENU_TEXT, MENU_BUTTONS)
    elif verdict["assessment"] == "not_emergency":
        show_menu(sender)
    elif verdict["assessment"] == "unclear":
        STATE.setdefault(sender, {}).update({"handoff": verdict.get("handoff"), "summary": verdict.get("summary")})
        save_state()
        send_question(
            sender,
            verdict["message"],
            verdict.get("yes_label") or FALLBACK_QUESTION["yes_label"],
            verdict.get("no_label") or FALLBACK_QUESTION["no_label"],
        )


def describe(message):
    # (text for the model, tapped button id or None, item for the details window); a caller in trouble may
    # send a pin or a voice note instead of words, and that still has to reach the model and the group.
    # item: "text" for the model, "line" as the on-call group sees it, "always" when the group gets it
    # even if the model finds nothing new in it (a pin or media the model cannot judge)
    kind = message.get("type")
    if kind == "text":
        text = message["text"]["body"]
        return text, None, {"text": text, "line": None, "always": False}  # line None: the text, quoted
    if kind == "interactive":
        reply = message["interactive"].get("button_reply") or {}
        return reply.get("title", ""), reply.get("id"), None
    if kind == "location":
        place = message["location"]
        lat, lon, name = place.get("latitude"), place.get("longitude"), place.get("name", "")
        text = f"[shared a location pin: {lat}, {lon} {name}]"
        line = f"📍 הפונה שיתף מיקום: https://maps.google.com/?q={lat},{lon}" + (f" ({name})" if name else "")
        return text, None, {"text": text, "line": line, "always": True}
    text = f"[sent a {kind} message that cannot be read automatically]"
    line = f"📎 הפונה שלח {MEDIA_NAMES.get(kind, 'קובץ')}. לא ניתן להעביר את הקובץ לכאן, יש לבקש ממנו בשיחה"
    return text, None, {"text": text, "line": line, "always": True}


def handle_message(message):
    sender, message_id = message.get("from"), message.get("id")
    with LOCK:
        if message_id in SEEN_IDS:
            return
        SEEN_IDS.append(message_id)
        del SEEN_IDS[:-500]
        text, button, item = describe(message)
        print(f"[message from {sender}] {text}")
        reply_to(sender, text, button, item)


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
    print("Chatting with the agent (Ctrl+C to quit). Type /yes or /no to tap the answer buttons, "
          "/info, /volunteer or /donation to tap a menu button.")
    menu = {"/info": "topic_info", "/volunteer": "topic_volunteer", "/donation": "topic_donation"}
    while True:
        text = input("you: ")
        if text in menu:
            reply_to("terminal", text, menu[text])
        elif text == "/yes":
            reply_to("terminal", "Yes", YES_ID)
        elif text == "/no":
            reply_to("terminal", "No", NO_ID)
        else:
            reply_to("terminal", text)


# alerts that failed (or were cut short by a restart) keep being retried until the group has them
threading.Thread(target=alert_retry_loop, daemon=True).start()
# caller details collected in a window are read and passed on in batches
threading.Thread(target=details_loop, daemon=True).start()


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
