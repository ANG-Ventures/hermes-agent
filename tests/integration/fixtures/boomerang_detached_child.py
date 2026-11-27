"""Isolated child harness: production detached entry, no plugin/network discovery."""
import os
import sys
import socket
from pathlib import Path
from unittest.mock import patch

# Invoke by script path in a fresh process; source is the tested checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from dotenv import load_dotenv
load_dotenv(Path(os.environ["HERMES_HOME"]) / ".env", override=True)
os.environ["HERMES_STATE_DELETED_WAL_CHECK"] = "registry"
connect = socket.socket.connect
def loopback_only(sock, address):
    if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise AssertionError("detached fixture attempted non-loopback connection")
    return connect(sock, address)
socket.socket.connect = loopback_only

import agent.model_metadata as metadata
import agent.relay_runtime as relay
import agent.turn_recovery as recovery
from tools.delegate_detached import _child_main

sleep = recovery.interruptible_backoff_sleep
with (patch("hermes_cli.plugins.discover_plugins", lambda *a, **kw: None),
      patch.object(metadata, "get_model_context_length", lambda *a, **kw: 256000),
      patch.object(metadata, "_resolve_custom_endpoint_context_length", lambda *a, **kw: 256000),
      patch.object(relay._ProcessRelayPluginConfiguration, "_activate", lambda *a, **kw: relay._RelayPluginConfigurationState.DISABLED),
      patch("hermes_cli.mcp_startup._discover_mcp_tools_without_interactive_oauth", lambda: None),
      patch("gateway.run_startup.GatewayStartupMixin._register_config_hooks", lambda *a, **kw: None),
      patch("model_tools.get_tool_definitions", lambda **kw: []),
      patch("model_tools.check_toolset_requirements", lambda **kw: {}),
      patch.object(recovery, "interruptible_backoff_sleep", lambda agent, wait, *a, **kw: sleep(agent, min(wait, 0.001), *a, **kw))):
    raise SystemExit(_child_main(Path(sys.argv[1])))
