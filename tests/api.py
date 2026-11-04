import argparse
import getpass
import json
import os
import sys

import boto3
import requests

REGION = "ap-south-1"
cog = boto3.client("cognito-idp", region_name=REGION)

# demo users and their groups
USERS = {"quality1": "Quality", "ops1": "Operations", "auditor1": "Auditor", "nogroup1": None}


def ids():
    pools = cog.list_user_pools(MaxResults=60)["UserPools"]
    pool = next((p for p in pools if p["Name"] == "coldchain-users"), None)
    if not pool:
        sys.exit("user pool coldchain-users not found: run terraform apply, check the region")
    clients = cog.list_user_pool_clients(UserPoolId=pool["Id"], MaxResults=60)["UserPoolClients"]
    client = next(c for c in clients if c["ClientName"] == "coldchain-api-client")
    return pool["Id"], client["ClientId"]


def password():
    return os.environ.get("COLDCHAIN_PW") or getpass.getpass("demo password (12+ chars, upper, lower, digit, symbol): ")


def token(user, pw, client_id):
    res = cog.initiate_auth(
        ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": user, "PASSWORD": pw})
    return res["AuthenticationResult"]["AccessToken"]


def call(url, method, path, tok=None, body=None):
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    r = requests.request(method, url.rstrip("/") + path, headers=headers, json=body, timeout=20)
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data


def setup():
    pool_id, _ = ids()
    pw = password()
    for user, group in USERS.items():
        try:
            cog.admin_create_user(UserPoolId=pool_id, Username=user, MessageAction="SUPPRESS")
        except cog.exceptions.UsernameExistsException:
            pass
        cog.admin_set_user_password(UserPoolId=pool_id, Username=user, Password=pw, Permanent=True)
        if group:
            cog.admin_add_user_to_group(UserPoolId=pool_id, Username=user, GroupName=group)
        print(f"ready: {user} ({group or 'no group'})")


def cases(url):
    _, client_id = ids()
    pw = password()
    tok = {u: token(u, pw, client_id) for u in USERS}
    results = []

    def check(name, got, want):
        ok = got == want
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}  (got {got}, want {want})")

    code, data = call(url, "GET", "/excursions", tok["quality1"])
    if code != 200 or not data.get("excursions"):
        sys.exit("need at least one excursion to test with: run the simulator in hot mode first")
    exc = data["excursions"][0]["excursion_id"]

    check("no token", call(url, "GET", "/excursions")[0], 401)
    check("garbage token", call(url, "GET", "/excursions", "abc.def.ghi")[0], 401)
    check("user without a group", call(url, "GET", "/excursions", tok["nogroup1"])[0], 403)
    check("auditor lists excursions", call(url, "GET", "/excursions", tok["auditor1"])[0], 200)
    check("operations filters by status", call(url, "GET", "/excursions?status=OPEN", tok["ops1"])[0], 200)
    check("bad status value", call(url, "GET", "/excursions?status=BOGUS", tok["quality1"])[0], 400)
    check("auditor lists shipments", call(url, "GET", "/shipments", tok["auditor1"])[0], 200)
    code, data = call(url, "GET", "/shipments/SHP-1001", tok["quality1"])
    check("shipment detail", code, 200)
    check("shipment detail has readings", bool(data.get("recent_readings")), True)
    check("unknown shipment", call(url, "GET", "/shipments/SHP-9999", tok["quality1"])[0], 404)
    check("malformed excursion id", call(url, "GET", "/excursions/not-an-id", tok["quality1"])[0], 400)
    check("unknown excursion id", call(url, "GET", "/excursions/EXC-20200101-00000000", tok["quality1"])[0], 404)
    check("unknown route", call(url, "GET", "/nothing", tok["quality1"])[0], 404)
    check("auditor may not acknowledge", call(url, "POST", f"/excursions/{exc}/ack", tok["auditor1"], {})[0], 403)
    check("operations may not decide", call(url, "POST", f"/excursions/{exc}/disposition", tok["ops1"],
                                            {"decision": "RELEASE"})[0], 403)
    check("operations may not read evidence", call(url, "GET", f"/excursions/{exc}/evidence", tok["ops1"])[0], 403)
    code, data = call(url, "GET", f"/excursions/{exc}/evidence", tok["quality1"])
    check("quality reads evidence list", code, 200)
    files = data.get("files", [])
    fetched = requests.get(files[0]["url"], timeout=20).status_code if files else None
    check("presigned link downloads the file", fetched, 200)
    check("auditor reads evidence list", call(url, "GET", f"/excursions/{exc}/evidence", tok["auditor1"])[0], 200)

    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


def one(args):
    _, client_id = ids()
    tok = token(args.user, password(), client_id)
    path = args.path
    if "latest" in path:  # newest excursion, as seen by this user
        code, data = call(args.url, "GET", "/excursions", tok)
        if code != 200 or not data.get("excursions"):
            sys.exit(f"cannot resolve latest: status {code}")
        path = path.replace("latest", data["excursions"][0]["excursion_id"])
    body = {}
    if args.note:
        body["note"] = args.note
    if args.decision:
        body["decision"] = args.decision
    code, data = call(args.url, args.method.upper(), path, tok, body or None)
    print(code)
    print(json.dumps(data, indent=2))


p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="cmd", required=True)
sub.add_parser("setup")
c = sub.add_parser("cases")
c.add_argument("--url", required=True)
o = sub.add_parser("call")
o.add_argument("--url", required=True)
o.add_argument("--user", required=True)
o.add_argument("--note", default="")
o.add_argument("--decision", default="")
o.add_argument("method")
o.add_argument("path")
args = p.parse_args()

if args.cmd == "setup":
    setup()
elif args.cmd == "cases":
    cases(args.url)
else:
    one(args)