import assert from "node:assert/strict";
import test from "node:test";

import {
  PACKET_MARK,
  apply,
  createAdapter,
  packetOf,
  spokenTurn,
} from "./index.js";

function host() {
  const calls = [];
  const waiting = { packet: `${PACKET_MARK}\n历史包` };
  const run = (payload) => {
    calls.push(payload);
    if (payload.hook_event_name === "Stop") return {};
    if (payload.hook_event_name === "UserPromptSubmit" && calls.filter((item) => item.hook_event_name === "UserPromptSubmit").length > 1) {
      return {};
    }
    if (!waiting.packet) return {};
    const packet = waiting.packet;
    waiting.packet = "";
    return { hookSpecificOutput: { additionalContext: packet } };
  };
  return { calls, run };
}

function context(handlers) {
  return {
    on(event, handler) {
      handlers[event] = handler;
    },
  };
}

test("a user line that arrives first is kept for the step, and the reply goes to the hook", async () => {
  const bridge = host();
  const handlers = {};
  createAdapter(bridge.run, "/tmp/rogmem-home")(context(handlers));
  handlers["session/event"](
    { id: "s", header: { cwd: "/work/rogmind" } },
    { type: "user/message", data: { content: [{ type: "text", text: "合成句：用户刚说的。" }] } },
  );
  handlers["session/event"](
    { id: "s", header: { cwd: "/work/rogmind" } },
    {
      type: "user/message",
      data: {
        content: [{ type: "text", text: `${PACKET_MARK}\n这是钩子塞回去的` }],
        source: { kind: "plugin", plugin: "rogmem-dsh" },
      },
    },
  );
  const decision = { kind: "enter", messages: [{ role: "user", content: [{ type: "text", text: "原步骤" }] }] };
  const opened = await handlers["agent/pre-step"](
    { step: 3, agent: { session: { id: "s", header: { cwd: "/work/rogmind" } } } },
    async () => decision,
  );
  assert.equal(opened.messages[0].content[0].text, `${PACKET_MARK}\n历史包`);
  assert.equal(opened.messages[1], decision.messages[0]);
  assert.equal(bridge.calls.some((item) => item.hook_event_name === "SessionStart"), false);
  handlers["session/event"](
    { id: "s", header: { cwd: "/work/rogmind" } },
    { type: "assistant/message", data: { content: "合成句：最后的回复。" } },
  );
  assert.deepEqual(
    bridge.calls.map((item) => item.hook_event_name),
    ["UserPromptSubmit", "Stop"],
  );
  assert.equal(bridge.calls[0].prompt, "合成句：用户刚说的。");
  assert.equal(bridge.calls[0].cwd, "/work/rogmind");
  assert.equal(bridge.calls[1].text, "合成句：最后的回复。");
  const again = await handlers["agent/pre-step"](
    { agent: { session: { id: "s" } } },
    async () => opened,
  );
  assert.equal(again, opened);
});

test("the step asks the hook when no earlier event brought a packet", async () => {
  const bridge = host();
  const handlers = {};
  apply(context(handlers), { home: "/tmp/rogmem-home", run: bridge.run });
  const decision = { kind: "enter", messages: [] };
  const opened = await handlers["agent/pre-step"](
    { agent: { session: { id: "fresh", header: { cwd: "/work/rogmind" } } } },
    async () => decision,
  );
  assert.equal(bridge.calls[0].hook_event_name, "SessionStart");
  assert.equal(opened.messages[0].source.plugin, "rogmem-dsh");
  assert.equal(packetOf({ additional_context: `${PACKET_MARK}\n另一路` }), `${PACKET_MARK}\n另一路`);
  assert.equal(spokenTurn({ type: "tool/result", data: { content: "工具输出" } }), null);
});

test("a hook that fails leaves the model's own step in place", async () => {
  const handlers = {};
  createAdapter(() => {
    throw new Error("hook down");
  })(context(handlers));
  const decision = { kind: "enter", messages: [{ role: "user", content: [{ type: "text", text: "原步骤" }] }] };
  const result = await handlers["agent/pre-step"](
    { agent: { session: { id: "s" } } },
    async () => decision,
  );
  assert.equal(result, decision);
  handlers["session/event"]({ id: "s" }, { type: "user/message", data: { content: "合成句" } });
});
