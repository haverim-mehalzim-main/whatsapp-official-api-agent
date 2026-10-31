import argparse
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import agent
import send_test

CLOUDFLARED = r"C:\Program Files (x86)\cloudflared\cloudflared.exe"


def start_tunnel(port):
    process = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in process.stderr:
        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if match:
            threading.Thread(target=lambda: [None for _ in process.stderr], daemon=True).start()
            return process, match.group(0)
    raise SystemExit("cloudflared exited without giving a URL")


def post_subscribed_apps(params):
    waba = agent.load_env()["whatsapp_business_account_id"]
    request = urllib.request.Request(
        f"https://graph.facebook.com/{send_test.API_VERSION}/{waba}/subscribed_apps",
        data=urllib.parse.urlencode(params).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {send_test.load_token()}"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        return json.load(e)


def register_callback(url):
    env = agent.load_env()
    # the app must be subscribed to the WhatsApp account to receive its webhooks
    subscribed = post_subscribed_apps({})
    if not subscribed.get("success"):
        return subscribed
    # set the app's webhook address, authenticated with the app access token (app id|app secret)
    request = urllib.request.Request(
        f"https://graph.facebook.com/{send_test.API_VERSION}/{env['meta_app_id']}/subscriptions",
        data=urllib.parse.urlencode(
            {
                "object": "whatsapp_business_account",
                "callback_url": url,
                "verify_token": env["whatsapp_verify_token"],
                "fields": "messages",
                "access_token": f"{env['meta_app_id']}|{env['meta_app_secret']}",
            }
        ).encode("utf-8"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        return json.load(e)


if __name__ == "__main__":
    # usage: python start.py                 -> start agent + tunnel, register the new URL with Meta
    #        python start.py --no-register   -> start agent + tunnel and just print the URL
    #        python start.py --dry-run       -> agent prints replies instead of sending them
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-register", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    agent.DRY_RUN = args.dry_run

    server = agent.ThreadingHTTPServer(("", args.port), agent.Webhook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Agent listening on port {args.port}" + (" (dry run)" if args.dry_run else ""))

    tunnel, url = start_tunnel(args.port)
    print(f"Tunnel: {url}")
    try:
        if args.no_register:
            print("Not registering with Meta (--no-register).")
        else:
            # the new tunnel hostname can take a few seconds to resolve for Meta
            for attempt in range(1, 7):
                result = register_callback(url)
                if result.get("success"):
                    print(f"Registered {url} with Meta.")
                    break
                print(f"Register attempt {attempt} failed: {result.get('error', result)}")
                time.sleep(5)
            else:
                print("Could not register the callback URL. Enter it manually in Meta.")
        print("Running. Press Ctrl+C to stop.")
        tunnel.wait()
    except KeyboardInterrupt:
        pass
    finally:
        tunnel.terminate()
