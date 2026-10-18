"""Pure restart policy; its state is persisted atomically with run outcomes."""

import random
from datetime import timedelta

from .errors import StorageError
from .models import parse_timestamp, utc_text


class RestartPolicy:
    def __init__(self, config, random_value=random.random):
        self.config = config
        self.random_value = random_value

    @staticmethod
    def empty():
        return {"version": 1, "failures": [], "consecutive_failures": 0,
                "next_start_utc": None, "open_until_utc": None, "last_error_code": None}

    def validate(self, value):
        try:
            if not isinstance(value, dict) or set(value) != set(self.empty()) or type(value["version"]) is not int or value["version"] != 1:
                raise ValueError()
            if not isinstance(value["failures"], list) or len(value["failures"]) > 1000:
                raise ValueError()
            if type(value["consecutive_failures"]) is not int or value["consecutive_failures"] < 0:
                raise ValueError()
            for stamp in value["failures"] + [value[k] for k in ("next_start_utc", "open_until_utc") if value[k] is not None]:
                parse_timestamp(stamp)
            if value["open_until_utc"] and (not value["next_start_utc"] or parse_timestamp(value["next_start_utc"]) < parse_timestamp(value["open_until_utc"])):
                raise ValueError()
            if value["last_error_code"] is not None and not isinstance(value["last_error_code"], str):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise StorageError("Invalid persisted supervisor state; refusing to bypass retry limits") from None

    def failure(self, state, now, error):
        self.validate(state)
        config = self.config
        recent = [stamp for stamp in state["failures"] if parse_timestamp(stamp) >= now - timedelta(seconds=config.window_seconds)]
        count = state["consecutive_failures"] + 1 if recent or state["open_until_utc"] else 1
        # Saturating multiplication avoids exponent overflow after long outages.
        backoff = config.backoff_initial_seconds
        for _ in range(count - 1):
            if backoff >= config.backoff_max_seconds:
                break
            backoff = min(config.backoff_max_seconds, backoff * 2)
        backoff = min(config.backoff_max_seconds, backoff * (1 + config.jitter_ratio * self.random_value()))
        recent = (recent + [utc_text(now)])[-config.max_failures_per_window:]
        reopen = state["open_until_utc"] is not None or len(recent) >= config.max_failures_per_window
        until = now + timedelta(seconds=config.cooldown_seconds) if reopen else None
        next_start = now + timedelta(seconds=backoff)
        if until:
            next_start = max(next_start, until)
        return {"version": 1, "failures": recent, "consecutive_failures": count,
                "next_start_utc": utc_text(next_start), "open_until_utc": utc_text(until) if until else None,
                "last_error_code": error}

    def allowed(self, state, now):
        self.validate(state)
        return state["next_start_utc"] is None or now >= parse_timestamp(state["next_start_utc"])

    def describe(self, state, now):
        self.validate(state)
        if state["open_until_utc"]:
            phase = "open" if not self.allowed(state, now) else "half_open"
        else:
            phase = "closed" if self.allowed(state, now) else "backoff"
        return {"phase": phase, "consecutive_failures": state["consecutive_failures"],
                "failures_in_window": sum(parse_timestamp(s) >= now - timedelta(seconds=self.config.window_seconds) for s in state["failures"]),
                "next_start_utc": state["next_start_utc"], "open_until_utc": state["open_until_utc"],
                "last_error_code": state["last_error_code"]}
