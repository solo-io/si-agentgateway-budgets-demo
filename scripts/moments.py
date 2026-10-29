#!/usr/bin/env python3
"""The scripted demo moments, one command each.

  tokens            Maria's two tokens side by side: bench-app vs project-atlas-app
  maria             Maria calls on bench, then Atlas, then Beacon; shows which counter moved
  block-tom         tom.vega loops on bench-app until his $3 daily cap returns 429,
                    then diego.ramos on Atlas still works
  wrong-app         tom.vega (bench only) gets a project-cobalt-app token; the gateway refuses it
  service-app       atlas-claims-summarizer fetches its own token and calls the model
  real [user] [app] one call to the real Anthropic API (needs ANTHROPIC_API_KEY in .env)
  status            dollar budget counters, read from the gateway API
"""

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agw  # noqa: E402
import personas as P  # noqa: E402

B, DIM, RED, GREEN, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[0m"
FLUSH_WAIT = 2  # let the response settle before reading the counters again


def counters():
    """Open budget counters from the gateway's admin API: {(budget, subject): used USD}.

    Read through the API, never from the SQLite file: opening the file from the
    host while the gateway writes it can corrupt it.
    """
    with urllib.request.urlopen(f"{agw.ADMIN}/api/budgets/usage", timeout=5) as r:
        data = json.loads(r.read())
    out = {}
    for b in data.get("budgets", []):
        if b.get("window", {}).get("expired"):
            continue
        subject = next(iter(b.get("subject", {}).values()), "")
        out[(b["name"], subject)] = float(b["usage"]["used"])
    return out


LIMITS = {"bench-monthly": P.BENCH_LIMIT, "tom-vega-daily": P.TOM_DAILY_LIMIT, "cobalt-daily": P.COBALT_DAILY_LIMIT,
          **{f"{p}-monthly": v for p, v in P.PROJECT_LIMITS.items()}}


def usd(x):
    return f"${x:,.2f}"


def bar(used, limit, width=30):
    frac = used / limit
    filled = min(width, int(frac * width))
    color = RED if frac >= 1 else ("\033[33m" if frac >= 0.8 else GREEN)
    return f"{color}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET} {100 * frac:4.0f}%"


def show_diff(before, after, focus=None):
    for key in sorted(set(before) | set(after)):
        if focus and key[1] not in focus:
            continue
        b, a = before.get(key, 0), after.get(key, 0)
        mark = f"{B}+${a - b:,.4f}{RESET}" if abs(a - b) > 1e-9 else f"{DIM}unchanged{RESET}"
        print(f"   {key[0]:16} {key[1]:14} {usd(a):>11}  {mark}")


def one_call(label, client, user, tin=20000, tout=1000, real=False, model="claude-sonnet-4-5"):
    jwt = agw.token(client, user)
    status, body, secs = agw.call(jwt, model, tin, tout, real=real,
                                  user_agent=P.SDK_UA if user is None else P.HUMAN_UA,
                                  prompt="In two sentences, what does an AI gateway do?" if real else
                                  "Summarize the attached claim notes.")
    if status == 200:
        u = body.get("usage", {})
        n = u.get("input_tokens", 0) + u.get("output_tokens", 0)
        c = P.cost_usd(body.get("model"), u.get("input_tokens", 0), u.get("output_tokens", 0))[0]
        print(f"{GREEN}200{RESET} {label}: {n:,} tokens = ${c:.4f}, model {body.get('model')}, {secs*1000:.0f} ms")
        if real:
            print(f"   {DIM}{body['content'][0]['text']}{RESET}")
    else:
        print(f"{RED}{status}{RESET} {label}: {json.dumps(body)[:200]}")
    return status


def cmd_tokens():
    a = agw.claims(agw.token("bench-app", "maria.gomez"))
    b = agw.claims(agw.token("project-atlas-app", "maria.gomez"))
    keys = ["preferred_username", "azp", "aud", "groups", "bench_user", "project"]
    print(f"{'claim':20} {'bench-app':32} {'project-atlas-app':32}")
    for k in keys:
        va, vb = json.dumps(a.get(k)), json.dumps(b.get(k))
        hl = B if va != vb else ""
        print(f"{hl}{k:20} {va:32} {vb:32}{RESET}")
    print(f"\n{DIM}The only difference that matters: bench_user vs project.{RESET}")


def cmd_maria():
    for client, focus in [("bench-app", {"maria.gomez", "atlas", "beacon"}),
                          ("project-atlas-app", {"maria.gomez", "atlas", "beacon"}),
                          ("project-beacon-app", {"maria.gomez", "atlas", "beacon"})]:
        before = counters()
        one_call(f"maria.gomez via {client}", client, "maria.gomez", 20000, 1000)
        time.sleep(FLUSH_WAIT)
        show_diff(before, counters(), focus)
        print()


def cmd_block_tom():
    print(f"tom.vega daily cap: ${P.TOM_DAILY_LIMIT} (Block). Looping on bench-app with Opus...\n")
    jwt = agw.token("bench-app", "tom.vega")
    spent = 0.0
    for i in range(1, 40):
        status, body, _ = agw.call(jwt, "claude-opus-4-5", 60000, 3000)
        if status == 429:
            print(f"{RED}{B}429 Budget exceeded{RESET} on call {i}: {json.dumps(body)}")
            break
        u = body.get("usage", {})
        c = P.cost_usd("claude-opus-4-5", u.get("input_tokens", 0), u.get("output_tokens", 0))[0]
        spent += c
        print(f"  call {i:2}: 200, ${c:.2f}  (${spent:.2f} so far)")
        time.sleep(0.4)
    else:
        print("No block after 40 calls. Is the tom-vega-daily budget in the config?")
        return
    print()
    one_call("diego.ramos via project-atlas-app, same minute", "project-atlas-app", "diego.ramos", 20000, 1000)
    print(f"\n{DIM}Tom hit his own ceiling. Nobody else noticed.{RESET}")


def cmd_wrong_app():
    jwt = agw.token("project-cobalt-app", "tom.vega")
    c = agw.claims(jwt)
    print(f"Keycloak issued it: project={c.get('project')!r}, groups={c.get('groups')}")
    status, body, _ = agw.call(jwt, "claude-sonnet-4-5", 1000, 200)
    color = GREEN if status == 403 else RED
    print(f"Gateway: {color}{status}{RESET} {json.dumps(body)[:160]}")
    print(f"{DIM}The gateway checks that the person is in project-cobalt before billing Cobalt.{RESET}")


def cmd_service_app():
    app = "atlas-claims-summarizer"
    c = agw.claims(agw.token(app))
    print(f"{app} token: project={c.get('project')!r}, app={c.get('app')!r}, sub={c.get('sub')}")
    time.sleep(FLUSH_WAIT)  # let earlier calls reach the database so the diff shows only this one
    before = counters()
    one_call(app, app, None, 30000, 900, model="claude-haiku-4-5")
    time.sleep(FLUSH_WAIT)
    show_diff(before, counters(), {"atlas"})


def cmd_real(user="maria.gomez", client="project-atlas-app"):
    one_call(f"{user} via {client} (real Anthropic)", client, user, real=True, model="claude-haiku-4-5")


def cmd_status():
    c = counters()
    print(f"{B}Projects (shared counters, 30-day window){RESET}")
    for p in P.PROJECT_LIMITS:
        used = c.get((f"{p}-monthly", p), 0)
        print(f"  {p:12} {usd(used):>10} / {usd(P.PROJECT_LIMITS[p]):>9}  {bar(used, P.PROJECT_LIMITS[p])}  {P.PROJECT_ACTIONS[p]}")
    print(f"\n{B}Bench (one counter per person, 30-day window){RESET}")
    for u in P.USERS:
        used = c.get(("bench-monthly", u), 0)
        print(f"  {u:12} {usd(used):>10} / {usd(P.BENCH_LIMIT):>9}  {bar(used, P.BENCH_LIMIT)}")
    print(f"\n{B}Demo overrides (24h){RESET}")
    for name, subj, lim in [("tom-vega-daily", "tom.vega", P.TOM_DAILY_LIMIT), ("cobalt-daily", "cobalt", P.COBALT_DAILY_LIMIT)]:
        used = c.get((name, subj), 0)
        print(f"  {name:14} {usd(used):>8} / {usd(lim):>7}  {bar(used, lim)}")


COMMANDS = {"tokens": cmd_tokens, "maria": cmd_maria, "block-tom": cmd_block_tom, "wrong-app": cmd_wrong_app,
            "service-app": cmd_service_app, "real": cmd_real, "status": cmd_status}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[sys.argv[1]](*sys.argv[2:])
