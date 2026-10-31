import sys

import start

# usage: python register.py https://your-service.up.railway.app
# tells Meta where to send WhatsApp events; needed once for a permanent address (start.py does it for tunnels)
if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        raise SystemExit("usage: python register.py https://your-service.up.railway.app")
    print(start.register_callback(sys.argv[1].rstrip("/")))
