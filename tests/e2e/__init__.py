"""Opaque-box 4-tier E2E test suite for the Remote 3ds Max Deployment System."""

from __future__ import annotations

TIER_MODULES: dict[int, str] = {
    1: "tests.e2e.test_tier1_features",
    2: "tests.e2e.test_tier2_boundaries",
    3: "tests.e2e.test_tier3_combinations",
    4: "tests.e2e.test_tier4_scenarios",
    5: "tests.e2e.test_tier5_adversarial",
}

TIER_LABELS: dict[int, str] = {
    1: "Tier 1 — Isolated Feature Tests",
    2: "Tier 2 — Boundary, Error & Security Tests",
    3: "Tier 3 — Cross-Feature Pairwise Combinations",
    4: "Tier 4 — Full End-to-End Real-World Scenarios",
    5: "Tier 5 — White-Box Adversarial Hardening Tests",
}

TIER_MINIMUMS: dict[int, int] = {
    1: 55,
    2: 50,
    3: 14,
    4: 8,
    5: 19,
}

ALL_FEATURE_IDS: tuple[str, ...] = tuple(f"F-{i:02d}" for i in range(1, 25))
