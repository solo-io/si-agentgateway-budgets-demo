"""The fictional firm, shared by the backfill and the live traffic generator.

Everything here mirrors keycloak/realm-si-demo.json and gateway/config.yaml.
If you change a user, client or budget there, change it here too.

A "stream" is one identity calling the gateway through one Keycloak client:
  - a person on bench-app            -> bench budget for that person
  - a person on project-<x>-app      -> project <x> budget
  - a service app (client credentials) -> project <x> budget, tagged by app
"""

from dataclasses import dataclass, field

KEYCLOAK_REALM = "si-demo"
PASSWORD = "demo-pass"

# Budgets, as in gateway/config.yaml. All in US dollars, priced by the gateway's
# built-in model cost catalog.
BENCH_LIMIT = 100          # per person, per 30-day window
PROJECT_LIMITS = {"atlas": 2000, "beacon": 1200, "cobalt": 800}
PROJECT_ACTIONS = {"atlas": "Block", "beacon": "Block", "cobalt": "Audit"}
TOM_DAILY_LIMIT = 3
COBALT_DAILY_LIMIT = 40

# Built-in catalog prices, USD per million tokens (input, output).
MODEL_RATES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-opus-4-5": (5.0, 25.0),
}

USERS = {
    "maria.gomez": ["atlas", "beacon"],
    "diego.ramos": ["atlas"],
    "ana.lopez": ["atlas"],
    "sam.ortiz": ["beacon"],
    "priya.nair": ["beacon"],
    "luis.mora": ["cobalt"],
    "kate.reed": ["cobalt"],
    "tom.vega": [],
    "nina.cruz": [],
    "omar.silva": [],
}

SERVICE_APPS = {
    "atlas-claims-summarizer": "atlas",
    "atlas-code-migrator": "atlas",
    "beacon-test-generator": "beacon",
    "cobalt-content-agent": "cobalt",
}

HUMAN_UA = "claude-cli/2.1.30 (external, cli)"  # what Claude Code sends
SDK_UA = "anthropic-python/0.69.0"

HUMAN_MODELS = {"claude-sonnet-4-5": 0.72, "claude-haiku-4-5": 0.18, "claude-opus-4-5": 0.10}


@dataclass
class Stream:
    key: str                 # unique id
    client_id: str           # Keycloak client
    username: str | None     # None for service apps
    project: str | None      # None on the bench
    app: str | None          # service app name
    share: float             # relative weight inside its budget (bench user or project)
    hours: str               # "office", "nightly", "steady"
    weekend: float           # weekend activity as a share of a weekday
    input_mean: int          # mean input tokens per request
    output_mean: int         # mean output tokens per request
    models: dict = field(default_factory=lambda: dict(HUMAN_MODELS))
    user_agent: str = HUMAN_UA
    live_weight: float = 1.0  # share of live traffic

    @property
    def attr_user(self):
        return self.app or self.username

    @property
    def attr_group(self):
        return f"project-{self.project}" if self.project else "bench"

    @property
    def budget_key(self):
        """Which monthly counter pays: ('bench', user) or ('project', name)."""
        return ("project", self.project) if self.project else ("bench", self.username)


def build_streams():
    s = []
    # Bench: every person, own counter. share is unused for bench (one stream per counter).
    bench_profiles = {
        # username: (input_mean, output_mean, models, live_weight). Inputs are Claude Code
        # sized: the tool sends the conversation and file context with every turn.
        "maria.gomez": (20000, 1100, HUMAN_MODELS, 1.5),
        "diego.ramos": (16000, 900, HUMAN_MODELS, 0.6),
        "ana.lopez": (14000, 800, HUMAN_MODELS, 0.5),
        "sam.ortiz": (18000, 1000, HUMAN_MODELS, 0.7),
        "priya.nair": (15000, 900, HUMAN_MODELS, 0.5),
        "luis.mora": (12000, 800, HUMAN_MODELS, 0.4),
        "kate.reed": (14000, 850, HUMAN_MODELS, 0.5),
        # Tom is learning on the bench: lots of medium prompts.
        "tom.vega": (16000, 1000, HUMAN_MODELS, 0.8),
        # Nina is between projects and doing certification work on Opus.
        "nina.cruz": (24000, 1600, {"claude-opus-4-5": 0.45, "claude-sonnet-4-5": 0.45, "claude-haiku-4-5": 0.10}, 0.3),
        "omar.silva": (12000, 700, HUMAN_MODELS, 0.6),
    }
    for user, (im, om, models, lw) in bench_profiles.items():
        s.append(Stream(f"bench:{user}", "bench-app", user, None, None, 1.0, "office", 0.10,
                        im, om, dict(models), HUMAN_UA, lw))

    # Project work by people, through the project apps.
    people_on_projects = [
        ("maria.gomez", "atlas", 0.16, 24000, 1300),
        ("diego.ramos", "atlas", 0.12, 20000, 1100),
        ("ana.lopez", "atlas", 0.10, 18000, 1000),
        ("maria.gomez", "beacon", 0.18, 22000, 1400),
        ("sam.ortiz", "beacon", 0.20, 20000, 1200),
        ("priya.nair", "beacon", 0.14, 17000, 1000),
        ("luis.mora", "cobalt", 0.22, 15000, 1200),
        ("kate.reed", "cobalt", 0.20, 15000, 1300),
    ]
    for user, proj, share, im, om in people_on_projects:
        s.append(Stream(f"{proj}:{user}", f"project-{proj}-app", user, proj, None, share, "office", 0.06,
                        im, om, dict(HUMAN_MODELS), HUMAN_UA, 1.5 if user == "maria.gomez" else 1.0))

    # Service apps: the agents the firm builds for clients.
    s.append(Stream("app:atlas-claims-summarizer", "atlas-claims-summarizer", None, "atlas", "atlas-claims-summarizer",
                    0.40, "office", 0.25, 30000, 900, {"claude-haiku-4-5": 0.85, "claude-sonnet-4-5": 0.15}, SDK_UA, 2.0))
    s.append(Stream("app:atlas-code-migrator", "atlas-code-migrator", None, "atlas", "atlas-code-migrator",
                    0.22, "nightly", 0.0, 30000, 8000, {"claude-opus-4-5": 0.6, "claude-sonnet-4-5": 0.4}, SDK_UA, 0.5))
    s.append(Stream("app:beacon-test-generator", "beacon-test-generator", None, "beacon", "beacon-test-generator",
                    0.48, "nightly", 0.5, 8000, 6000, {"claude-sonnet-4-5": 0.8, "claude-haiku-4-5": 0.2}, SDK_UA, 1.0))
    s.append(Stream("app:cobalt-content-agent", "cobalt-content-agent", None, "cobalt", "cobalt-content-agent",
                    0.58, "steady", 0.7, 6000, 2000, {"claude-sonnet-4-5": 0.7, "claude-haiku-4-5": 0.3}, SDK_UA, 1.5))
    return s


# Month pace: the share of each dollar budget the counter would reach by the end
# of its 30-day window at the seeded rate. The backfill scales usage so the
# counter sits at pace * (elapsed share of the window) when the seed runs.
BENCH_PACE = {
    "maria.gomez": 0.40,
    "diego.ramos": 0.20,
    "ana.lopez": 0.15,
    "sam.ortiz": 0.30,
    "priya.nair": 0.18,
    "luis.mora": 0.12,
    "kate.reed": 0.16,
    "tom.vega": 0.70,
    "nina.cruz": 0.97,   # Nina will run out before the window ends.
    "omar.silva": 0.35,
}
PROJECT_PACE = {
    "atlas": 0.92,
    "beacon": 1.05,      # Beacon is on pace to hit its cap.
    "cobalt": 1.35,      # Cobalt is Audit, so it runs over and is logged, not blocked.
}


def mean_cost(stream):
    """Expected USD per request for a stream, at its mean sizes and model mix."""
    total = sum(stream.models.values())
    return sum(w / total * cost_usd(m, stream.input_mean, stream.output_mean)[0] for m, w in stream.models.items())


def cost_usd(model, input_tokens, output_tokens):
    rin, rout = MODEL_RATES[model]
    return (input_tokens * rin + output_tokens * rout) / 1_000_000, input_tokens * rin / 1_000_000, output_tokens * rout / 1_000_000
