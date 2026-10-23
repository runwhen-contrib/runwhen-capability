def main(ctx):
    with ctx.artifacts.open("dump", "text/plain") as out:
        out.write(b"written by a python task\n")
    return {"dump": out.ref}
