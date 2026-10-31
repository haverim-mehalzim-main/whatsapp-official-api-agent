import argparse
import json
import os
import urllib.error
import urllib.request

PHONE_NUMBER_ID = "1359994940539279"
API_VERSION = "v21.0"
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.txt")
TOKEN_KEY = "whatsapp_api_permanent_token"


def load_token():
    token = os.environ.get(TOKEN_KEY)
    if token:
        return token.strip()
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            key, _, value = line.strip().partition("=")
            if key.strip() == TOKEN_KEY:
                return value.strip().strip('"').strip("'")
    raise SystemExit(f"{TOKEN_KEY} not found in {ENV_FILE}")


def send(to, payload):
    url = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
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
