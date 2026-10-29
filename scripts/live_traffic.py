#!/usr/bin/env python3
"""Continuous synthetic traffic through the gateway, while you demo.

Every request is real: a Keycloak token for the persona's app, a call to
/v1/messages on the gateway, JWT validation, budget check and charge, request
log. Only the model is a mock (it reports the token counts this script asks
for), so the run costs nothing.

The firm's day is compressed: by default one real minute is one business hour,
so a 24-minute loop shows a morning ramp, a lunch dip, and the nightly batch
jobs (Beacon test generation, the Atlas code migrator).

tom.vega is left out of the background traffic so his 20k daily cap is still
there for the live block moment (scripts/moments.py block-tom).

Usage:
  python3 scripts/live_traffic.py [--rps 0.6] [--scale 0.5] [--speed 60] [--start-hour 9]
"""

import argparse
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import agw  # noqa: E402
import personas as P  # noqa: E402
from backfill import hour_weight, lognormal, pick  # noqa: E402

PROMPTS = {
    "atlas": ["Summarize claim CLM-20931 and flag missing documents.",
              "Map this COBOL copybook to a Java DTO.",
              "Why does the claims API return 500 on a duplicate claim number?"],
    "beacon": ["Generate pytest cases for the premium rounding function.",
               "Write boundary tests for multi-vehicle quotes.",
               "Explain this flaky test failure."],
    "cobalt": ["Write five headline options for the autumn campaign.",
               "Rewrite this paragraph in the house style.",
               "Draft a 120-word teaser for the documentary."],
    None: ["Explain Kubernetes Gateway API in two paragraphs.",
           "Help me study for the AWS Solutions Architect exam.",
           "Review my Python for idiomatic style."],
}

COLORS = {"bench": "\033[36m", "project-atlas": "\033[33m", "project-beacon": "\033[35m",
          "project-cobalt": "\033[32m"}
RESET, RED, DIM = "\033[0m", "\033[31m", "\033[2m"

print_lock = threading.Lock()
cooldown = {}
stats = {"ok": 0, "blocked": 0, "error": 0, "tokens": 0}


def one(stream, sim_hour, scale, rng):
    if cooldown.get(stream.key, 0) > time.time():
        return
    model = pick(rng, stream.models)
    tin = lognormal(rng, max(50, int(stream.input_mean * scale)))
    tout = lognormal(rng, max(20, int(stream.output_mean * scale)), 0.4)
    try:
        jwt = agw.token(stream.client_id, stream.username)
    except Exception as e:  # Keycloak down or wrong secret
        with print_lock:
            print(f"{RED}token error for {stream.key}: {e}{RESET}", flush=True)
        stats["error"] += 1
        return
    prompt = rng.choice(PROMPTS[stream.project])
    status, body, secs = agw.call(jwt, model, tin, tout, prompt, user_agent=stream.user_agent)
    if status == 401:  # stale token, e.g. Keycloak restarted: get a fresh one and retry once
        agw.forget(stream.client_id, stream.username)
        jwt = agw.token(stream.client_id, stream.username)
        status, body, secs = agw.call(jwt, model, tin, tout, prompt, user_agent=stream.user_agent)
    who = f"{stream.attr_user:26}"
    bucket = stream.attr_group
    color = COLORS.get(bucket, "")
    now = datetime.now().strftime("%H:%M:%S")
    if status == 200:
        u = body.get("usage", {})
        n = u.get("input_tokens", 0) + u.get("output_tokens", 0)
        stats["ok"] += 1
        stats["tokens"] += n
        line = f"{DIM}{now} sim {sim_hour:02d}:00{RESET}  {who} {color}{bucket:15}{RESET} {model:18} {n:>7,} tok  {secs*1000:5.0f} ms"
    elif status == 429:
        stats["blocked"] += 1
        cooldown[stream.key] = time.time() + 60
        line = f"{now} sim {sim_hour:02d}:00  {who} {color}{bucket:15}{RESET} {RED}429 BLOCKED by budget (retry in {body.get('retry_after', '?')}s){RESET}"
    else:
        stats["error"] += 1
        line = f"{now} sim {sim_hour:02d}:00  {who} {bucket:15} {RED}{status} {str(body)[:120]}{RESET}"
    with print_lock:
        print(line, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rps", type=float, default=0.6, help="average requests per second (default 0.6)")
    ap.add_argument("--scale", type=float, default=0.5, help="request size relative to the backfill personas")
    ap.add_argument("--speed", type=float, default=60, help="simulated seconds per real second (60: 1 min = 1 hour)")
    ap.add_argument("--start-hour", type=int, default=9, help="simulated ET hour at start")
    ap.add_argument("--include-tom", action="store_true", help="include tom.vega in background traffic")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    streams = [s for s in P.build_streams() if args.include_tom or s.username != "tom.vega"]
    by_key = {s.key: s for s in streams}
    t0 = time.time()
    pool = ThreadPoolExecutor(max_workers=12)
    print(f"Sending ~{args.rps} req/s to {agw.GATEWAY} (mock model). Ctrl+C to stop.\n", flush=True)
    last_report = time.time()
    try:
        while True:
            sim_hour = int(args.start_hour + (time.time() - t0) * args.speed / 3600) % 24
            weights = {s.key: s.live_weight * max(0.02, hour_weight(s.hours, sim_hour)) for s in streams}
            pool.submit(one, by_key[pick(rng, weights)], sim_hour, args.scale, random.Random(rng.random()))
            # Busier in the simulated working day, quieter at night.
            busy = 0.35 + 0.65 * max(hour_weight("office", sim_hour), 0.3 * hour_weight("nightly", sim_hour))
            time.sleep(rng.expovariate(args.rps * busy))
            if time.time() - last_report > 60:
                last_report = time.time()
                with print_lock:
                    print(f"{DIM}--- {stats['ok']} ok, {stats['blocked']} blocked, {stats['error']} errors, "
                          f"{stats['tokens']:,} tokens since start ---{RESET}", flush=True)
    except KeyboardInterrupt:
        print(f"\nStopped. {stats['ok']} ok, {stats['blocked']} blocked, {stats['error']} errors, "
              f"{stats['tokens']:,} tokens.")
        pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
