import argparse
import json
import os
import urllib.error
import urllib.request

DEFAULT_PHONE_NUMBER_ID = "1359994940539279"  # used until whatsapp_phone_number_id is set (env variable or .env)
API_VERSION = "v21.0"
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
TOKEN_KEY = "whatsapp_api_permanent_token"
PHONE_ID_KEY = "whatsapp_phone_number_id"


def load_setting(key):
    value = os.environ.get(key)
    if value:
        return value.strip()
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                name, _, value = line.strip().partition("=")
                if name.strip() == key and value.strip():
                    return value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return None


def load_token():
    token = load_setting(TOKEN_KEY)
    if not token:
        raise SystemExit(f"{TOKEN_KEY} not found in the environment or in {ENV_FILE}")
    return token


def phone_number_id():
    return load_setting(PHONE_ID_KEY) or DEFAULT_PHONE_NUMBER_ID


def send(to, payload):
    url = f"https://graph.facebook.com/{API_VERSION}/{phone_number_id()}/messages"
    body = json.dumps({"messaging_product": "whatsapp", "to": to, **payload}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {load_token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        return json.load(e)


def send_template(to, name="hello_world", language="en_US"):
    return send(to, {"type": "template", "template": {"name": name, "language": {"code": language}}})


def send_text(to, text):
    return send(to, {"type": "text", "text": {"body": text}})


if __name__ == "__main__":
    # usage: python send_test.py 31648681560                                  -> hello_world template
    #        python send_test.py 31648681560 --template NAME [--lang en_US]   -> any approved template
    #        python send_test.py 31648681560 --text "some text"               -> free text (only within 24h of their last message)
    parser = argparse.ArgumentParser()
    parser.add_argument("to", help="number with country code, no +")
    parser.add_argument("--template", default="hello_world")
    parser.add_argument("--lang", default="en_US")
    parser.add_argument("--text")
    args = parser.parse_args()
    result = send_text(args.to, args.text) if args.text else send_template(args.to, args.template, args.lang)
    print(json.dumps(result, indent=2, ensure_ascii=False))
