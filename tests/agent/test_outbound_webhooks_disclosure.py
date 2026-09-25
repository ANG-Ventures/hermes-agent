"""An outbound-webhook callback must not be named by its URL.

``WebhookTarget.label`` falls back to the URL when the optional ``name:`` is omitted, and a
webhook URL is a bearer credential (``hooks.slack.com/services/T…/B…/<secret>``, ``?token=``).
The callback's ``__name__`` is what the plugin dispatcher renders into model-facing block
directives, so it must carry a secret-free identity instead.
"""

from __future__ import annotations

from agent import outbound_webhooks

_PLANTED = "hunter2PRODSup3rSecret"


def test_unnamed_target_callback_name_does_not_carry_the_url():
    target = outbound_webhooks.WebhookTarget(
        url=f"https://hooks.slack.com/services/T000/B000/{_PLANTED}", events=["pre_tool_call"],
    )
    cb = outbound_webhooks._make_callback("pre_tool_call", target)

    assert _PLANTED not in cb.__name__
    assert _PLANTED not in cb.__qualname__
    assert cb.__name__.startswith("outbound_webhook[pre_tool_call:webhook#")
    assert target.label == target.url, "the log channel keeps the URL"


def test_named_target_keeps_its_operator_chosen_name():
    target = outbound_webhooks.WebhookTarget(
        url=f"https://example.invalid/hook?token={_PLANTED}", events=["post_tool_call"], name="audit-sink",
    )
    assert outbound_webhooks._make_callback("post_tool_call", target).__name__ == "outbound_webhook[post_tool_call:audit-sink]"


def test_distinct_unnamed_targets_stay_distinguishable():
    a = outbound_webhooks.WebhookTarget(url="https://example.invalid/a", events=["post_tool_call"])
    b = outbound_webhooks.WebhookTarget(url="https://example.invalid/b", events=["post_tool_call"])
    assert a.display_label != b.display_label
