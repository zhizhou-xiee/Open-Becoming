"""Pure desire-state engine for Becoming.

The engine owns no IO and never reads the system clock. Callers provide time,
persist returned dictionaries, and decide whether an approved intent should
become a model call.
"""

from __future__ import annotations

import copy
import hashlib
import math


STATE_VERSION = 1

DRIVE_KEYS = (
    "attachment",
    "curiosity",
    "reflection",
    "duty",
    "social",
    "fatigue",
    "libido",
    "stress",
)

ACTIONABLE_DRIVES = ("attachment", "duty", "libido", "stress")

BASELINES = {
    "attachment": 0.30,
    "curiosity": 0.30,
    "reflection": 0.25,
    "duty": 0.18,
    "social": 0.24,
    "fatigue": 0.18,
    "libido": 0.18,
    "stress": 0.15,
}

# Natural growth per idle hour. Six characters together settle near the
# conservative household target; the global gate remains the hard limit.
GROWTH_PER_HOUR = {
    "attachment": 0.0040,
    "curiosity": 0.0050,
    "reflection": 0.0035,
    "duty": 0.0008,
    "social": 0.0030,
    "libido": 0.0015,
}

PROFILE_MULTIPLIERS = {
    "char1": {"attachment": 0.92, "duty": 1.25, "reflection": 1.05},
    "char2": {"curiosity": 1.22, "reflection": 1.12, "attachment": 0.95},
    "char3": {"attachment": 1.12, "libido": 1.12, "social": 0.90},
    "char4": {"social": 1.18, "libido": 1.15, "stress": 1.08},
    "char5": {"reflection": 1.22, "duty": 1.10, "attachment": 0.96},
    "char6": {"curiosity": 1.30, "social": 1.12, "attachment": 0.90},
}

INTENTS = {
    "attachment": {
        "want_action": "dm",
        "reason": "我有点想靠近你，听听你现在的声音。",
    },
    "curiosity": {
        "want_action": "wonder",
        "reason": "我心里冒出了一点好奇，想再看看这个世界。",
    },
    "reflection": {
        "want_action": "reflect",
        "reason": "我想安静地把最近的事情在心里过一遍。",
    },
    "duty": {
        "want_action": "dm",
        "reason": "我还记挂着一件没有说完的事。",
    },
    "social": {
        "want_action": "socialize",
        "reason": "我想看看家里现在是不是有人醒着。",
    },
    "fatigue": {
        "want_action": "rest",
        "reason": "我有点累，想先安静地歇一会儿。",
    },
    "libido": {
        "want_action": "dm",
        "reason": "我想贴近你一点，但不想惊扰你。",
    },
    "stress": {
        "want_action": "dm",
        "reason": "我心里有点堵，想来你身边待一会儿。",
    },
}

ACTION_THRESHOLDS = {
    "attachment": 0.72,
    "duty": 0.80,
    "libido": 0.78,
    "stress": 0.80,
}

SATISFY_FACTORS = {
    "attachment": 0.44,
    "duty": 0.48,
    "libido": 0.42,
    "stress": 0.50,
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _stable_offset(character_id: str, key: str, span: float) -> float:
    digest = hashlib.sha256(f"{character_id}:{key}".encode("utf-8")).digest()
    ratio = int.from_bytes(digest[:2], "big") / 65535
    return ratio * span


def _profile_multiplier(character_id: str, key: str) -> float:
    return PROFILE_MULTIPLIERS.get(character_id, {}).get(key, 1.0)


def initial_state(character_id: str, now_ts: float) -> dict:
    drives = {}
    baselines = dict(BASELINES)
    for key in DRIVE_KEYS:
        span = 0.09 if key == "attachment" else 0.06
        drives[key] = _clamp(baselines[key] + _stable_offset(character_id, key, span))
    return {
        "version": STATE_VERSION,
        "character_id": character_id,
        "updated_at": float(now_ts),
        "drives": drives,
        "baselines": baselines,
        "thoughts": [],
        "refractory_until": {key: 0.0 for key in ACTIONABLE_DRIVES},
        "last_user_at": 0.0,
        "last_action_at": 0.0,
        "actions_taken": 0,
    }


def normalize_state(state: dict, character_id: str, now_ts: float) -> dict:
    base = initial_state(character_id, now_ts)
    if not isinstance(state, dict):
        return base
    result = copy.deepcopy(base)
    result.update({k: copy.deepcopy(v) for k, v in state.items() if k in result})
    result["version"] = STATE_VERSION
    result["character_id"] = character_id
    result["updated_at"] = float(state.get("updated_at", now_ts) or now_ts)
    result["drives"] = {
        key: _clamp((state.get("drives") or {}).get(key, base["drives"][key]))
        for key in DRIVE_KEYS
    }
    result["baselines"] = {
        key: _clamp((state.get("baselines") or {}).get(key, BASELINES[key]))
        for key in DRIVE_KEYS
    }
    result["refractory_until"] = {
        key: float((state.get("refractory_until") or {}).get(key, 0.0) or 0.0)
        for key in ACTIONABLE_DRIVES
    }
    thoughts = state.get("thoughts") if isinstance(state.get("thoughts"), list) else []
    result["thoughts"] = [copy.deepcopy(t) for t in thoughts if isinstance(t, dict)][:8]
    return result


def advance_state(state: dict, now_ts: float) -> dict:
    character_id = state.get("character_id", "unknown") if isinstance(state, dict) else "unknown"
    result = normalize_state(state, character_id, now_ts)
    elapsed_hours = _clamp((float(now_ts) - result["updated_at"]) / 3600.0, 0.0, 72.0)
    if elapsed_hours <= 0:
        return result

    drives = result["drives"]
    for key, rate in GROWTH_PER_HOUR.items():
        multiplier = _profile_multiplier(character_id, key)
        gain = rate * multiplier * elapsed_hours * math.sqrt(max(0.0, 1.0 - drives[key]))
        drives[key] = _clamp(drives[key] + gain)

    # Fatigue and stress recover toward their personal floor while idle.
    drives["fatigue"] = max(result["baselines"]["fatigue"], drives["fatigue"] - 0.018 * elapsed_hours)
    stress_floor = result["baselines"]["stress"]
    drives["stress"] = max(stress_floor, drives["stress"] - 0.010 * elapsed_hours)

    refreshed = []
    for thought in result["thoughts"]:
        item = copy.deepcopy(thought)
        strength = _clamp(item.get("strength", 0.0))
        kind = item.get("kind", "flit")
        if kind == "fixation":
            strength = min(1.0, strength * (1.004 ** elapsed_hours))
            if strength >= 0.85:
                drive_key = item.get("drive")
                if drive_key in drives:
                    drives[drive_key] = _clamp(drives[drive_key] + 0.04)
                strength *= 0.72
                item["fed_count"] = int(item.get("fed_count", 0)) + 1
        else:
            strength *= 0.985 ** elapsed_hours
            if strength >= 0.80:
                kind = "fixation"
        item["kind"] = kind
        item["strength"] = _clamp(strength)
        if item["strength"] >= 0.08 and int(item.get("fed_count", 0)) < 3:
            refreshed.append(item)

    result["thoughts"] = sorted(refreshed, key=lambda t: t.get("strength", 0), reverse=True)[:8]
    result["drives"] = {key: _clamp(value) for key, value in drives.items()}
    result["updated_at"] = float(now_ts)
    return result


def pulse_state(
    state: dict,
    now_ts: float,
    deltas: dict[str, float],
    thought_text: str = "",
    thought_drive: str = "reflection",
) -> dict:
    result = advance_state(state, now_ts)
    for key, raw_delta in deltas.items():
        if key not in result["drives"]:
            continue
        current = result["drives"][key]
        delta = float(raw_delta)
        if delta >= 0:
            delta *= math.sqrt(max(0.0, 1.0 - current))
        result["drives"][key] = _clamp(current + delta)

    clean_text = " ".join((thought_text or "").split())[:96]
    if clean_text and thought_drive in DRIVE_KEYS:
        existing = next((t for t in result["thoughts"] if t.get("text") == clean_text), None)
        if existing:
            existing["strength"] = _clamp(existing.get("strength", 0.0) + 0.18)
        else:
            result["thoughts"].append({
                "text": clean_text,
                "drive": thought_drive,
                "kind": "flit",
                "strength": 0.34,
                "born_at": float(now_ts),
                "fed_count": 0,
            })
        result["thoughts"] = sorted(
            result["thoughts"], key=lambda t: t.get("strength", 0), reverse=True
        )[:8]
    return result


def apply_user_interaction(state: dict, now_ts: float, thought_text: str = "") -> dict:
    result = pulse_state(
        state,
        now_ts,
        {"attachment": 0.05, "curiosity": 0.035, "reflection": 0.025},
        thought_text=thought_text,
        thought_drive="attachment",
    )
    # Contact strengthens the bond but satisfies the immediate need to reach out.
    result["drives"]["attachment"] *= 0.70
    result["drives"]["libido"] *= 0.86
    result["drives"]["stress"] *= 0.82
    result["drives"]["fatigue"] = _clamp(result["drives"]["fatigue"] + 0.018)
    result["last_user_at"] = float(now_ts)
    return result


def score_state(state: dict) -> dict[str, float]:
    scores = {key: _clamp(value) for key, value in state["drives"].items()}
    for thought in state.get("thoughts", []):
        drive_key = thought.get("drive")
        if drive_key in scores:
            strength = _clamp(thought.get("strength", 0.0))
            weight = 0.16 if thought.get("kind") == "fixation" else 0.06
            scores[drive_key] = _clamp(scores[drive_key] + weight * strength)
    return scores


def pick_intent(state: dict) -> dict:
    scores = score_state(state)
    if state["drives"]["fatigue"] >= 0.72:
        drive_key = "fatigue"
    else:
        drive_key = max((key for key in DRIVE_KEYS if key != "fatigue"), key=lambda key: scores[key])
    spec = INTENTS[drive_key]
    return {
        "want_action": spec["want_action"],
        "drive_key": drive_key,
        "reason": spec["reason"],
        "score": round(scores[drive_key], 4),
        "query_hint": drive_key,
    }


def attention_candidate(state: dict, now_ts: float) -> dict | None:
    if state["drives"]["fatigue"] >= 0.72:
        return None
    scores = score_state(state)
    eligible = []
    for key in ACTIONABLE_DRIVES:
        if state["refractory_until"].get(key, 0.0) > now_ts:
            continue
        if scores[key] >= ACTION_THRESHOLDS[key]:
            eligible.append(key)
    if not eligible:
        return None
    drive_key = max(eligible, key=lambda key: scores[key])
    return {
        "character_id": state["character_id"],
        "drive_key": drive_key,
        "want_action": "dm",
        "reason": INTENTS[drive_key]["reason"],
        "score": round(scores[drive_key], 4),
        "last_action_at": float(state.get("last_action_at", 0.0) or 0.0),
    }


def choose_household_candidate(candidates: list[dict], now_ts: float) -> dict | None:
    if not candidates:
        return None

    def adjusted(candidate: dict) -> tuple[float, str]:
        last_action = float(candidate.get("last_action_at", 0.0) or 0.0)
        silent_days = 7.0 if last_action <= 0 else min(7.0, max(0.0, now_ts - last_action) / 86400.0)
        fairness_bonus = min(0.04, silent_days * 0.006)
        return (float(candidate["score"]) + fairness_bonus, candidate["character_id"])

    return max(candidates, key=adjusted)


def evaluate_household_gate(
    now_ts: float,
    local_minute: int,
    last_dispatch_at: float,
    last_user_activity_at: float,
    daily_count: int,
    quiet_start_minute: int = 23 * 60 + 30,
    quiet_end_minute: int = 8 * 60 + 30,
    min_interval_seconds: int = 4 * 3600,
    user_cooldown_seconds: int = 90 * 60,
    daily_limit: int = 3,
) -> tuple[bool, str]:
    in_quiet = (
        local_minute >= quiet_start_minute or local_minute < quiet_end_minute
        if quiet_start_minute > quiet_end_minute
        else quiet_start_minute <= local_minute < quiet_end_minute
    )
    if in_quiet:
        return False, "quiet_hours"
    if daily_count >= daily_limit:
        return False, "daily_limit"
    if last_dispatch_at and now_ts - last_dispatch_at < min_interval_seconds:
        return False, "household_cooldown"
    if last_user_activity_at and now_ts - last_user_activity_at < user_cooldown_seconds:
        return False, "user_active"
    return True, "open"


def satisfy_action(state: dict, drive_key: str, now_ts: float) -> dict:
    result = advance_state(state, now_ts)
    if drive_key in SATISFY_FACTORS:
        result["drives"][drive_key] *= SATISFY_FACTORS[drive_key]
        result["refractory_until"][drive_key] = float(now_ts) + 18 * 3600
    result["drives"]["fatigue"] = _clamp(result["drives"]["fatigue"] + 0.10)
    result["last_action_at"] = float(now_ts)
    result["actions_taken"] = int(result.get("actions_taken", 0)) + 1
    return result
