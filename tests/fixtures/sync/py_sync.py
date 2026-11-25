import json
import os
import stat

HOST_ONLY = ("RELAY_URL", "POOL_ID", "EXECUTOR_TOKEN_FILE", "RW_EXECUTOR_TOKEN_FILE")


def main(ctx):
    path = os.environ.get("RW_SYNC_CREDENTIAL")
    host_only = sum(name in os.environ for name in HOST_ONLY)
    if path is None:
        return {"sync": {"path": None, "hostOnly": host_only}}
    with open(path) as f:
        doc = json.load(f)
    return {
        "sync": {
            "path": path,
            "mode": oct(stat.S_IMODE(os.stat(path).st_mode)),
            "apiBaseUrl": doc["apiBaseUrl"],
            "workspace": doc["workspace"],
            "token": doc["token"],
            "hostOnly": host_only,
        }
    }
