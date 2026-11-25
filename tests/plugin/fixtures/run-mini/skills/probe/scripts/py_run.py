def main(ctx, snippet="false"):
    ctx.run(["sh", "-c", snippet])
    return {}
