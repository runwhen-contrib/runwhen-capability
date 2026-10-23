import os


def main(ctx):
    return {"dir": os.environ["RW_ARTIFACTS_DIR"]}
