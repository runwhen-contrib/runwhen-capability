from runwhen_capability import RwError


def main(ctx):
    raise RwError("not_found", "no such deployment", scope="web")
