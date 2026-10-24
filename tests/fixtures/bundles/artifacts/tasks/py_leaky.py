def main(ctx):
    writer = ctx.artifacts.open("dump", "text/plain")
    writer.write(b"x" * (2 * 1024 * 1024))  # whole chunks: the write is under way
    return {"note": "returned with the writer open"}
