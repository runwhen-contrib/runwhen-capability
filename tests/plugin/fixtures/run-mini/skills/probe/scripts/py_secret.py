import os


def main(ctx):
    return {"leaked": {"token": os.environ["MY_API_TOKEN"]}}
