#!/usr/bin/env python3
"""Backfill 30 days of synthetic LLM usage into the agentgateway database.

Writes straight into the gateway's SQLite file (data/agentgateway.db):
  - request_logs: one row per synthetic request. The UI's Logs and Analytics
    pages read these, so the charts have a month of history.
  - budget_usage: the live budget counters for the current windows, computed
    from the same rows. The Budgets page and enforcement read these, so the
    gateway starts with month-to-date spend already on each counter.

Request logs alone do not move budgets. This script writes both, from one
simulation that applies the same rules as the gateway (epoch-aligned windows,
Block checked before the call, Audit only logged), so the two always agree.

The gateway must be stopped while this runs: it holds budget counters in
memory and writes them back every few seconds. ./demo.sh seed handles that.

Usage:
  python3 scripts/backfill.py [--db data/agentgateway.db] [--days 30] [--seed 26]
                              [--now 2026-10-26T16:30:00Z] [--keep-existing]
"""

import argparse
import json
import math
import os
import random
import secrets
import sqlite3
import sys
import urllib.request
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import personas as P  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY_MS = 86_400_000
MONTH_MS = 30 * DAY_MS

# Columns written, by name. The gateway owns the schema and migrates it (v2026.10.1
# added cache, reasoning and latency columns), so the seed never creates tables.
COLUMNS = ("id", "started_at", "completed_at", "duration_ms", "trace_id", "span_id", "http_status", "error",
           "gen_ai_operation_name", "gen_ai_provider_name", "gen_ai_request_model", "gen_ai_response_model",
           "input_tokens", "output_tokens", "total_tokens", "cost", "agentgateway_user", "agentgateway_group",
           "user_agent_name", "has_payload", "attributes_json",
           "cache_read_input_tokens", "cache_creation_input_tokens")


# ---------------------------------------------------------------- time shape

def hour_weight(profile, local_hour):
    """Relative activity in one local (ET) hour."""
    if profile == "office":
        # Ramp from 8am, lunch dip, taper after 6pm, a little late-evening work.
        curve = {8: 0.3, 9: 0.9, 10: 1.0, 11: 1.0, 12: 0.6, 13: 0.8, 14: 1.0, 15: 1.0,
                 16: 0.9, 17: 0.6, 18: 0.3, 19: 0.15, 20: 0.12, 21: 0.1, 22: 0.05}
        return curve.get(local_hour, 0.01)
    if profile == "nightly":
        # Batch jobs kick off at 1am ET and run for a few hours.
        return {1: 1.0, 2: 1.0, 3: 0.8, 4: 0.4}.get(local_hour, 0.0)
    # steady: a content pipeline that runs all day, a bit busier in business hours.
    return 1.0 if 7 <= local_hour <= 20 else 0.55


def day_factors(rng, streams, first_day, n_days):
    """Per-stream, per-day multipliers: weekends, days off, jitter, adoption growth."""
    factors = {}
    for s in streams:
        days_off = set()
        if s.username:
            # Everyone takes a day or two off in a month.
            days_off = set(rng.sample(range(n_days), k=rng.choice([0, 1, 1, 2])))
        for d in range(n_days):
            date = first_day + timedelta(days=d)
            f = s.weekend if date.weekday() >= 5 else 1.0
            if d in days_off and date.weekday() < 5:
                f = 0.0
            f *= math.exp(rng.gauss(0, 0.30))          # day-to-day noise
            f *= 0.80 + 0.40 * d / max(1, n_days - 1)  # adoption grows over the month
            factors[(s.key, date)] = f
    # Atlas code migrator: a migration sprint in the middle of the month.
    for d in range(n_days):
        date = first_day + timedelta(days=d)
        if n_days * 0.35 <= d <= n_days * 0.55:
            factors[("app:atlas-code-migrator", date)] *= 3.0
    return factors


def lognormal(rng, mean, sigma=0.55):
    return max(1, int(rng.lognormvariate(math.log(mean) - sigma * sigma / 2, sigma)))


def pick(rng, weights):
    r = rng.random() * sum(weights.values())
    for k, w in weights.items():
        r -= w
        if r <= 0:
            return k
    return k


# ---------------------------------------------------------------- generation

def generate(streams, start, now, rng):
    """Return a list of request dicts (unenforced), calibrated to the month pace."""
    first_day = start.astimezone(ET).date()
    n_days = (now.astimezone(ET).date() - first_day).days + 1
    factors = day_factors(rng, streams, first_day, n_days)

    now_ms = int(now.timestamp() * 1000)
    win_start_ms = now_ms - now_ms % MONTH_MS
    elapsed = (now_ms - win_start_ms) / MONTH_MS

    # Activity weight per stream per UTC hour.
    hours = []
    t = start.replace(minute=0, second=0, microsecond=0)
    while t < now:
        hours.append(t)
        t += timedelta(hours=1)
    weight = {}
    for s in streams:
        for h in hours:
            local = h.astimezone(ET)
            weight[(s.key, h)] = factors.get((s.key, local.date()), 0.0) * hour_weight(s.hours, local.hour)

    # Calibrate: each dollar budget lands at pace * elapsed share of its window.
    by_counter = defaultdict(list)
    for s in streams:
        by_counter[s.budget_key].append(s)
    rate = {}
    for (kind, name), members in by_counter.items():
        if kind == "bench":
            target = P.BENCH_PACE[name] * P.BENCH_LIMIT * elapsed
        else:
            target = P.PROJECT_PACE[name] * P.PROJECT_LIMITS[name] * elapsed
        total_share = sum(m.share for m in members)
        for m in members:
            w_in_window = sum(weight[(m.key, h)] for h in hours if h.timestamp() * 1000 >= win_start_ms) or 1.0
            rate[m.key] = target * (m.share / total_share) / w_in_window  # USD per weight unit

    reqs = []
    for s in streams:
        mean_req = P.mean_cost(s)
        for h in hours:
            expected = rate[s.key] * weight[(s.key, h)] / mean_req
            for _ in range(poisson(rng, expected)):
                ts = h + timedelta(seconds=rng.uniform(0, 3600))
                if ts >= now or ts < start:
                    continue
                reqs.append(make_request(rng, s, ts))

    # Tom is in a training session today: no bench calls yet, so his $3 daily cap
    # is untouched when the live block moment runs.
    today_ms = now_ms - now_ms % DAY_MS
    reqs = [r for r in reqs if not (r["stream"].key == "bench:tom.vega" and r["ts_ms"] >= today_ms)]

    # Exact landing: scale this window's token sizes so each counter hits its dollar target.
    for (kind, name), members in by_counter.items():
        keys = {m.key for m in members}
        cur = [r for r in reqs if r["stream"].key in keys and r["ts_ms"] >= win_start_ms]
        actual = sum(cost(r) for r in cur)
        target = (P.BENCH_PACE[name] * P.BENCH_LIMIT if kind == "bench"
                  else P.PROJECT_PACE[name] * P.PROJECT_LIMITS[name]) * elapsed
        if actual > 0:
            k = target / actual
            for r in cur:
                r["input"] = max(1, round(r["input"] * k))
                r["output"] = max(1, round(r["output"] * k))
    return reqs, win_start_ms


def cost(r):
    return P.cost_usd(r["model"], r["input"], r["output"])[0]


def poisson(rng, lam):
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(rng.gauss(lam, math.sqrt(lam)) + 0.5))
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def make_request(rng, s, ts, input_tokens=None, output_tokens=None, model=None):
    return {
        "stream": s,
        "ts": ts,
        "ts_ms": int(ts.timestamp() * 1000),
        "model": model or pick(rng, s.models),
        "input": input_tokens or lognormal(rng, s.input_mean),
        "output": output_tokens or lognormal(rng, s.output_mean, 0.45),
    }


def add_runaway(reqs, streams, start, win_start_ms, rng):
    """omar.silva's script on bench-app loops for an evening until the bench cap stops it."""
    omar = next(s for s in streams if s.key == "bench:omar.silva")
    win_start = datetime.fromtimestamp(win_start_ms / 1000, timezone.utc)
    # Prefer the previous window, so Omar is unblocked again on demo day.
    latest = win_start - timedelta(hours=8)
    earliest = start + timedelta(days=1)
    if latest <= earliest:
        latest = win_start + timedelta(days=2)
        earliest = win_start + timedelta(days=1)
    day = earliest + (latest - earliest) * 0.5
    local = day.astimezone(ET)
    begin = datetime(local.year, local.month, local.day, 18, 10, tzinfo=ET).astimezone(timezone.utc)
    if begin < earliest:
        begin += timedelta(days=1)
    t = begin
    end = begin + timedelta(hours=5)
    while t < end:
        reqs.append(make_request(rng, omar, t, lognormal(rng, 40000, 0.2), lognormal(rng, 2000, 0.2), "claude-opus-4-5"))
        t += timedelta(seconds=rng.uniform(8, 16))
    return begin


# ---------------------------------------------------------------- enforcement

def budgets_for(s):
    """Budgets matched by a stream: (name, scope, limit, window_ms, action)."""
    out = []
    if s.project is None:
        out.append(("bench-monthly", {"bench_user": s.username}, P.BENCH_LIMIT, MONTH_MS, "Block"))
        if s.username == "tom.vega":
            out.append(("tom-vega-daily", {"bench_user": "tom.vega"}, P.TOM_DAILY_LIMIT, DAY_MS, "Block"))
    else:
        out.append((f"{s.project}-monthly", {"project": s.project}, P.PROJECT_LIMITS[s.project], MONTH_MS,
                    P.PROJECT_ACTIONS[s.project]))
        if s.project == "cobalt":
            out.append(("cobalt-daily", {"project": "cobalt"}, P.COBALT_DAILY_LIMIT, DAY_MS, "Audit"))
    return out


def budget_id(name, scope):
    # Mirrors the gateway: http/budget/enterprise.rs matched_budget + mod.rs budget_id.
    scope_id = "enterprise-standalone"
    for k in sorted(scope):
        v = scope[k]
        scope_id += f":{len(k)}:{k}:{len(v)}:{v}"
    return f"api-key:{len(scope_id)}:{scope_id}:{len(name)}:{name}"


def enforce(reqs):
    """Walk requests in time order and apply Block/Audit like the gateway."""
    counters = defaultdict(float)  # (budget_id, window_start_ms) -> USD
    meta = {}
    for r in sorted(reqs, key=lambda r: r["ts_ms"]):
        matched = budgets_for(r["stream"])
        blocked = False
        for name, scope, limit, win, action in matched:
            bid = budget_id(name, scope)
            ws = r["ts_ms"] - r["ts_ms"] % win
            if action == "Block" and counters[(bid, ws)] >= limit:
                blocked = True
                break
        r["blocked"] = blocked
        if blocked:
            continue
        for name, scope, limit, win, action in matched:
            bid = budget_id(name, scope)
            ws = r["ts_ms"] - r["ts_ms"] % win
            counters[(bid, ws)] += cost(r)
            meta[bid] = win
    return counters, meta


# ---------------------------------------------------------------- rows

def ts_text(dt):
    # sqlx writes DateTime<Utc> to SQLite as RFC 3339 with an explicit +00:00 offset,
    # and the UI's time filters compare these strings, so match it exactly.
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def uuid7(ts_ms):
    rand = secrets.randbits(74)
    value = (ts_ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | (rand >> 62) << 64 | 0b10 << 62 | (rand & ((1 << 62) - 1))
    return str(uuid.UUID(int=value))


def latency_ms(rng, model, output_tokens):
    per_tok = {"claude-haiku-4-5": 6, "claude-sonnet-4-5": 14, "claude-opus-4-5": 22}[model]
    return int(350 + output_tokens * per_tok * rng.uniform(0.8, 1.25))


def to_row(rng, r, subs):
    """One request_logs row, shaped like the rows the gateway writes (checked against v2026.10.1).

    Blocked requests are never passed here: the gateway does not write 429s or 403s to
    the request log, only to its stdout log, so the seed does not either.
    """
    s = r["stream"]
    # The gateway stores the product name only ("claude-cli/2.1.30 (...)" -> "claude-cli").
    ua = s.user_agent.split("/")[0]
    total, cin, cout = P.cost_usd(r["model"], r["input"], r["output"])
    dur = latency_ms(rng, r["model"], r["output"])
    attrs = {
        "gateway": "default/default",
        "listener": "default",
        "route": "internal/llm:request",
        "endpoint": "mock-llm:8080",
        "src.addr": f"172.18.0.1:{rng.randint(32768, 60999)}",
        "http.method": "POST",
        "http.host": "localhost",
        "http.path": "/v1/messages",
        "http.version": "HTTP/1.1",
        "http.status": 200,
        "jwt.sub": subs[s.attr_user],
        "protocol": "llm",
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": r["model"],
        "gen_ai.response.model": r["model"],
        "agw.ai.original_model": r["model"],
        "gen_ai.usage.input_tokens": r["input"],
        "gen_ai.usage.cache_creation.input_tokens": 0,
        "gen_ai.usage.cache_read.input_tokens": 0,
        "gen_ai.usage.output_tokens": r["output"],
        "gen_ai.request.max_tokens": max(64, r["output"] * 2),
        "duration": f"{dur}ms",
        # The standard attributes from gateway/config.yaml. An absent claim logs as "".
        "agentgateway.app": s.app or s.client_id,
        "agentgateway.bench_user": "" if s.project else s.username,
        "agentgateway.group": s.attr_group,
        "agentgateway.project": s.project or "",
        "agentgateway.user": s.attr_user,
        "user_agent.name": ua,
        "agw.ai.usage.cost.total": total,
        "agw.ai.usage.cost.input": cin,
        "agw.ai.usage.cost.output": cout,
        "agw.ai.usage.cost.cacheRead": 0.0,
        "agw.ai.usage.cost.cacheWrite": 0.0,
        "agw.ai.usage.cost.cacheWrite1h": 0.0,
        "agw.ai.usage.cost.reasoning": 0.0,
        "agw.ai.usage.cost.inputAudio": 0.0,
        "agw.ai.usage.cost.outputAudio": 0.0,
        "agw.ai.usage.cost.pages": 0.0,
    }
    return (uuid7(r["ts_ms"]), ts_text(r["ts"]), ts_text(r["ts"] + timedelta(milliseconds=dur)), dur,
            None, None, 200, None, "chat", "anthropic", r["model"], r["model"],
            r["input"], r["output"], r["input"] + r["output"], total,
            s.attr_user, s.attr_group, ua, 0, json.dumps(attrs, separators=(",", ":")), 0, 0)


def subjects(streams):
    """Keycloak sub per user or app, so seeded rows match live ones. Falls back to stable fakes."""
    out = {}
    try:
        import agw
        for s in streams:
            if s.attr_user not in out:
                out[s.attr_user] = agw.claims(agw.token(s.client_id, s.username))["sub"]
    except Exception as e:
        print(f"(Keycloak not reachable, using stable placeholder subs: {e})")
    for s in streams:
        out.setdefault(s.attr_user, str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{s.attr_user}.si-demo")))
    return out


# ---------------------------------------------------------------- main

def gateway_running(url="http://localhost:15321/healthz/ready"):
    try:
        with urllib.request.urlopen(url, timeout=1):
            return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "agentgateway.db"))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=26, help="random seed; same seed, same history")
    ap.add_argument("--now", help="end of the history, ISO 8601 UTC (default: now)")
    ap.add_argument("--keep-existing", action="store_true",
                    help="keep rows already in the database (default: wipe request logs and budget usage)")
    ap.add_argument("--force", action="store_true", help="run even if the gateway looks up")
    args = ap.parse_args()

    if gateway_running() and not args.force:
        sys.exit("The gateway is running. Stop it first (./demo.sh seed does this), "
                 "or it will overwrite the budget counters from memory.")

    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now
           else datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = now - timedelta(days=args.days)
    rng = random.Random(args.seed)
    streams = P.build_streams()

    reqs, win_start_ms = generate(streams, start, now, rng)
    runaway_at = add_runaway(reqs, streams, start, win_start_ms, rng)
    counters, meta = enforce(reqs)

    if not os.path.exists(args.db):
        sys.exit(f"{args.db} does not exist. Start the gateway once so it creates and migrates the schema "
                 "(./demo.sh up), then seed.")
    db = sqlite3.connect(args.db)
    have = {row[1] for row in db.execute("PRAGMA table_info(request_logs)")}
    missing = set(COLUMNS) - have
    if missing or "budget_usage" not in {r[0] for r in db.execute("SELECT name FROM sqlite_master")}:
        sys.exit(f"Unexpected schema in {args.db} (missing {sorted(missing) or 'budget_usage'}). "
                 "Is this a database the gateway created?")
    if not args.keep_existing:
        db.execute("DELETE FROM request_log_payloads")
        db.execute("DELETE FROM request_logs")
        db.execute("DELETE FROM budget_usage")

    subs = subjects(streams)
    rows = [to_row(rng, r, subs) for r in sorted(reqs, key=lambda r: r["ts_ms"]) if not r["blocked"]]
    db.executemany(f"INSERT INTO request_logs ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})", rows)

    # Budget counters for the windows that are open now. Older windows are pruned
    # by the gateway at startup anyway.
    now_ms = int(now.timestamp() * 1000)
    usage = []
    for (bid, ws), used in counters.items():
        win = meta[bid]
        if ws <= now_ms < ws + win and used > 0:
            # The gateway stores USD as integer nanodollars.
            usage.append((bid, ws, ws + win, "USD", int(round(used * 1e9)), now_ms))
    db.executemany(
        "INSERT INTO budget_usage VALUES (?,?,?,?,?,?) ON CONFLICT(budget_id) DO UPDATE SET "
        "window_start=excluded.window_start, window_end=excluded.window_end, unit=excluded.unit, "
        "used_amount=excluded.used_amount, updated_at=excluded.updated_at", usage)
    db.commit()
    db.close()

    report(reqs, usage, start, now, win_start_ms, runaway_at)


def report(reqs, usage, start, now, win_start_ms, runaway_at):
    ok = [r for r in reqs if not r["blocked"]]
    blocked = [r for r in reqs if r["blocked"]]
    cost = sum(P.cost_usd(r["model"], r["input"], r["output"])[0] for r in ok)
    win_start = datetime.fromtimestamp(win_start_ms / 1000, timezone.utc)
    print(f"History {start:%Y-%m-%d %H:%M} to {now:%Y-%m-%d %H:%M} UTC: "
          f"{len(ok):,} requests logged, {len(blocked):,} more blocked by budget (not logged, as on the gateway), "
          f"{sum(r['input'] + r['output'] for r in ok):,} tokens, ${cost:,.2f}")
    print(f"Current 30d budget window opened {win_start:%Y-%m-%d} "
          f"and closes {win_start + timedelta(days=30):%Y-%m-%d} (UTC, epoch-aligned).")
    print(f"omar.silva runaway job: {runaway_at.astimezone(ET):%a %b %d %I:%M %p} ET")
    print()
    print(f"{'budget counter':44} {'spent':>11} {'limit':>9} {'%':>6}")
    limits = {"bench-monthly": P.BENCH_LIMIT, "tom-vega-daily": P.TOM_DAILY_LIMIT, "cobalt-daily": P.COBALT_DAILY_LIMIT,
              **{f"{p}-monthly": l for p, l in P.PROJECT_LIMITS.items()}}
    for bid, _, _, _, used, _ in sorted(usage, key=lambda u: u[0]):
        parts = bid.split(":")
        name, value = parts[-1], parts[-3]
        limit = limits[name]
        usd = used / 1e9
        print(f"{name + ' / ' + value:44} {'$' + format(usd, ',.2f'):>11} {'$' + format(limit, ','):>9} {100 * usd / limit:>5.0f}%")

if __name__ == "__main__":
    main()
