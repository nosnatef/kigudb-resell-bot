import test from "node:test";
import assert from "node:assert/strict";
import worker, { dispatchWorkflow } from "./worker.mjs";

const env = {
  GITHUB_TOKEN: "private-test-token", GITHUB_OWNER: "nosnatef",
  GITHUB_REPO: "kigudb-resell-bot", GITHUB_WORKFLOW: "discord-hourly.yml", GITHUB_REF: "main",
};

test("dispatches a real send on main", async () => {
  let calls = 0;
  await dispatchWorkflow(env, async (url, options) => {
    calls++;
    assert.equal(url, "https://api.github.com/repos/nosnatef/kigudb-resell-bot/actions/workflows/discord-hourly.yml/dispatches");
    assert.equal(options.method, "POST");
    assert.deepEqual(JSON.parse(options.body), { ref: "main", inputs: { send: true } });
    assert.equal(options.headers.Authorization, "Bearer private-test-token");
    return { status: 204 };
  });
  assert.equal(calls, 1);
  assert.equal(typeof worker.scheduled, "function");
  assert.equal(worker.fetch, undefined);
});

test("missing secret fails before contacting GitHub", async () => {
  await assert.rejects(dispatchWorkflow({ ...env, GITHUB_TOKEN: "" }, () => assert.fail()), /Missing GITHUB_TOKEN/);
});

test("errors expose status without credentials and do not retry", async () => {
  for (const status of [401, 403, 404, 422, 429, 500]) {
    let calls = 0;
    await assert.rejects(dispatchWorkflow(env, async () => {
      calls++;
      return { status, text: () => assert.fail("Do not expose response body") };
    }), error => error.message.includes(`HTTP ${status}`) && !error.message.includes(env.GITHUB_TOKEN));
    assert.equal(calls, 1);
  }
});

test("ambiguous connection failures are not retried or exposed", async () => {
  let calls = 0;
  await assert.rejects(dispatchWorkflow(env, async () => {
    calls++;
    throw new Error(env.GITHUB_TOKEN);
  }), error => error.message.includes("connection failed") && !error.message.includes(env.GITHUB_TOKEN));
  assert.equal(calls, 1);
});
