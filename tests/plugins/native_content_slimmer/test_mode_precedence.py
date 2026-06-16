from __future__ import annotations

from itertools import product

import pytest

from plugins.native_content_slimmer.config import (
    COMPRESS_OFFLOAD,
    LOSSLESS_OFFLOAD,
    PASS_THROUGH,
    NativeContentSlimmerConfigError,
    load_slimmer_config,
    resolve_mode_precedence,
)


SLIMMER_MODES = ("off", "shadow", "active")
COMPRESSION_MODES = ("off", "shadow", "active")


@pytest.mark.parametrize(
    ("slimmer_mode", "compression_mode", "eval_passed"),
    list(product(SLIMMER_MODES, COMPRESSION_MODES, (False, True))),
)
def test_mode_precedence_table_has_exactly_one_outcome(
    slimmer_mode: str,
    compression_mode: str,
    eval_passed: bool,
) -> None:
    if slimmer_mode == "off" and compression_mode == "active":
        with pytest.raises(NativeContentSlimmerConfigError):
            resolve_mode_precedence(
                slimmer_mode=slimmer_mode,
                compression_mode=compression_mode,
                eval_passed=eval_passed,
            )
        return

    decision = resolve_mode_precedence(
        slimmer_mode=slimmer_mode,
        compression_mode=compression_mode,
        eval_passed=eval_passed,
    )

    expected = PASS_THROUGH
    if compression_mode == "active" and eval_passed:
        expected = COMPRESS_OFFLOAD
    elif slimmer_mode == "active":
        expected = LOSSLESS_OFFLOAD

    assert decision.outcome == expected
    assert sum(
        (
            decision.passes_through,
            decision.emits_lossless_marker,
            decision.emits_compressed_marker,
        )
    ) == 1
    assert not (decision.emits_lossless_marker and decision.emits_compressed_marker)


@pytest.mark.parametrize("eval_passed", (False, True))
def test_canary_mode_uses_active_precedence_without_second_writer(eval_passed: bool) -> None:
    decision = resolve_mode_precedence(
        slimmer_mode="shadow",
        compression_mode="canary",
        eval_passed=eval_passed,
    )

    assert decision.outcome == (COMPRESS_OFFLOAD if eval_passed else PASS_THROUGH)
    assert not (decision.emits_lossless_marker and decision.emits_compressed_marker)


def test_load_config_rejects_active_compression_when_offload_is_off() -> None:
    with pytest.raises(NativeContentSlimmerConfigError, match="compression_mode=active"):
        load_slimmer_config(
            {
                "plugins": {
                    "native_content_slimmer": {
                        "slimmer_mode": "off",
                        "compression_mode": "active",
                    }
                }
            }
        )


def test_load_config_accepts_compression_controls() -> None:
    cfg = load_slimmer_config(
        {
            "plugins": {
                "native_content_slimmer": {
                    "slimmer_mode": "shadow",
                    "compression_mode": "canary",
                    "compression_strategies": {
                        "json_compact": True,
                        "log_dedup": {"enabled": False},
                    },
                    "compression_lane_params": {
                        "terminal:json": {"max_items": 20, "preserve_keys": ["unhealthy"]}
                    },
                    "compression_canary_percent": 7.5,
                    "compression_breaker_ceiling": 0.25,
                }
            }
        }
    )

    assert cfg.enabled is True
    assert cfg.mode == "shadow"
    assert cfg.slimmer_mode == "shadow"
    assert cfg.compression_mode == "canary"
    assert cfg.compression_strategies == {"json_compact": True, "log_dedup": False}
    assert cfg.compression_lane_params == {
        "terminal:json": {"max_items": 20, "preserve_keys": ["unhealthy"]}
    }
    assert cfg.compression_canary_percent == 7.5
    assert cfg.compression_breaker_ceiling == 0.25
