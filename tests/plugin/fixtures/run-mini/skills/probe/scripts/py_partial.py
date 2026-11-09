def main(ctx):
    ctx.error("unreachable", "could not reach pgbouncer", scope="pgbouncer:6432")
    return {"pools": []}
