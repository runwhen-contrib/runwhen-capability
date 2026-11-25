import json


def main(ctx):
    done = ctx.run(["kubectl", "-n", ctx.resource.namespace, "get", "pods", "-o", "json"])
    return {
        "pods": {
            "namespace": ctx.resource.namespace,
            "count": len(json.loads(done.stdout)["items"]),
        }
    }
