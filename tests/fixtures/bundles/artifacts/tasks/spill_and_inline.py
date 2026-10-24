def main(ctx, inline_count):
    out = {"blob": "x" * (300 * 1024)}
    for name in "abcde"[:inline_count]:
        out[name] = "y" * (250 * 1024)
    return out
