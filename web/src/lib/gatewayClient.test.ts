// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { JsonRpcGatewayClient } from "@hermes/shared";

import { GatewayClient } from "./gatewayClient";

const reloadMocks = vi.hoisted(() => ({
  maybeReloadForLoopbackWsAuthFailure: vi.fn(() => false),
}));

vi.mock("./dashboard-auth-reload", () => ({
  maybeReloadForLoopbackWsAuthFailure:
    reloadMocks.maybeReloadForLoopbackWsAuthFailure,
}));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;

  listeners = new Map<string, Array<(event: EventLike) => void>>();
  readyState = 0;
  url: string;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: string, cb: (event: EventLike) => void) {
    const list = this.listeners.get(type) ?? [];
    list.push(cb);
    this.listeners.set(type, list);
  }

  close() {}

  emit(type: string, event: EventLike) {
    for (const cb of this.listeners.get(type) ?? []) {
      cb(event);
    }
  }

  removeEventListener(type: string, cb: (event: EventLike) => void) {
    const list = this.listeners.get(type) ?? [];
    this.listeners.set(
      type,
      list.filter((item) => item !== cb),
    );
  }

  send() {}
}

type EventLike = {
  code?: number;
};

beforeEach(() => {
  FakeWebSocket.instances = [];
  reloadMocks.maybeReloadForLoopbackWsAuthFailure.mockClear();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
    configurable: true,
    value: "stale-token",
    writable: true,
  });
  Object.defineProperty(window, "__HERMES_AUTH_REQUIRED__", {
    configurable: true,
    value: false,
    writable: true,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  // fork parity NOTE (2026-08-07 upstream merge): the source-tagging suite below
  // spies on JsonRpcGatewayClient.prototype, so the shared teardown must also
  // restore prototype spies — upstream's teardown only unstubbed globals.
  vi.restoreAllMocks();
});

describe("GatewayClient", () => {
  it("treats loopback 4401 closes as stale-token reload candidates", async () => {
    reloadMocks.maybeReloadForLoopbackWsAuthFailure.mockReturnValue(true);
    const gw = new GatewayClient();
    const connectPromise = gw.connect();

    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    socket.readyState = 1;
    socket.emit("open", {});
    await connectPromise;

    socket.emit("close", { code: 4401 });

    expect(
      reloadMocks.maybeReloadForLoopbackWsAuthFailure,
    ).toHaveBeenCalledWith(4401);
    expect(gw.connectionState).toBe("open");
  });
});

// The dashboard, the Electron desktop app, and the Ink TUI all drive the same
// tui_gateway JSON-RPC server, which historically stamped every turn
// platform="tui". GatewayClient now tags SESSION-ORIGINATING calls with
// source:"dashboard" so the backend attributes browser-dashboard turns to the
// dashboard client (→ blackbox turns.platform → tokens.ace source chart). These
// tests pin that contract by spying on the inherited request().

function captureForwarded() {
  // Spy on the base-class request so we see exactly what GatewayClient forwards,
  // without needing a live WebSocket.
  const spy = vi
    .spyOn(JsonRpcGatewayClient.prototype, "request")
    .mockResolvedValue({} as never);
  return spy;
}

describe("GatewayClient source tagging", () => {
  it('stamps source:"dashboard" on session.create', async () => {
    const spy = captureForwarded();
    await new GatewayClient().request("session.create", { cols: 96 });
    expect(spy).toHaveBeenCalledWith(
      "session.create",
      { cols: 96, source: "dashboard" },
      undefined,
      undefined,
    );
  });

  it('stamps source:"dashboard" on session.resume', async () => {
    const spy = captureForwarded();
    await new GatewayClient().request("session.resume", { session_id: "s1" });
    expect(spy.mock.calls[0][1]).toMatchObject({ session_id: "s1", source: "dashboard" });
  });

  it("does NOT add source to non-session-originating methods", async () => {
    const spy = captureForwarded();
    await new GatewayClient().request("prompt.submit", { session_id: "s1", text: "hi" });
    expect(spy.mock.calls[0][1]).not.toHaveProperty("source");
  });

  it("never overrides an explicit caller-provided source (e.g. sidebar sidecar tool)", async () => {
    const spy = captureForwarded();
    await new GatewayClient().request("session.create", { close_on_disconnect: true, source: "tool" });
    expect(spy.mock.calls[0][1]).toMatchObject({ source: "tool" });
  });
});
