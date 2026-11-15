/**
 * DSH speaks in plugin events. This file only translates those events into the
 * official hook and puts the hook's packet into the step. Capture, the outbox,
 * and the quiet period stay in that one script.
 */
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const name = "rogmem-dsh";
export const PACKET_MARK = "【rogmem 历史包】";

export function hookScript(home = homedir()) {
  const beside = join(dirname(fileURLToPath(import.meta.url)), "rogmem.py");
  if (existsSync(beside)) return beside;
  const installed = join(home, ".dsh", "hooks", "rogmem.py");
  return existsSync(installed) ? installed : "";
}

export function runHook(payload, { home = homedir(), python = "python3", script = "" } = {}) {
  const target = script || hookScript(home);
  if (!target) return {};
  let proc;
  try {
    proc = spawnSync(python, [target, "--program", "DSH", "--home", home], {
      input: JSON.stringify(payload),
      encoding: "utf8",
      timeout: 4000,
    });
  } catch {
    return {};
  }
  try {
    const loaded = JSON.parse(proc.stdout || "{}");
    return loaded && typeof loaded === "object" ? loaded : {};
  } catch {
    return {};
  }
}

export function packetOf(result) {
  if (!result || typeof result !== "object") return "";
  const specific = result.hookSpecificOutput && result.hookSpecificOutput.additionalContext;
  const candidates = [specific, result.additional_context, result.context];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.includes(PACKET_MARK)) return candidate;
  }
  return "";
}

export function visibleText(content) {
  if (typeof content === "string") return content.trim();
  if (Array.isArray(content)) {
    return content.map((part) => visibleText(part)).filter(Boolean).join("\n").trim();
  }
  if (content && typeof content === "object") {
    if (content.type != null && content.type !== "text") return "";
    if (typeof content.text === "string") return content.text.trim();
    if (content.content != null) return visibleText(content.content);
  }
  return "";
}

export function sessionIdOf(session) {
  const header = session?.header ?? {};
  const id = session?.id ?? header.id;
  const text = id == null ? "" : String(id).trim();
  return text || "session";
}

export function snapshotMessage(text) {
  return {
    id: randomUUID(),
    role: "user",
    content: [{ type: "text", text }],
    source: { kind: "plugin", plugin: name, form: "snapshot" },
  };
}

export function alreadyInjected(messages) {
  return (messages || []).some((message) => {
    if (message?.source?.plugin === name) return true;
    return visibleText(message?.content).includes(PACKET_MARK);
  });
}

function ruleText(home) {
  try {
    const text = readFileSync(join(home, ".dsh", "skills", "rogmem", "SKILL.md"), "utf8");
    const start = text.indexOf("【rogmem 规则版本：");
    const end = text.indexOf("【rogmem 规则结束】");
    if (start >= 0 && end > start) return text.slice(start, end + "【rogmem 规则结束】".length).trim();
  } catch {
    /* the packet from the hook still stands */
  }
  return "";
}

function withRule(packet, home) {
  const rule = ruleText(home);
  if (!rule || packet.includes("【rogmem 规则版本：")) return packet;
  return `${rule}\n\n${packet}`;
}

export function eventFor(session, eventName) {
  const payload = { hook_event_name: eventName, session_id: sessionIdOf(session) };
  const cwd = session?.header?.cwd;
  if (typeof cwd === "string" && cwd.trim()) payload.cwd = cwd.trim();
  return payload;
}

export function spokenTurn(event) {
  const type = event?.type;
  if (type !== "user/message" && type !== "assistant/message") return null;
  const data = event.data ?? {};
  const source = data.source ?? data.message?.source;
  if (source?.kind === "plugin") return null;
  const text = visibleText(data.content ?? data.message?.content);
  if (!text || text.includes(PACKET_MARK)) return null;
  return { role: type === "assistant/message" ? "assistant" : "user", text };
}

export function createAdapter(run, home = homedir()) {
  const waiting = new Map();
  return function apply(ctx) {
    ctx.on("agent/pre-step", async (payload, next) => {
      const decision = await next();
      if (!decision || decision.kind === "reject" || payload?.signal?.aborted) return decision;
      try {
        const session = payload?.agent?.session;
        const sid = sessionIdOf(session);
        const messages = Array.isArray(decision.messages) ? decision.messages : [];
        if (alreadyInjected(messages)) {
          waiting.delete(sid);
          return decision;
        }
        let packet = waiting.get(sid) || "";
        if (!packet) {
          packet = packetOf(run(eventFor(session, "SessionStart"), { home }));
          if (packet) waiting.set(sid, packet);
        }
        if (!packet) return decision;
        waiting.delete(sid);
        return { kind: "enter", messages: [snapshotMessage(withRule(packet, home)), ...messages] };
      } catch {
        return decision;
      }
    }, { prepend: true });
    ctx.on("session/event", (session, event) => {
      try {
        const spoken = spokenTurn(event);
        if (!spoken) return;
        const payload = eventFor(session, spoken.role === "assistant" ? "Stop" : "UserPromptSubmit");
        if (spoken.role === "assistant") payload.text = spoken.text;
        else payload.prompt = spoken.text;
        const packet = packetOf(run(payload, { home }));
        if (packet) waiting.set(sessionIdOf(session), packet);
      } catch {
        /* the session continues either way */
      }
    });
  };
}

export function apply(ctx, options = {}) {
  const home = options.home || homedir();
  const run = options.run || runHook;
  createAdapter(run, home)(ctx);
}
