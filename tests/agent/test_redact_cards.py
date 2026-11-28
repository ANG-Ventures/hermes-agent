"""Payment card masking (t_90c8b36c): tool results, inbound user text, browser_vision screenshots.

Every card number here is MADE UP: Luhn-valid with a real network prefix, last fours 0366 / 2226 / 2347,
none shared with a real card.
"""
from __future__ import annotations

import json
import shlex
from unittest.mock import patch

import pytest

from agent import redact_cards
from agent.redact_cards import (
    CARD_IMAGE_REFUSAL,
    count_card_numbers,
    luhn,
    mask_card_numbers,
)
from agent.tool_dispatch_helpers import make_tool_result_message

VISA = "4532015112830366"
MC = "5555444433332226"
AMEX = "341256789012347"


def _cfg(**security):
    return patch("hermes_cli.config.load_config_readonly", return_value={"security": security})


def test_fixture_numbers_are_luhn_valid_and_not_samples():
    for n in (VISA, MC, AMEX):
        assert luhn(n) and n not in redact_cards.PLACEHOLDERS
        assert not n.endswith("1120")


@pytest.mark.parametrize("raw, masked", [
    (f"{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}", "XXXX XXXX XXXX 0366"),
    (f"{VISA[:4]}-{VISA[4:8]}-{VISA[8:12]}-{VISA[12:]}", "XXXX-XXXX-XXXX-0366"),
    (VISA, "XXXXXXXXXXXX0366"),
    (MC, "XXXXXXXXXXXX2226"),
    (f"{AMEX[:4]} {AMEX[4:10]} {AMEX[10:]}", "XXXX XXXXXX X2347"),
    (AMEX, "XXXXXXXXXXX2347"),
])
def test_card_groupings_mask_to_last_four(raw, masked):
    assert mask_card_numbers(f"card: {raw}.") == f"card: {masked}."


@pytest.mark.parametrize("text", [
    "order 112-1234567-1234567 shipped",                      # Amazon 3-7-7 order number
    "order 112 1234567 1234567 shipped",
    "ticket 4532015112830367",                                # 16 digits, Luhn fails
    "ticket 4532 0151 1283 0367",
    "sha 9f2c4532015112830366ab",                             # hex hash with a Luhn-valid digit run inside
    "deadbeef4532015112830366 and 4532015112830366cafe",
    "commit 4532015112830366a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "ts=1795828039000000",                                    # microsecond epoch: no network prefix
    "4111 1111 1111 1111 cvc 123",                            # published sample number
    "card 4242424242424242 exp 12/30",
    "id 45320151128303661",                                   # 17 digits: not a card length
])
def test_false_positives_pass_untouched(text):
    assert mask_card_numbers(text) == text


def test_cvc_and_expiry_masked_on_same_and_next_two_lines():
    text = f"card {VISA} cvc: 417\nExpiry: 08/29\nCVV2 = 417\nexp 09/30\nzip 93401"
    assert mask_card_numbers(text) == (
        "card XXXXXXXXXXXX0366 cvc: XXX\nExpiry: XX/XX\nCVV2 = XXX\nexp 09/30\nzip 93401")


def test_cvc_needs_its_label_and_a_masked_number():
    assert mask_card_numbers(f"card {VISA}\nqty 417") == "card XXXXXXXXXXXX0366\nqty 417"
    assert mask_card_numbers("cvc: 417 exp 08/29") == "cvc: 417 exp 08/29"
    assert mask_card_numbers("ticket 4532015112830367 cvc 417") == "ticket 4532015112830367 cvc 417"


def test_json_encoded_form_fill_output():
    """Route 1 shape: terminal output inside a JSON tool result, lines joined by the \\n escape."""
    out = json.dumps({"output": f"filled #cardNumber={VISA}\ncardCvc=417\ncardExpiry=0829\nok", "exit_code": 0})
    masked = mask_card_numbers(out)
    assert VISA not in masked and "417" not in masked and "0829" not in masked
    assert json.loads(masked)["output"] == "filled #cardNumber=XXXXXXXXXXXX0366\ncardCvc=XXX\ncardExpiry=XXXX\nok"


def test_count_card_numbers_matches_the_hook_gate():
    assert count_card_numbers(f"{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}") == 1
    assert count_card_numbers("4111 1111 1111 1111") == 0
    assert count_card_numbers("112-1234567-1234567") == 0
    assert count_card_numbers("****  ****  ****  0366") == 0


# ── route 1: terminal tool result ─────────────────────────────────────────────

def test_terminal_result_is_masked_in_the_transcript_message():
    content = json.dumps({"output": f"Card number: {VISA}\nCVC: 417", "exit_code": 0})
    with _cfg():
        msg = make_tool_result_message("terminal", content, "call_1")
    assert VISA not in msg["content"] and "XXXXXXXXXXXX0366" in msg["content"]
    assert "CVC: XXX" in msg["content"]


def test_terminal_result_untouched_when_knob_off():
    content = json.dumps({"output": f"Card number: {VISA}"})
    with _cfg(mask_card_numbers_tool_results=False):
        msg = make_tool_result_message("terminal", content, "call_1")
    assert VISA in msg["content"]


def test_result_without_card_is_byte_identical():
    content = json.dumps({"output": "order 112-1234567-1234567\nsha 4532015112830366ab"})
    with _cfg():
        assert make_tool_result_message("terminal", content, "call_1")["content"] == content


# ── route 2: image-read result (model wrote the number out of a screenshot) ───

def test_image_read_result_is_masked():
    content = json.dumps({"success": True, "analysis": f"The card reads {MC}, exp 11/28, CVC 913."})
    with _cfg():
        msg = make_tool_result_message("vision_analyze", content, "call_2")
    assert MC not in msg["content"] and "XXXXXXXXXXXX2226" in msg["content"]
    assert "exp XX/XX" in msg["content"] and "CVC XXX" in msg["content"]


def test_multimodal_text_parts_and_summary_are_masked():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    content = [{"type": "text", "text": f"Amex {AMEX}"}, image]
    with _cfg():
        msg = make_tool_result_message("browser_vision", content, "call_3",
                                       multimodal_text_summary=f"summary {AMEX}")
    flat = json.dumps(msg)
    assert AMEX not in flat and "XXXXXXXXXXX2347" in flat
    assert msg["content"] == [{"type": "text", "text": "Amex XXXXXXXXXXX2347"}, image]
    assert msg["_multimodal_text_summary"] == "summary XXXXXXXXXXX2347"


# ── route 3: inbound user message (separate switch, default off) ──────────────

def _turn(user_message, **cfg):
    from tests.agent.test_turn_context import _FakeAgent, _build
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        if cfg:
            with _cfg(**cfg):
                return _build(_FakeAgent(), user_message=user_message)
        return _build(_FakeAgent(), user_message=user_message)


def test_user_message_unmasked_by_default():
    assert redact_cards._KNOB_DEFAULTS["mask_card_numbers_user_messages"] is False
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["security"]["mask_card_numbers_user_messages"] is False
    ctx = _turn(f"my cards: {VISA} and {MC}")
    assert ctx.messages[-1]["content"] == f"my cards: {VISA} and {MC}"


def test_user_message_masked_when_switched_on():
    ctx = _turn(f"my cards: {VISA} and {MC}", mask_card_numbers_user_messages=True)
    assert ctx.messages[-1]["content"] == "my cards: XXXXXXXXXXXX0366 and XXXXXXXXXXXX2226"


def test_config_defaults_carry_all_three_knobs():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    sec = DEFAULT_CONFIG["security"]
    assert sec["mask_card_numbers_tool_results"] is True
    assert sec["browser_vision_card_ocr"] is True
    for name, default in redact_cards._KNOB_DEFAULTS.items():
        assert sec[name] is default


# ── browser_vision screenshot OCR ─────────────────────────────────────────────

def test_screenshot_refusal_uses_the_hook_gate_wording(tmp_path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"png")
    with _cfg(), patch.object(redact_cards, "_ocr", return_value=(f"Card\n{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}", None)):
        assert redact_cards.screenshot_card_refusal(shot) == CARD_IMAGE_REFUSAL
    with _cfg(), patch.object(redact_cards, "_ocr", return_value=("Order 112-1234567-1234567", None)):
        assert redact_cards.screenshot_card_refusal(shot) is None


def test_screenshot_ocr_fails_open_and_obeys_its_knob(tmp_path, caplog):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"png")
    with _cfg(), patch.object(redact_cards, "_ocr", return_value=(None, "tesseract not installed")):
        assert redact_cards.screenshot_card_refusal(shot) is None
    assert "UNCHECKED" in caplog.text
    with _cfg(browser_vision_card_ocr=False), patch.object(redact_cards, "_ocr") as ocr:
        assert redact_cards.screenshot_card_refusal(shot) is None
    ocr.assert_not_called()


def test_screenshot_ocr_runs_the_real_subprocess_path(tmp_path):
    """_ocr through a stand-in tesseract that echoes its stdin: a card in the 'image' refuses."""
    fake = tmp_path / "tesseract"
    fake.write_text("#!/bin/sh\ncat\n")
    fake.chmod(0o755)
    shot = tmp_path / "s.png"
    shot.write_text(f"Card number\n{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}\n")
    with _cfg(), patch.object(redact_cards, "TESSERACT", (str(fake),)):
        assert redact_cards.screenshot_card_refusal(shot) == CARD_IMAGE_REFUSAL
        shot.write_text("Order 112-1234567-1234567\n")
        assert redact_cards.screenshot_card_refusal(shot) is None


def _browser_vision(tmp_path, ocr_text):
    from tools.browser_tool import browser_vision
    shots_dir = tmp_path / "browser_screenshots"
    shots_dir.mkdir()
    shot = shots_dir / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    with (
        patch("hermes_constants.get_hermes_dir", return_value=shots_dir),
        patch("tools.browser_tool_lifecycle._cleanup_old_screenshots"),
        patch("tools.browser_tool_session._run_browser_command",
              return_value={"success": True, "data": {"path": str(shot)}}),
        patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=False),
        patch.object(redact_cards, "_ocr", return_value=(ocr_text, None)),
        patch("tools.browser_tool_vision._analyze_screenshot_with_aux_llm", return_value="a checkout page") as aux,
    ):
        result = json.loads(browser_vision("what is on the page?", task_id="test"))
    return result, aux


def test_browser_vision_refuses_a_screenshot_with_a_card_before_any_model(tmp_path):
    result, aux = _browser_vision(tmp_path, f"Pay with\n{MC[:4]} {MC[4:8]} {MC[8:12]} {MC[12:]}\n08/29")
    aux.assert_not_called()
    assert result.get("success") is not True
    assert CARD_IMAGE_REFUSAL in json.dumps(result)
    assert MC not in json.dumps(result)


def test_browser_vision_without_a_card_reaches_the_model(tmp_path):
    result, aux = _browser_vision(tmp_path, "Order 112-1234567-1234567 total $41.00")
    aux.assert_called_once()
    assert result["success"] is True and result["analysis"] == "a checkout page"


# ── Prism round 1 (ee01291296) ───────────────────────────────────────────────

def test_json_escaped_separators_are_recognised_and_left_intact():
    nb = "\u2011"
    raw = json.dumps({"output": f"{VISA[:4]}{nb}{VISA[4:8]}{nb}{VISA[8:12]}{nb}{VISA[12:]}"})
    assert "\\u2011" in raw
    assert json.loads(mask_card_numbers(raw)) == {"output": f"XXXX{nb}XXXX{nb}XXXX{nb}0366"}
    tabbed = json.dumps({"output": "\t".join((VISA[:4], VISA[4:8], VISA[8:12], VISA[12:]))})
    assert json.loads(mask_card_numbers(tabbed)) == {"output": "XXXX\tXXXX\tXXXX\t0366"}


def test_masked_bare_json_numbers_stay_valid_json():
    out = mask_card_numbers('{"cardNumber":%s,"cvv":417, "exp": 1128}' % VISA)
    assert json.loads(out) == {"cardNumber": "XXXXXXXXXXXX0366", "cvv": "XXX", "exp": "XXXX"}
    quoted = mask_card_numbers('{"cardNumber":"%s","cvv":"417"}' % VISA)
    assert json.loads(quoted) == {"cardNumber": "XXXXXXXXXXXX0366", "cvv": "XXX"}


def test_bare_number_inside_a_json_encoded_string_keeps_both_levels_valid():
    inner = '{"cardNumber":%s}' % VISA
    out = mask_card_numbers(json.dumps({"output": inner}))
    assert json.loads(json.loads(out)["output"]) == {"cardNumber": "XXXXXXXXXXXX0366"}
    twice = mask_card_numbers(json.dumps({"o": json.dumps({"x": inner})}))
    assert json.loads(json.loads(json.loads(twice)["o"])["x"]) == {"cardNumber": "XXXXXXXXXXXX0366"}


def test_session_title_input_is_masked_with_the_user_message_switch():
    from tests.agent.test_turn_context import _FakeAgent, _build
    seen = []
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None), \
            patch("agent.turn_context._maybe_title_session_at_turn_start",
                  lambda agent, messages, title=None: seen.append(title)):
        with _cfg(mask_card_numbers_user_messages=True):
            _build(_FakeAgent(), user_message=f"card {VISA}", title_user_message=f"card {VISA}")
        with _cfg():
            _build(_FakeAgent(), user_message=f"card {VISA}", title_user_message=f"card {VISA}")
    assert seen == ["card XXXXXXXXXXXX0366", f"card {VISA}"]


def test_numbers_sharing_a_group_are_both_masked():
    a, b = "4000 0000 0004 4000", "4000 0000 0000 0366"  # both Luhn-valid; a's last group is b's first
    assert luhn(a.replace(" ", "")) and luhn(b.replace(" ", ""))
    out = mask_card_numbers(f"cards {a} 0000 0000 0366 end")
    assert out == "cards XXXX XXXX XXXX XXXX XXXX XXXX 0366 end"


def test_full_width_digits_are_masked():
    fw = VISA.translate({ord(c): ord(c) + 0xFEE0 for c in "0123456789"})
    assert mask_card_numbers(f"card {fw}") == "card " + "X" * 12 + fw[-4:]
    amex_fw = AMEX.translate({ord(c): ord(c) + 0xFEE0 for c in "0123456789"})
    assert mask_card_numbers(f"card {amex_fw}") == "card " + "X" * 11 + amex_fw[-4:]


@pytest.mark.parametrize("tool", ["read_file", "search_files"])
def test_file_reads_stay_byte_identical_to_disk(tool):
    content = json.dumps({"content": 'card = "4000 0000 0000 0002"\nother = "%s"' % VISA})
    with _cfg():
        assert make_tool_result_message(tool, content, "call_9")["content"] == content


def test_submit_time_user_row_is_masked_when_switched_on(tmp_path):
    from tui_gateway import session_workdir

    written = []

    class _DB:
        def append_message(self, target, role, content=None, **kw):
            written.append(content)
            return 1

    from contextlib import contextmanager

    @contextmanager
    def _db(_session):
        yield _DB()

    with patch.object(session_workdir, "_session_db", _db), \
            patch.object(session_workdir, "_submit_row_target_key", lambda s: "k"):
        with _cfg(mask_card_numbers_user_messages=True):
            staged = session_workdir._write_submit_user_row({"session_key": "k"}, f"card {VISA}", None)
        with _cfg():
            session_workdir._write_submit_user_row({"session_key": "k"}, f"card {VISA}", None)
    assert written == ["card XXXXXXXXXXXX0366", f"card {VISA}"]
    assert staged["content"] == "card XXXXXXXXXXXX0366"


def test_key_ending_in_a_backslash_keeps_json_valid():
    out = mask_card_numbers(json.dumps({"C:\\": int(VISA)}))
    assert json.loads(out) == {"C:\\": "XXXXXXXXXXXX0366"}
    nested = mask_card_numbers(json.dumps({"o": json.dumps({"C:\\": int(VISA)})}))
    assert json.loads(json.loads(nested)["o"]) == {"C:\\": "XXXXXXXXXXXX0366"}


def test_terminal_spill_file_is_masked_on_disk(tmp_path):
    from tools.terminal_tool_result import _redact_spill_file
    spill = tmp_path / "spill.txt"
    spill.write_text(f"card {VISA}\n", encoding="utf-8")
    with _cfg():
        assert _redact_spill_file(str(spill), 100, "echo") != []
    assert spill.read_text(encoding="utf-8") == "card XXXXXXXXXXXX0366\n"


def test_executor_masks_a_result_before_it_spills():
    from types import SimpleNamespace
    from tests.agent.test_malformed_tool_arguments import _make_agent
    agent = _make_agent()
    call = SimpleNamespace(id="call-1", type="function",
                           function=SimpleNamespace(name="web_search", arguments='{"query": "q"}'))
    spilled = []

    def persist(**kw):
        spilled.append(kw["content"])
        return kw["content"]

    with _cfg(), patch.object(agent, "_invoke_tool", return_value=f"card {VISA}"), \
            patch("model_tools.handle_function_call", return_value=f"card {VISA}"), \
            patch("agent.tool_executor.maybe_persist_tool_result", side_effect=persist):
        agent._execute_tool_calls_sequential(SimpleNamespace(content="", tool_calls=[call]), [], "task-1")
    assert spilled == ["card XXXXXXXXXXXX0366"]


# ── vision_analyze: OCR the bytes it is about to send (t_56a9d083) ────────────

def _png_bytes() -> bytes:
    import struct
    import zlib
    w, h = 8, 4
    rows = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _data_url() -> str:
    import base64
    return "data:image/png;base64," + base64.b64encode(_png_bytes()).decode()


class _RemoteEnv:
    """A worker's terminal backend on another host: the file exists only there."""
    def __init__(self):
        self.commands = []

    def execute(self, cmd):
        import base64
        self.commands.append(cmd)
        return {"returncode": 0, "output": base64.b64encode(_png_bytes()).decode()}


def _vision_analyze(tmp_path, image_url, ocr, *, native=False, env=None, **security):
    """Run the vision_analyze handler with the model calls stubbed; ``ocr`` is _ocr's return value."""
    import asyncio
    from unittest.mock import AsyncMock
    from tools import vision_tools
    patches = [
        _cfg(**security),
        patch("tools.vision_tools.get_hermes_dir", return_value=tmp_path),
        patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=native),
        patch("tools.vision_tools._validate_raster_image_decodable", return_value=None),
        patch("tools.vision_tools.async_call_llm", new=AsyncMock(return_value=object())),
        patch("tools.vision_tools.extract_content_or_reasoning", return_value="a receipt"),
        patch("tools.vision_tools._load_auxiliary_client"),
        patch.object(redact_cards, "_ocr", return_value=ocr) if ocr is not None else patch.object(redact_cards, "luhn", redact_cards.luhn),
    ]
    if env is not None:
        patches += [patch("tools.image_source._is_local_terminal_backend", return_value=False),
                    patch("tools.image_source._ensure_container_env"),
                    patch("tools.image_source._get_active_env", return_value=env)]
    from contextlib import ExitStack
    with ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]
        out = asyncio.run(vision_tools._handle_vision_analyze(
            {"image_url": image_url, "question": "what does it say?"}, task_id="t"))
        llm = mocks[4]
    return out, llm


_CARD_OCR = (f"Card number\n{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}\nExp 08/29", None)


def test_vision_analyze_refuses_a_data_url_image_with_a_card_before_any_model(tmp_path):
    out, llm = _vision_analyze(tmp_path, _data_url(), _CARD_OCR)
    llm.assert_not_called()
    result = json.loads(out)
    assert result["success"] is False and result["error"] == CARD_IMAGE_REFUSAL
    assert VISA not in out


def test_vision_analyze_refuses_a_remote_terminal_host_image_with_a_card(tmp_path):
    """The path exists only on the worker's terminal host; the bytes come back through env.execute."""
    env = _RemoteEnv()
    for native in (False, True):
        out, llm = _vision_analyze(tmp_path, "/srv/ci/scratch/t_x/shot.png", _CARD_OCR, native=native, env=env)
        llm.assert_not_called()
        assert CARD_IMAGE_REFUSAL in out and VISA not in out
        assert '"_multimodal"' not in out
    assert env.commands and "/srv/ci/scratch/t_x/shot.png" in env.commands[0]


@pytest.mark.parametrize("text", ["Card ending XXXX XXXX XXXX 0366", "Order 112-1234567-1234567 total $41.00"])
def test_vision_analyze_passes_masked_numbers_and_order_numbers(tmp_path, text):
    out, llm = _vision_analyze(tmp_path, _data_url(), (text, None))
    llm.assert_called()
    assert json.loads(out) == {"success": True, "analysis": "a receipt"}
    out, _ = _vision_analyze(tmp_path, _data_url(), (text, None), native=True)
    assert isinstance(out, dict) and out["_multimodal"] is True


def test_vision_analyze_ocr_missing_allows_with_one_warning(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="agent.redact_cards"):
        out, llm = _vision_analyze(tmp_path, _data_url(), (None, "tesseract not installed"))
    llm.assert_called()
    assert json.loads(out)["success"] is True
    lines = [r.getMessage() for r in caplog.records if "UNCHECKED" in r.getMessage()]
    assert len(lines) == 1 and "image read sent UNCHECKED" in lines[0] and "tesseract not installed" in lines[0]


def test_vision_analyze_card_ocr_obeys_its_own_knob(tmp_path):
    with patch.object(redact_cards, "_ocr") as ocr:
        out, llm = _vision_analyze(tmp_path, _data_url(), None, image_read_card_ocr=False)
    ocr.assert_not_called()
    llm.assert_called()
    # browser_vision's knob does not turn this one off.
    out, llm = _vision_analyze(tmp_path, _data_url(), _CARD_OCR, browser_vision_card_ocr=False)
    assert CARD_IMAGE_REFUSAL in out


def test_vision_analyze_ocr_runs_the_real_subprocess_on_the_sent_bytes(tmp_path):
    """A stand-in tesseract records the bytes it was given and prints a card: the image is refused, and
    the bytes OCRed are exactly the PNG the tool would have sent."""
    seen = tmp_path / "seen.bin"
    fake = tmp_path / "tesseract"
    fake.write_text(f"#!/bin/sh\ncat > {shlex.quote(str(seen))}\nprintf 'Card\\n{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}\\n'\n")
    fake.chmod(0o755)
    with patch.object(redact_cards, "TESSERACT", (str(fake),)):
        out, llm = _vision_analyze(tmp_path, _data_url(), None)
    llm.assert_not_called()
    assert CARD_IMAGE_REFUSAL in out
    assert seen.read_bytes() == _png_bytes()


def test_vision_analyze_real_ocr_on_a_rendered_card_image(tmp_path):
    """Real tesseract on a rendered image of a made-up Luhn-valid number (skipped where OCR is absent)."""
    import os
    pil = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    if not any(os.access(b, os.X_OK) for b in redact_cards.TESSERACT):
        pytest.skip("tesseract not installed")
    import base64
    import io
    im = pil.new("RGB", (900, 160), "white")
    try:
        font = ImageFont.load_default(size=48)
    except TypeError:
        pytest.skip("Pillow too old for a sized default font")
    ImageDraw.Draw(im).text((20, 50), f"{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}", fill="black", font=font)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    out, llm = _vision_analyze(tmp_path, url, None)
    llm.assert_not_called()
    assert CARD_IMAGE_REFUSAL in out


def _rgba_card_url(alpha: int, size=(1800, 200)):
    """A PNG whose RGB shows the card; ``alpha`` 0 hides it from a white-composited view (Prism 77de86bbc303)."""
    import base64
    import io
    pil = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    try:
        font = ImageFont.load_default(size=60)
    except TypeError:
        pytest.skip("Pillow too old for a sized default font")
    im = pil.new("RGB", size, "white")
    ImageDraw.Draw(im).text((40, 60), f"{VISA[:4]}   {VISA[4:8]}   {VISA[8:12]}   {VISA[12:]}", fill="black", font=font)
    im = im.convert("RGBA")
    im.putalpha(alpha)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_vision_analyze_ocrs_the_alpha_dropped_view_of_a_transparent_image(tmp_path):
    """The JPEG resize drops alpha (convert("RGB")): OCR clean on the composited view, card under the alpha."""
    seen = []

    def ocr(path):
        seen.append(path)
        return ("", None) if len(seen) % 2 else _CARD_OCR
    for native in (False, True):
        with patch.object(redact_cards, "_ocr", side_effect=ocr):
            out, llm = _vision_analyze(tmp_path, _rgba_card_url(0), None, native=native)
        llm.assert_not_called()
        assert CARD_IMAGE_REFUSAL in out and '"_multimodal"' not in out
    assert len(seen) == 4 and seen[1].endswith(".rgb.png") and seen[3].endswith(".rgb.png")
    assert not list(tmp_path.glob("*.rgb.png"))


def test_vision_analyze_opaque_image_is_ocred_once(tmp_path):
    with patch.object(redact_cards, "_ocr", return_value=("receipt", None)) as ocr:
        out, llm = _vision_analyze(tmp_path, _rgba_card_url(255, (400, 100)), None, native=True)
    assert ocr.call_count == 1 and out["_multimodal"] is True


def test_vision_analyze_real_ocr_refuses_a_card_hidden_under_transparency(tmp_path):
    """Prism repro with real tesseract: 1800x200 RGBA, alpha 0, native path (it resizes to JPEG)."""
    import os
    if not any(os.access(b, os.X_OK) for b in redact_cards.TESSERACT):
        pytest.skip("tesseract not installed")
    out, llm = _vision_analyze(tmp_path, _rgba_card_url(0), None, native=True)
    assert CARD_IMAGE_REFUSAL in out and '"_multimodal"' not in out


def _trns_card_png(path):
    """RGB PNG: black made-up card digits on white, with black marked transparent (a tRNS colour key)."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (1800, 200), "white")
    try:
        font = ImageFont.load_default(size=60)
    except TypeError:
        pytest.skip("Pillow too old for a sized default font")
    ImageDraw.Draw(im).text((40, 60), f"{VISA[:4]}   {VISA[4:8]}   {VISA[8:12]}   {VISA[12:]}", fill="black", font=font)
    im.save(path, format="PNG", transparency=(0, 0, 0))


def test_alpha_dropped_copy_drops_a_trns_colour_key(tmp_path):
    pytest.importorskip("PIL.Image")
    from PIL import Image
    from tools.vision_tools import _alpha_dropped_copy
    src = tmp_path / "trns.png"
    _trns_card_png(src)
    out = _alpha_dropped_copy(src)
    assert out is not None
    with Image.open(out) as im:
        assert im.mode == "RGB" and "transparency" not in im.info



# ── t_c497904d: linear time, JSON arrays/root, flattened copy lifetime ───────

def _old_window(text, start, end):
    """``_window`` as it was before t_c497904d (rescans from the start of the text per card)."""
    line_start = 0
    for m in redact_cards._LINE_BREAK.finditer(text, 0, start):
        line_start = m.end()
    stop, breaks = len(text), 0
    for m in redact_cards._LINE_BREAK.finditer(text, end):
        breaks += 1
        if breaks == 3:
            stop = m.start()
            break
    return line_start, stop


def _card_rows(n, seed=7):
    """n rows, each with a made-up Luhn-valid Visa number: plain table rows (CRLF) and JSON-escaped rows."""
    import random
    rng = random.Random(seed)

    def card():
        body = "4" + "".join(rng.choice("0123456789") for _ in range(14))
        return next(body + d for d in "0123456789" if luhn(body + d))
    return "".join(
        "order %06d  customer name here  card %s  cvv %03d  exp 08/29  ok\r\n" % (i, card(), rng.randrange(1000))
        if i % 3 else '{\\"order\\": %d, \\"card\\": %s}\\n  cvc: %03d\n' % (i, card(), rng.randrange(1000))
        for i in range(n))


def test_mask_is_linear_and_matches_the_old_windowing():
    import time
    sample = _card_rows(500)
    with patch.object(redact_cards, "_window", lambda text, s, e, breaks=None: _old_window(text, s, e)):
        old = mask_card_numbers(sample)
    assert old != sample and mask_card_numbers(sample) == old
    big = _card_rows(20000, seed=8)
    t0 = time.perf_counter()
    out = mask_card_numbers(big)
    elapsed = time.perf_counter() - t0
    assert out.count("XXXXXXXXXXXX") == 20000
    assert elapsed < 2.0, "20,000 card rows took %.1f s" % elapsed


def test_masked_bare_json_numbers_in_arrays_and_at_the_root_stay_valid_json():
    m = "XXXXXXXXXXXX0366"
    assert json.loads(mask_card_numbers('{"cards":[%s]}' % VISA)) == {"cards": [m]}
    assert json.loads(mask_card_numbers('{"cards":[1, %s, 5]}' % VISA)) == {"cards": [1, m, 5]}
    assert json.loads(mask_card_numbers(json.dumps({"cards": [int(VISA)] * 2}, indent=2))) == {"cards": [m, m]}
    assert json.loads(mask_card_numbers(VISA)) == m
    assert json.loads(mask_card_numbers(VISA + "\n")) == m
    assert json.loads(mask_card_numbers(json.dumps(["say \"hi\"", int(VISA)]))) == ["say \"hi\"", m]
    # Inside a JSON-encoded string: after a key, after the string's opening quote, pretty-printed, two levels.
    for inner in ('{"cards":[%s]}' % VISA, "[%s]" % VISA, json.dumps([int(VISA)], indent=2)):
        out = json.loads(json.loads(mask_card_numbers(json.dumps({"output": inner})))["output"])
        assert m in json.dumps(out) and VISA not in json.dumps(out)
    twice = mask_card_numbers(json.dumps({"o": json.dumps({"x": "[%s]" % VISA})}))
    assert json.loads(json.loads(json.loads(twice)["o"])["x"]) == [m]
    # Not a JSON number: a spaced card at the root stays unquoted.
    assert mask_card_numbers(f"{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}") == "XXXX XXXX XXXX 0366"


def test_cancelled_image_read_leaves_no_flattened_copy(tmp_path):
    """The alpha-dropped ``.rgb.png`` copy shows the unmasked card: a read cancelled while the copy exists
    must still delete it."""
    import asyncio
    import threading
    from tools import vision_tools
    url = _rgba_card_url(0, (400, 100))
    made, release = threading.Event(), threading.Event()
    real = vision_tools._alpha_dropped_copy

    def copy_then_wait(path):
        out = real(path)
        made.set()
        release.wait(10)
        return out

    async def run():
        task = asyncio.create_task(vision_tools._prepare_image(url, "t", None, validate_decode=False))
        assert await asyncio.to_thread(made.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(tmp_path.glob("*.rgb.png")), "the copy was not made before the cancel"
        release.set()
        # asyncio.run joins the worker thread on exit, so the copy's fate is settled when it returns.

    with _cfg(), patch("tools.vision_tools.get_hermes_dir", return_value=tmp_path), \
            patch.object(vision_tools, "_alpha_dropped_copy", copy_then_wait), \
            patch.object(redact_cards, "_ocr", return_value=("", None)):
        asyncio.run(run())
    assert not list(tmp_path.glob("*.rgb.png"))
