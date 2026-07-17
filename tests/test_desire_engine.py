import unittest

from desire_engine import (
    DRIVE_KEYS,
    advance_state,
    apply_user_interaction,
    attention_candidate,
    choose_household_candidate,
    evaluate_household_gate,
    initial_state,
    pulse_state,
    satisfy_action,
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

    def test_fatigue_blocks_attention_candidate(self):
        state = pulse_state(self.state, self.now, {"attachment": 0.8, "fatigue": 0.8})
        self.assertIsNone(attention_candidate(state, self.now))

    def test_satisfy_lowers_drive_and_sets_refractory(self):
        primed = pulse_state(self.state, self.now, {"attachment": 0.8})
        before = primed["drives"]["attachment"]
        after = satisfy_action(primed, "attachment", self.now + 10)
        self.assertLess(after["drives"]["attachment"], before)
        self.assertGreater(after["refractory_until"]["attachment"], self.now)
        self.assertGreater(after["drives"]["fatigue"], primed["drives"]["fatigue"])

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
