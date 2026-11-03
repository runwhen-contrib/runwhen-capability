import os
import stat


def main(ctx, names=""):
    seen = {}
    for name in [n for n in names.split(",") if n]:
        value = os.environ.get(name)
        if value is not None and os.path.isfile(value):
            with open(value) as f:
                content = f.read()
            mode = oct(stat.S_IMODE(os.stat(value).st_mode))
            seen[name] = {"file": value, "len": len(content), "mode": mode, "content": content}
        else:
            seen[name] = {"value": value}
    return {"creds": seen}
