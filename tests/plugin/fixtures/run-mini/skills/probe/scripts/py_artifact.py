import os


def main(ctx):
    with open(os.path.join(os.environ["RW_OUT"], "dump.txt"), "w") as f:
        f.write("a dump\n")
    with open(os.path.join(os.environ["RW_OUT"], "scratch.txt"), "w") as f:
        f.write("not an output\n")
    return {"n": {"v": 1}}
