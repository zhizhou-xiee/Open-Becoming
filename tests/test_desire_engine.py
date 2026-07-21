import unittest

from desire_engine import (
    DRIVE_KEYS,
    advance_state,
    apply_user_interaction,
    attention_candidate,
    choose_household_candidate,
    evaluate_household_gate,
    initial_state,
    normalize_state,
    pulse_state,
    recover_from_sleep,
    satisfy_action,
    satisfy_passive_drive,
)


class DesireEngineTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_750_000_000.0
        self.state = initial_state("char3", self.now)

    def assert_bounded(self, state):
        self.assertEqual(set(state["drives"]), set(DRIVE_KEYS))
        for value in state["drives"].values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_initial_and_long_advance_stay_bounded(self):
        state = advance_state(self.state, self.now + 365 * 86400)
        self.assert_bounded(state)

    def test_repeated_positive_pulses_have_diminishing_gain(self):
        first = pulse_state(self.state, self.now, {"attachment": 0.2})
        second = pulse_state(first, self.now, {"attachment": 0.2})
        first_gain = first["drives"]["attachment"] - self.state["drives"]["attachment"]
        second_gain = second["drives"]["attachment"] - first["drives"]["attachment"]
        self.assertLess(second_gain, first_gain)

    def test_user_contact_satisfies_immediate_attachment_need(self):
        primed = pulse_state(self.state, self.now, {"attachment": 0.55})
        after = apply_user_interaction(primed, self.now + 60, "User发来一张照片")
        self.assertLess(after["drives"]["attachment"], primed["drives"]["attachment"])
        self.assertEqual(after["last_user_at"], self.now + 60)
        self.assertEqual(after["thoughts"][0]["text"], "User发来一张照片")

    def test_user_contact_resolves_accumulated_curiosity(self):
        primed = pulse_state(self.state, self.now, {"curiosity": 0.65})
        after = apply_user_interaction(primed, self.now + 60, "给你看一个新东西")
        self.assertLess(after["drives"]["curiosity"], primed["drives"]["curiosity"])
        self.assertGreaterEqual(
            after["drives"]["curiosity"], after["baselines"]["curiosity"]
        )

    def test_reflection_processes_toward_baseline_during_quiet_time(self):
        primed = pulse_state(self.state, self.now, {"reflection": 0.55})
        target = primed["baselines"]["reflection"] + 0.06
        after = advance_state(primed, self.now + 6 * 3600)
        self.assertAlmostEqual(
            after["drives"]["reflection"],
            target + (primed["drives"]["reflection"] - target) * 0.5,
            places=6,
        )

    def test_inward_drives_settle_at_personal_idle_levels_instead_of_one(self):
        state = pulse_state(
            self.state, self.now, {"curiosity": 0.65, "reflection": 0.65}
        )
        after = advance_state(state, self.now + 72 * 3600)
        self.assertLess(after["drives"]["curiosity"], state["drives"]["curiosity"])
        self.assertLess(after["drives"]["reflection"], state["drives"]["reflection"])
        self.assertLess(after["drives"]["curiosity"], 0.80)

    def test_private_contact_also_softly_satisfies_social_need(self):
        primed = pulse_state(self.state, self.now, {"social": 0.60})
        after = apply_user_interaction(primed, self.now + 60, "在吗")
        self.assertLess(after["drives"]["social"], primed["drives"]["social"])

    def test_passive_scene_satisfies_inward_drive_without_dropping_below_floor(self):
        for drive_key in ("curiosity", "reflection"):
            with self.subTest(drive_key=drive_key):
                primed = pulse_state(self.state, self.now, {drive_key: 0.55})
                after = satisfy_passive_drive(primed, drive_key, self.now)
                self.assertLess(after["drives"][drive_key], primed["drives"][drive_key])
                self.assertGreaterEqual(
                    after["drives"][drive_key], after["baselines"][drive_key]
                )

    def test_fatigue_blocks_attention_candidate(self):
        state = pulse_state(self.state, self.now, {"attachment": 0.8, "fatigue": 0.8})
        self.assertIsNone(attention_candidate(state, self.now))

    def test_sleep_recovers_fatigue_by_duration_without_crossing_floor(self):
        tired = pulse_state(self.state, self.now, {"fatigue": 0.82})
        nap = recover_from_sleep(tired, self.now + 30 * 60, 0.5)
        full_night = recover_from_sleep(tired, self.now + 8 * 3600, 8)

        self.assertLess(nap["drives"]["fatigue"], tired["drives"]["fatigue"])
        self.assertLess(full_night["drives"]["fatigue"], nap["drives"]["fatigue"])
        self.assertLess(full_night["drives"]["fatigue"], 0.25)
        self.assertGreaterEqual(
            full_night["drives"]["fatigue"], full_night["baselines"]["fatigue"]
        )

    def test_satisfy_lowers_drive_and_sets_refractory(self):
        primed = pulse_state(self.state, self.now, {"attachment": 0.8})
        before = primed["drives"]["attachment"]
        after = satisfy_action(primed, "attachment", self.now + 10)
        self.assertLess(after["drives"]["attachment"], before)
        self.assertGreater(after["refractory_until"]["attachment"], self.now)
        self.assertGreater(after["drives"]["fatigue"], primed["drives"]["fatigue"])

    def test_autonomous_drive_becomes_candidate_only_when_available(self):
        primed = pulse_state(self.state, self.now, {"curiosity": 0.8})
        blocked = attention_candidate(
            primed, self.now, allowed_drives={"attachment", "reflection"}
        )
        self.assertIsNone(blocked)

        candidate = attention_candidate(
            primed, self.now, allowed_drives={"curiosity"}
        )
        self.assertEqual(candidate["drive_key"], "curiosity")
        self.assertEqual(candidate["want_action"], "wonder")

    def test_old_state_gains_autonomous_refractory_slots(self):
        old = initial_state("char3", self.now)
        old["refractory_until"] = {"attachment": self.now + 10}
        normalized = normalize_state(old, "char3", self.now)
        self.assertEqual(normalized["refractory_until"]["attachment"], self.now + 10)
        for drive_key in ("curiosity", "reflection", "social"):
            self.assertEqual(normalized["refractory_until"][drive_key], 0.0)

    def test_autonomous_actions_have_independent_shorter_cooldowns(self):
        primed = pulse_state(self.state, self.now, {"curiosity": 0.8})
        before = primed["drives"]["curiosity"]
        after = satisfy_action(primed, "curiosity", self.now)
        self.assertLess(after["drives"]["curiosity"], before)
        self.assertEqual(after["refractory_until"]["curiosity"], self.now + 6 * 3600)
        self.assertEqual(after["refractory_until"]["attachment"], 0.0)

    def test_household_gate_enforces_quiet_cooldowns_and_limit(self):
        allowed, reason = evaluate_household_gate(self.now, 0, 0, 0, 0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "quiet_hours")

        allowed, reason = evaluate_household_gate(self.now, 12 * 60, self.now - 60, 0, 0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "household_cooldown")

        allowed, reason = evaluate_household_gate(self.now, 12 * 60, 0, self.now - 60, 0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "user_active")

        allowed, reason = evaluate_household_gate(self.now, 12 * 60, 0, 0, 3)
        self.assertFalse(allowed)
        self.assertEqual(reason, "daily_limit")

        allowed, reason = evaluate_household_gate(self.now, 12 * 60, 0, 0, 0)
        self.assertTrue(allowed)
        self.assertEqual(reason, "open")

    def test_household_choice_uses_score_before_small_fairness_bonus(self):
        picked = choose_household_candidate([
            {"character_id": "a", "score": 0.81, "last_action_at": self.now - 86400},
            {"character_id": "b", "score": 0.75, "last_action_at": 0},
        ], self.now)
        self.assertEqual(picked["character_id"], "a")


if __name__ == "__main__":
    unittest.main()
