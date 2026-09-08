/* SPDX-License-Identifier: LGPL-2.1-or-later */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { mapWithConcurrency, OwnerRefreshGate, SchedulerRequestGate, withTimeout } from "../src/scheduler-request.js";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(await readFile(resolve(here, "scheduler-refresh-fixture.json"), "utf8"));
const gate = new SchedulerRequestGate();
const applied = [];

const older = gate.begin(fixture.owner_uid);
const newer = gate.begin(fixture.owner_uid);
assert.equal(older.generation, fixture.responses.find(response => response.label === "older").generation);
assert.equal(newer.generation, fixture.responses.find(response => response.label === "newer").generation);

const responses = fixture.responses.map(response => new Promise(resolveResponse => {
    setTimeout(() => {
        const request = response.label === "older" ? older : newer;
        if (gate.isCurrent(fixture.owner_uid, request))
            applied.push(response.label);
        resolveResponse();
    }, response.delay_ms);
}));
await Promise.all(responses);

assert.deepEqual(applied, fixture.expected_applied);
assert.equal(gate.isCurrent(fixture.owner_uid, older), false);
assert.equal(gate.isCurrent(fixture.owner_uid, newer), true);

const refreshGate = new OwnerRefreshGate();
const active = new Map();
const maxActive = new Map();
const run = (uid, delay) => refreshGate.start(uid, async request => {
    const count = (active.get(request.key) || 0) + 1;
    active.set(request.key, count);
    maxActive.set(request.key, Math.max(maxActive.get(request.key) || 0, count));
    await new Promise(resolveResponse => setTimeout(resolveResponse, delay));
    active.set(request.key, active.get(request.key) - 1);
    return request;
});
const ownerA = run(1000, 10);
const ownerARepeat = run(1000, 1);
const ownerB = run(2000, 1);
assert.equal(ownerA, ownerARepeat);
assert.notEqual(ownerA, ownerB);
refreshGate.invalidate(1000);
await Promise.all([ownerA.promise, ownerB.promise]);
assert.equal(maxActive.get("1000"), 1);
assert.equal(maxActive.get("2000"), 1);
assert.equal(refreshGate.isCurrent(1000, ownerA.request), false);
assert.equal(refreshGate.isCurrent(2000, ownerB.request), true);

// The timer may enumerate many owner sockets. A per-owner gate alone would
// still start one inventory and scheduler collector per owner at once; the
// global cap keeps the next timer fire bounded while retaining owner
// isolation.
const globalGate = new OwnerRefreshGate(2);
let globalActive = 0;
let globalMaxActive = 0;
const globalRuns = [1, 2, 3, 4].map(uid => globalGate.start(uid, async () => {
    globalActive++;
    globalMaxActive = Math.max(globalMaxActive, globalActive);
    await new Promise(resolveResponse => setTimeout(resolveResponse, 4));
    globalActive--;
}));
await Promise.all(globalRuns.map(entry => entry.promise));
assert.equal(globalMaxActive, 2);

let activeWorkers = 0;
let maxWorkers = 0;
const bounded = await mapWithConcurrency([1, 2, 3, 4, 5], async value => {
    activeWorkers++;
    maxWorkers = Math.max(maxWorkers, activeWorkers);
    await new Promise(resolveResponse => setTimeout(resolveResponse, 2));
    activeWorkers--;
    return value * 2;
}, 2);
assert.deepEqual(bounded, [2, 4, 6, 8, 10]);
assert.equal(maxWorkers, 2);
await assert.rejects(withTimeout(new Promise(() => {}), 2, "fixture-timeout"), /fixture-timeout/);

console.log("scheduler-refresh fixture: PASS");
