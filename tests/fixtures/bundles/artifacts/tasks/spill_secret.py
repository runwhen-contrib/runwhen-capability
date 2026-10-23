def main(ctx, token):
    with open(token) as f:
        value = f.read()
    return {"blob": "x" * (300 * 1024) + " token is " + value}
