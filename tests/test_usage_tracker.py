"""Tests for tero2.usage_tracker."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from tero2.usage_tracker import UsageTracker, _validate_limits


# ── _validate_limits unit tests ───────────────────────────────────────


class TestValidateLimits:
    def test_valid_dict_of_floats(self) -> None:
        data = {"anthropic": 0.5, "openai": 0.8}
        assert _validate_limits(data) == {"anthropic": 0.5, "openai": 0.8}

    def test_int_values_coerced_to_float(self) -> None:
        result = _validate_limits({"anthropic": 1, "openai": 0})
        assert result == {"anthropic": 1.0, "openai": 0.0}
        assert isinstance(result["anthropic"], float)

    def test_empty_dict_is_valid(self) -> None:
        assert _validate_limits({}) == {}

    def test_non_dict_returns_empty(self) -> None:
        assert _validate_limits([]) == {}
        assert _validate_limits("string") == {}
        assert _validate_limits(None) == {}
        assert _validate_limits(42) == {}

    def test_non_string_key_returns_empty(self) -> None:
        assert _validate_limits({1: 0.5}) == {}

    def test_bool_value_rejected(self) -> None:
        # bool is a subclass of int but should not be accepted
        assert _validate_limits({"anthropic": True}) == {}

    def test_string_value_returns_empty(self) -> None:
        assert _validate_limits({"anthropic": "0.5"}) == {}

    def test_none_value_returns_empty(self) -> None:
        assert _validate_limits({"anthropic": None}) == {}

    def test_mixed_valid_invalid_returns_empty(self) -> None:
        # any invalid value → whole dict rejected
        assert _validate_limits({"anthropic": 0.5, "openai": "bad"}) == {}


# ── fetch_limits ──────────────────────────────────────────────────────


class TestFetchLimits:
    def _make_tracker(self) -> UsageTracker:
        return UsageTracker()

    def test_returns_valid_data(self) -> None:
        payload = json.dumps({"anthropic": 0.6, "openai": 0.3})
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = payload

        tracker = self._make_tracker()
        with patch("subprocess.run", return_value=mock_result):
            result = tracker.fetch_limits()

        assert result == {"anthropic": 0.6, "openai": 0.3}

    def test_caut_not_installed_returns_empty(self) -> None:
        tracker = self._make_tracker()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = tracker.fetch_limits()
        assert result == {}

    def test_nonzero_returncode_returns_empty(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        tracker = self._make_tracker()
        with patch("subprocess.run", return_value=mock_result):
            result = tracker.fetch_limits()
        assert result == {}

    def test_invalid_json_returns_empty(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not-json{"

        tracker = self._make_tracker()
        with patch("subprocess.run", return_value=mock_result):
            result = tracker.fetch_limits()
        assert result == {}

    def test_invalid_schema_returns_empty(self) -> None:
        # valid JSON but wrong structure
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(["item1", "item2"])

        tracker = self._make_tracker()
        with patch("subprocess.run", return_value=mock_result):
            result = tracker.fetch_limits()
        assert result == {}

    def test_timeout_returns_empty(self) -> None:
        import subprocess

        tracker = self._make_tracker()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="caut", timeout=10)):
            result = tracker.fetch_limits()
        assert result == {}

    def test_values_outside_01_still_pass_schema(self) -> None:
        # schema only checks type, not range — limit bars should clamp in the UI
        payload = json.dumps({"anthropic": 1.5})
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = payload

        tracker = self._make_tracker()
        with patch("subprocess.run", return_value=mock_result):
            result = tracker.fetch_limits()
        assert result == {"anthropic": 1.5}


# ── session accumulation ──────────────────────────────────────────────


class TestSessionAccumulation:
    def _make_tracker(self) -> UsageTracker:
        return UsageTracker()

    def test_initial_summary_is_empty(self) -> None:
        tracker = self._make_tracker()
        summary = tracker.session_summary()
        assert summary["total_tokens"] == 0
        assert summary["total_cost"] == 0.0
        assert summary["providers"] == {}

    def test_single_step_recorded(self) -> None:
        tracker = self._make_tracker()
        tracker.record_step("anthropic", tokens=100, cost=0.01, is_estimated=False)
        summary = tracker.session_summary()

        assert summary["total_tokens"] == 100
        assert summary["total_cost"] == pytest.approx(0.01)
        assert "anthropic" in summary["providers"]
        p = summary["providers"]["anthropic"]
        assert p["tokens"] == 100
        assert p["cost"] == pytest.approx(0.01)
        assert p["steps"] == 1
        assert p["is_estimated"] is False

    def test_multiple_steps_same_provider(self) -> None:
        tracker = self._make_tracker()
        tracker.record_step("anthropic", tokens=100, cost=0.01, is_estimated=False)
        tracker.record_step("anthropic", tokens=200, cost=0.02, is_estimated=False)
        summary = tracker.session_summary()

        assert summary["total_tokens"] == 300
        assert summary["total_cost"] == pytest.approx(0.03)
        p = summary["providers"]["anthropic"]
        assert p["tokens"] == 300
        assert p["cost"] == pytest.approx(0.03)
        assert p["steps"] == 2

    def test_multiple_providers(self) -> None:
        tracker = self._make_tracker()
        tracker.record_step("anthropic", tokens=100, cost=0.01, is_estimated=False)
        tracker.record_step("openai", tokens=50, cost=0.005, is_estimated=True)
        summary = tracker.session_summary()

        assert summary["total_tokens"] == 150
        assert "anthropic" in summary["providers"]
        assert "openai" in summary["providers"]
        assert summary["providers"]["openai"]["is_estimated"] is True

    def test_estimated_flag_latches_to_true(self) -> None:
        tracker = self._make_tracker()
        tracker.record_step("anthropic", tokens=100, cost=0.01, is_estimated=False)
        # second step on same provider is estimated
        tracker.record_step("anthropic", tokens=50, cost=0.005, is_estimated=True)
        summary = tracker.session_summary()
        assert summary["providers"]["anthropic"]["is_estimated"] is True

    def test_estimated_flag_stays_false_when_never_estimated(self) -> None:
        tracker = self._make_tracker()
        tracker.record_step("anthropic", tokens=100, cost=0.01, is_estimated=False)
        tracker.record_step("anthropic", tokens=50, cost=0.005, is_estimated=False)
        summary = tracker.session_summary()
        assert summary["providers"]["anthropic"]["is_estimated"] is False

    def test_summary_returns_copy_of_providers(self) -> None:
        tracker = self._make_tracker()
        tracker.record_step("anthropic", tokens=100, cost=0.01, is_estimated=False)
        summary = tracker.session_summary()
        # mutating the returned dict should not affect internal state
        summary["providers"]["anthropic"]["tokens"] = 9999
        summary2 = tracker.session_summary()
        assert summary2["providers"]["anthropic"]["tokens"] == 100


# ── async refresh loop ────────────────────────────────────────────────


class TestRefreshLoop:
    async def test_refresh_loop_calls_fetch_limits(self) -> None:
        """Loop should call fetch_limits at least once after the offset."""
        tracker = UsageTracker()
        payload = json.dumps({"anthropic": 0.7})
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = payload

        call_count = 0

        def fake_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_result

        with patch("subprocess.run", side_effect=fake_run):
            with patch("random.uniform", return_value=0.0):
                # Run loop for just over one cycle but cancel before second refresh
                task = asyncio.create_task(tracker.start_refresh_loop())
                # allow at least one fetch to happen
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert call_count >= 1

    async def test_get_limits_reflects_cached_value(self) -> None:
        """get_limits() should return whatever was last fetched."""
        tracker = UsageTracker()
        payload = json.dumps({"anthropic": 0.55})
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = payload

        with patch("subprocess.run", return_value=mock_result):
            with patch("random.uniform", return_value=0.0):
                task = asyncio.create_task(tracker.start_refresh_loop())
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        limits = await tracker.get_limits()
        assert limits == {"anthropic": 0.55}

    async def test_refresh_loop_graceful_when_caut_missing(self) -> None:
        """Loop must not crash when caut is not installed."""
        tracker = UsageTracker()

        with patch("subprocess.run", side_effect=FileNotFoundError):
            with patch("random.uniform", return_value=0.0):
                task = asyncio.create_task(tracker.start_refresh_loop())
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        limits = await tracker.get_limits()
        assert limits == {}
