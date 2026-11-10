import os

import agent

# the server entry point (Railway): no tunnel, the platform gives a permanent public address and a PORT
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Webhook listening on port {port}", flush=True)
    agent.startup_checks()
    agent.ThreadingHTTPServer(("", port), agent.Webhook).serve_forever()
