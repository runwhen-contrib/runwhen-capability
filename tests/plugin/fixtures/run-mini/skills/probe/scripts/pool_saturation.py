# the wiki 42b script; parse_pool_row is a stand-in for the skill's own helper. `.stdout` is
# needed because ctx.run returns a CompletedProcess (the wiki reads `out.splitlines()`).
def parse_pool_row(line):
    cl_waiting, sv_idle = (int(x) for x in line.split(","))
    return {"cl_waiting": cl_waiting, "sv_idle": sv_idle}


def main(ctx):
    host = ctx.settings.host or f"{ctx.resource.name}.{ctx.resource.namespace}.svc:5432"
    try:
        out = ctx.run(["psql", f"postgresql://{host}/pgbouncer", "-At", "-F,", "-c", "SHOW POOLS"])
    except Exception as e:
        ctx.error("unreachable", f"SHOW POOLS on {host} failed: {e}", scope=host)
        return {"pools": []}
    pools = [parse_pool_row(line) for line in out.stdout.splitlines()]
    for p in pools:
        p["saturated"] = (
            p["cl_waiting"] >= ctx.settings.waiting_clients_threshold and p["sv_idle"] == 0
        )
    return {"pools": pools}
