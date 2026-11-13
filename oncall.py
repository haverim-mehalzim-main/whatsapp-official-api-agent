import json
import urllib.error
import urllib.request

import agent

# the incident bot (incident_management) already accepts a report at this path and posts the text,
# unchanged, to its configured WhatsApp group
PATH = "/api/incident-report"

# each route on the incident bot accepts only its own event name (anything else is a 400), so a report built for
# one route can never be posted to another route's group by mistake
EVENT_BY_PATH = {
    "/api/incident-report": "incident.opened",
    "/api/volunteer-report": "volunteer.inquiry",
    "/api/donation-report": "donation.inquiry",
}


def build_text(sender):
    return (
        "🚨 פנייה דחופה בוואטסאפ\n"
        f"מספר: +{sender}\n"
        "הפונה קיבל הודעה שנציג יחזור אליו בהקדם. יש לחזור אליו."
    )


def build_details_text(sender, fields, lines, dropped=0):
    # the model's fields first (the full picture so far), then the caller's new messages exactly as written,
    # so a name or a number the model got wrong can always be checked against the original
    parts = [f"📝 עדכון פרטים, פנייה מ-+{sender}"]
    if any(fields.values()):
        parts += [
            f"שם הפונה: {fields.get('name') or 'לא נמסר'}",
            f"מיקום: {fields.get('location') or 'לא נמסר'}",
            f"מה קרה: {fields.get('what') or 'לא נמסר'}",
        ]
    if lines:
        parts += ["", "הודעות הפונה:", *lines]
    if dropped:
        parts.append(f"(הפונה שלח עוד {dropped} הודעות שלא הועברו, כי עבר אורך המקסימום)")
    return "\n".join(parts)


def build_topic_text(sender, header, body, lines):
    # a non-emergency inquiry (information, volunteering, donation, general), with what the caller wrote, as written
    parts = [header, f"מספר: +{sender}", body]
    if lines:
        parts += ["", "מה שהפונה כתב:", *lines]
    return "\n".join(parts)


def send_report(incident_id, text, path=PATH, fields=None):
    # returns True once the incident bot has accepted the report; the incident id makes retries safe,
    # because the bot ignores a report id it already received. path picks the route, and so the group.
    # fields: the optional structured fields the incident bot uses for its inquiries board on monday
    # (caller_phone, inquiry_type, and on a details update parent_incident_id and details); the group still
    # gets report_text exactly as written
    env = agent.load_env()
    base, secret = env.get("incident_agent_url", "").rstrip("/"), env.get("incident_agent_secret", "")
    if not base or not secret:
        print("[oncall error] incident_agent_url / incident_agent_secret are empty (.env or environment variables)")
        return False
    body = {"event": EVENT_BY_PATH.get(path, "incident.opened"), "incident_id": incident_id, "report_text": text}
    body.update(fields or {})
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as e:
        print(f"[oncall error] incident bot answered {e.code} on {path}")
        return False
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[oncall error] {e}")
        return False
    print(f"[oncall] report accepted, sent to the group: {result.get('sent')}")
    return bool(result.get("accepted"))
