import argparse
import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import agent
import send_test

CALL_TWIML = (
    "<Response><Say>We received a call that needs your response. "
    "Please check WhatsApp now.</Say></Response>"
)
DRY_RUN = False

PENDING = {}  # WhatsApp message ID -> (employee_number, callback_number)
PENDING_LOCK = threading.Lock()


def twilio_post(path, params):
    env = agent.load_env()
    sid = env.get("twilio_account_sid")
    token = env.get("twilio_auth_token")
    if not sid or not token:
        raise RuntimeError("twilio_account_sid / twilio_auth_token are empty in .env")
    credentials = base64.b64encode(f"{sid}:{token}".encode()).decode()
    request = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/{path}.json",
        data=urllib.parse.urlencode(params).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Basic {credentials}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Twilio error {e.code}: {e.read().decode('utf-8', 'replace')}")


def place_call(employee_number):
    if DRY_RUN:
        print(f"[dry run] call {employee_number}")
        return "dry-run"
    number = agent.load_env().get("twilio_number")
    return twilio_post("Calls", {"To": employee_number, "From": number, "Twiml": CALL_TWIML})["sid"]


def send_sms(employee_number, callback_number):
    body = f"We got a call. Please call {callback_number} now."
    print(f"[sms fallback] {employee_number}")
    if DRY_RUN:
        print(f"[dry run] sms {body}")
        return
    number = agent.load_env().get("twilio_number")
    twilio_post("Messages", {"To": employee_number, "From": number, "Body": body})


def send_whatsapp(employee_number, callback_number):
    if DRY_RUN:
        print(f"[dry run] whatsapp template to {employee_number}: {callback_number}")
        return None
    env = agent.load_env()
    result = send_test.send(
        employee_number.lstrip("+"),  # digits only, with country code
        {
            "type": "template",
            "template": {
                "name": env.get("whatsapp_template_name", ""),
                "language": {"code": env.get("whatsapp_template_language", "en")},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": callback_number}]}
                ],
            },
        },
    )
    if "error" in result:
        print(f"[whatsapp error] {result['error']}")
        send_sms(employee_number, callback_number)  # the send request itself failed
        return None
    wamid = result["messages"][0]["id"]
    with PENDING_LOCK:
        PENDING[wamid] = (employee_number, callback_number)
    return wamid


def alert_employee(employee_number, callback_number):
    # the call and the WhatsApp template go out together; returns (call SID, WhatsApp message ID)
    with ThreadPoolExecutor() as pool:
        call_future = pool.submit(place_call, employee_number)
        whatsapp_future = pool.submit(send_whatsapp, employee_number, callback_number)
        try:
            call_sid = call_future.result()
        except Exception as e:
            print(f"[call error] {e}")
            call_sid = None
        return call_sid, whatsapp_future.result()


def on_status(status):
    # called by the webhook for every WhatsApp delivery status
    wamid, state = status.get("id"), status.get("status")
    with PENDING_LOCK:
        if state == "failed" and wamid in PENDING:
            employee_number, callback_number = PENDING.pop(wamid)
        else:
            if state in ("delivered", "read"):
                PENDING.pop(wamid, None)
            return
    try:
        send_sms(employee_number, callback_number)
    except Exception as e:
        print(f"[sms error] {e}")


if __name__ == "__main__":
    # usage: python alert.py EMPLOYEE_NUMBER CALLBACK_NUMBER            -> call + WhatsApp (+ SMS if WhatsApp fails)
    #        python alert.py EMPLOYEE_NUMBER CALLBACK_NUMBER --dry-run  -> print what would be done
    # the SMS fallback on a failed status only works inside the running webhook server (start.py)
    parser = argparse.ArgumentParser()
    parser.add_argument("employee_number")
    parser.add_argument("callback_number")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    DRY_RUN = args.dry_run
    print(alert_employee(args.employee_number, args.callback_number))
