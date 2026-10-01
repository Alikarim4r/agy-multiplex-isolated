from __future__ import annotations

import unittest

import routing as rt


def groups(gemini: float | None = None, claude: float | None = None):
    out = []
    if gemini is not None:
        out.append({
            "id": "gemini",
            "label": "Gemini Models",
            "windows": {
                "five_hour": {"remaining_percent": gemini},
                "weekly": {"remaining_percent": gemini},
            },
        })
    if claude is not None:
        out.append({
            "id": "claude_gpt",
            "label": "Claude and GPT models",
            "windows": {
                "five_hour": {"remaining_percent": claude},
                "weekly": {"remaining_percent": claude},
            },
        })
    return out
class RoutingTests(unittest.TestCase):
    def test_same_model_moves_to_account_with_quota(self):
        matrix = {"A1": groups(claude=0), "A2": groups(claude=61)}
        route = rt.choose_route(
            accounts=["A1", "A2"], slots=1, active_pairs=set(),
            models=["claude-opus-4-6-thinking"], quota_matrix=matrix,
            exclusions=set(), blocked_models=set(), min_remaining_percent=1,
            cursor=0,
        )
        self.assertIsNotNone(route)
        self.assertEqual(route.account, "A2")
        self.assertEqual(route.model, "claude-opus-4-6-thinking")

    def test_falls_back_to_next_model_family(self):
        matrix = {
            "A1": groups(gemini=70, claude=0),
            "A2": groups(gemini=90, claude=0),
        }
        route = rt.choose_route(
            accounts=["A1", "A2"], slots=1, active_pairs=set(),
            models=["claude-opus-4-6-thinking", "gemini-3.8-flash-high"],
            quota_matrix=matrix, exclusions=set(), blocked_models=set(),
            min_remaining_percent=1, cursor=0,
        )
        self.assertIsNotNone(route)
        self.assertEqual(route.model, "gemini-3.8-flash-high")
        self.assertEqual(route.account, "A2")
    def test_exclusion_reroutes_same_model(self):
        matrix = {"A1": groups(claude=80), "A2": groups(claude=60)}
        route = rt.choose_route(
            accounts=["A1", "A2"], slots=1, active_pairs=set(),
            models=["claude-opus-4-6-thinking"], quota_matrix=matrix,
            exclusions={("A1", "claude-opus-4-6-thinking")},
            blocked_models=set(), min_remaining_percent=1, cursor=0,
        )
        self.assertIsNotNone(route)
        self.assertEqual(route.account, "A2")

    def test_unknown_quota_is_allowed_after_known_healthy(self):
        matrix = {"A1": [], "A2": groups(gemini=55)}
        route = rt.choose_route(
            accounts=["A1", "A2"], slots=1, active_pairs=set(),
            models=["gemini-3.8-flash-high"], quota_matrix=matrix,
            exclusions=set(), blocked_models=set(), min_remaining_percent=1,
            cursor=0,
        )
        self.assertIsNotNone(route)
        self.assertEqual(route.account, "A2")

    def test_failure_classification(self):
        self.assertEqual(rt.route_failure_kind("RESOURCE_EXHAUSTED code 429"), "quota_exhausted")
        self.assertEqual(rt.route_failure_kind("model not found"), "model_unavailable")
        self.assertIsNone(rt.route_failure_kind("permission denied"))

    def test_model_normalization_keeps_priority_without_duplicates(self):
        self.assertEqual(
            rt.normalize_models("claude-opus-4-6-thinking, gemini-3.8-flash-high, Claude-Opus-4-6-Thinking"),
            ["claude-opus-4-6-thinking", "gemini-3.8-flash-high"],
        )


if __name__ == "__main__":
    unittest.main()
