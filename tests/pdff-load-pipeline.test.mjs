import assert from "node:assert/strict";
import test from "node:test";

import { runPdffLoadPipeline } from "../app/lib/pdffLoadPipeline.ts";

function deferred() {
  let resolve;
  const promise = new Promise((next) => { resolve = next; });
  return { promise, resolve };
}

test("In/Opp start only after Fat, Water, and pancreas reference are ready", async () => {
  const tasks = Object.fromEntries(["f", "w", "in", "opp", "reference"].map((key) => [key, deferred()]));
  const events = [];
  const pipeline = runPdffLoadPipeline({
    loadVolume: async (sequence) => {
      events.push(`start:${sequence}`);
      await tasks[sequence].promise;
      events.push(`ready:${sequence}`);
      return sequence;
    },
    loadReference: async () => {
      events.push("start:reference");
      await tasks.reference.promise;
      events.push("ready:reference");
      return { available: true };
    },
    onPrimaryReady: () => events.push("primary-ready"),
  });

  assert.deepEqual(events, ["start:f", "start:w", "start:reference"]);
  tasks.f.resolve();
  tasks.w.resolve();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(events.some((event) => event === "start:in" || event === "start:opp"), false);

  tasks.reference.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(events.slice(-3), ["primary-ready", "start:in", "start:opp"]);

  tasks.in.resolve();
  tasks.opp.resolve();
  await pipeline;
});
