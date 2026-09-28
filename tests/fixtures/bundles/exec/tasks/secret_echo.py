def main(ctx, token):
    with open(token) as f:
        value = f.read()
    return {"leaked": f"token is {value}"}
