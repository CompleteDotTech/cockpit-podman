/* SPDX-License-Identifier: LGPL-2.1-or-later */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { healthAssessment, isValidHealthDetails } from "../src/health.js";
import { KeyedRequestGate, mapWithConcurrency, OwnerRefreshGate, RequestConcurrencyGate, SchedulerRequestGate, withTimeout } from "../src/scheduler-request.js";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(await readFile(resolve(here, "scheduler-refresh-fixture.json"), "utf8"));

// A closed connection can still deliver one final stats frame after a
// same-UID reconnect. The production guard must run before that stale frame
// can replace the new connection's pending batch or cancel its timer.
const applicationSource = await readFile(resolve(here, "../src/app.jsx"), "utf8");
// cockpit.spawn().input(message) closes the stream by default. The scheduler
// collector must keep its identity write open and send one explicit EOF with
// the following null input; otherwise the bridge receives duplicate `done`
// controls when the transport is already closing.
assert.equal((applicationSource.match(/process\.input\(JSON\.stringify\(identity\), true\);/g) || []).length, 1);
assert.equal((applicationSource.match(/process\.input\(JSON\.stringify\(identity\)\);/g) || []).length, 0);
const bridgeControls = [];
const schedulerProcess = {
    input(message, stream) {
        if (!stream)
            bridgeControls.push({ control: "done" });
        else
            bridgeControls.push({ data: message });
    },
};
const schedulerIdentity = { containers: [] };
schedulerProcess.input(JSON.stringify(schedulerIdentity), true);
schedulerProcess.input(null);
assert.deepEqual(bridgeControls, [
    { data: JSON.stringify(schedulerIdentity) },
    { control: "done" },
]);
// Exercise the production inventory mapping with a real list-shaped one-shot.
const inventoryMapperStart = applicationSource.indexOf("const containerFromInventory =");
const inventoryMapperEnd = applicationSource.indexOf("\n// sort order", inventoryMapperStart);
const mapInventory = new Function(applicationSource.slice(inventoryMapperStart, inventoryMapperEnd) + "\nreturn containerFromInventory;")();
for (const [exitCode, expected] of [[0, "completed"], ["0", "completed"], [1, "stopped"], [null, "stopped"], [undefined, "stopped"]]) {
    const row = mapInventory({ Id: "one-shot", Names: ["one-shot"], State: "exited", ExitCode: exitCode,
        Labels: { "com.docker.compose.oneoff": "True" } }, 0, "0-one-shot");
    assert.equal(healthAssessment(row).status, expected);
}

const statsQueueStart = applicationSource.indexOf("    queueContainerStats(con, stats) {");
const statsQueueEnd = applicationSource.indexOf("\n    clearContainerStats", statsQueueStart);
assert.ok(statsQueueStart >= 0 && statsQueueEnd > statsQueueStart);
const statsQueueBody = applicationSource.slice(statsQueueStart, statsQueueEnd);
assert.ok(statsQueueBody.startsWith("    queueContainerStats(con, stats) {\n        if (!this.isCurrentConnection(con))\n            return;"));

// Scheduler metadata must start from the validated inventory snapshot in the
// periodic owner pass. The explicit request arguments keep scheduler and
// health results tied to the same generations.
const ownerHealthStart = applicationSource.indexOf("    async collectOwnerHealth(con, request) {");
const ownerHealthEnd = applicationSource.indexOf("\n    schedulerErrorRecord", ownerHealthStart);
const ownerHealthBody = applicationSource.slice(ownerHealthStart, ownerHealthEnd);
const ownerSchedulerStart = ownerHealthBody.indexOf("schedulerPromise = this.updateSchedulerCoverage");
const ownerInspectStart = ownerHealthBody.indexOf("const results = await mapWithConcurrency");
assert.ok(ownerSchedulerStart >= 0 && ownerSchedulerStart < ownerInspectStart);
assert.match(ownerHealthBody.slice(ownerSchedulerStart, ownerInspectStart),
             /schedulerRequest, request,[\s\S]*HEALTH_SCHEDULER_TIMEOUT_MS/);
assert.match(ownerHealthBody, /result && !isValidHealthDetails\(inventory, result\.detail\)/);
assert.match(ownerHealthBody, /errors\[key\] = CONTAINER_INSPECT_INVALID/);
assert.match(ownerHealthBody, /failed\.healthDetailsLoaded = false/);
assert.match(applicationSource, /if \(!isValidHealthDetails\(\{ Id: id \}, details\)\)/);

// The owner refresh must classify null/undefined and malformed truthy detail
// replies as collection failures, while a later exact inspect clears that
// failure and is eligible to mark details loaded.
const ownerInventory = { Id: "owner-refresh-container" };
const validOwnerDetail = { Id: ownerInventory.Id, State: { Status: "running" }, Config: {} };
for (const malformed of [null, undefined, { ...validOwnerDetail, Id: "other" },
    { ...validOwnerDetail, State: {} }, { ...validOwnerDetail, Config: [] }])
    assert.equal(isValidHealthDetails(ownerInventory, malformed), false);
assert.equal(isValidHealthDetails(ownerInventory, validOwnerDetail), true);

const initialHealthStart = applicationSource.indexOf("    async collectInitialContainers(con, refreshRequest) {");
const initialHealthEnd = applicationSource.indexOf("\n    updateImages", initialHealthStart);
const initialHealthBody = applicationSource.slice(initialHealthStart, initialHealthEnd);
const initialSchedulerStart = initialHealthBody.indexOf("schedulerPromise = this.updateSchedulerCoverage");
const initialInspectStart = initialHealthBody.indexOf("const results = await mapWithConcurrency");
assert.equal(initialSchedulerStart, -1);
assert.equal(initialInspectStart, -1);
assert.match(applicationSource,
             /this\.healthRefreshFollowups = new Map\(\)/);
assert.match(applicationSource,
             /healthRefreshFollowups\.set\(ownerKey, con\)[\s\S]*setTimeout\(\(\) =>/);
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

// Invalidating an in-flight owner must not make a reconnect reuse the old
// promise.  The fresh generation waits for the old operation to settle even
// when a global owner slot is available.
const reconnectGate = new OwnerRefreshGate(2);
const reconnectEvents = [];
const staleReconnect = reconnectGate.start("reconnect-owner", async () => {
    reconnectEvents.push("old-start");
    await new Promise(resolveResponse => setTimeout(resolveResponse, 10));
    reconnectEvents.push("old-done");
    return "old-result";
});
reconnectGate.invalidate("reconnect-owner");
const freshReconnect = reconnectGate.start("reconnect-owner", async () => {
    reconnectEvents.push("fresh-start");
    return "fresh-result";
});
assert.notEqual(staleReconnect, freshReconnect);
assert.equal(await staleReconnect.promise, "old-result");
assert.equal(await freshReconnect.promise, "fresh-result");
assert.deepEqual(reconnectEvents, ["old-start", "old-done", "fresh-start"]);
assert.equal(reconnectGate.isCurrent("reconnect-owner", staleReconnect.request), false);
assert.equal(reconnectGate.isCurrent("reconnect-owner", freshReconnect.request), true);

// Model the initial-load lifecycle: an invalidated generation may resolve
// without committing `containersLoaded`, so the caller must start the queued
// fresh generation and eventually settle loaded state.
const initialLifecycleGate = new OwnerRefreshGate(2);
let initialLoaded = false;
let initialRuns = 0;
const runInitialLoad = () => {
    initialLifecycleGate.invalidate("initial-owner");
    const entry = initialLifecycleGate.start("initial-owner", async request => {
        initialRuns++;
        await new Promise(resolveResponse => setTimeout(resolveResponse, 2));
        if (!initialLifecycleGate.isCurrent("initial-owner", request))
            return;
        initialLoaded = true;
    });
    return entry.promise.then(() => {
        if (!initialLoaded)
            return runInitialLoad();
    });
};
const initialLoad = runInitialLoad();
initialLifecycleGate.invalidate("initial-owner");
await initialLoad;
assert.equal(initialLoaded, true);
assert.equal(initialRuns, 2);

// Event detail requests are keyed by full owner/container identity. A newer
// event for B must discard only B's older inspect while an in-flight A inspect
// remains applicable. The production path uses a separate owner gate for
// scheduler coverage, so scheduler invalidation cannot drop container detail.
const eventDetailGate = new SchedulerRequestGate();
const detailAKey = "event-guard-owner-a";
const detailBKey = "event-guard-owner-b";
const detailA = eventDetailGate.begin(detailAKey);
const detailBOld = eventDetailGate.begin(detailBKey);
const detailBNew = eventDetailGate.begin(detailBKey);
assert.equal(eventDetailGate.isCurrent(detailAKey, detailA), true);
assert.equal(eventDetailGate.isCurrent(detailBKey, detailBOld), false);
assert.equal(eventDetailGate.isCurrent(detailBKey, detailBNew), true);

// Health and exec events do not launch scheduler collection, so they must not
// invalidate an in-flight owner scheduler result with no replacement. A
// topology event does request a new owner-scoped scheduler generation.
const schedulerCoverageGate = new SchedulerRequestGate();
const schedulerOwner = "event-guard-scheduler-owner";
const schedulerInFlight = schedulerCoverageGate.begin(schedulerOwner);
assert.equal(schedulerCoverageGate.isCurrent(schedulerOwner, schedulerInFlight), true);
const topologyRefresh = schedulerCoverageGate.begin(schedulerOwner);
assert.equal(schedulerCoverageGate.isCurrent(schedulerOwner, schedulerInFlight), false);
assert.equal(schedulerCoverageGate.isCurrent(schedulerOwner, topologyRefresh), true);

// Health events can arrive continuously while both owner inventories are
// inspected. Model the actual event path: each event starts a per-container
// inspect, records its generation/action, and commits only when that event is
// still current. The initial inventory captures generations before listing,
// preserves event-updated rows at commit, and merges event-created rows that
// were absent from the listing. Exercise the observed 84/72-row scopes.
const sleep = delay => new Promise(resolveResponse => setTimeout(resolveResponse, delay));
const eventFloodGate = new OwnerRefreshGate(2);
const runEventFloodInitialLoad = async (rowCount, ownerIndex) => {
    const uid = `event-flood-${ownerIndex}`;
    const ids = Array.from({ length: rowCount }, (_, index) =>
        `${index.toString(16).padStart(2, "0")}${"0".repeat(62)}`);
    const eventGate = new SchedulerRequestGate();
    const eventGenerations = new Map();
    const eventActions = new Map();
    const pendingEvents = new Map();
    const state = new Map();
    const schedulerGate = new SchedulerRequestGate();
    const initialEventGenerations = new Map(eventGenerations);
    let eventCount = 0;
    let loaded = false;

    const emitContainerEvent = (id, action = "health_status") => {
        const key = `${uid}-${id}`;
        const request = eventGate.begin(key);
        eventGenerations.set(key, request.generation);
        eventActions.set(key, action);
        const wait = pendingEvents.get(key) || Promise.resolve();
        const operation = wait.then(async () => {
            await sleep(1);
            if (eventGate.isCurrent(key, request))
                state.set(key, { Id: id, source: "event", generation: request.generation });
        });
        pendingEvents.set(key, operation);
        operation.finally(() => {
            if (pendingEvents.get(key) === operation)
                pendingEvents.delete(key);
        });
        return operation;
    };

    const entry = eventFloodGate.start(uid, async request => {
        // getContainers() resolves before the bounded 8-worker inspect fanout.
        await sleep(2);
        const inventory = ids.map(id => ({ Id: id }));
        const inspected = await mapWithConcurrency(inventory, async row => {
            await sleep(1);
            return { ...row, source: "inventory" };
        }, 8);
        assert.equal(eventFloodGate.isCurrent(uid, request), true);

        const listedKeys = new Set(inspected.map(row => `${uid}-${row.Id}`));
        const committed = new Map();
        for (const [key, row] of state) {
            if (key.startsWith(`${uid}-`) && !listedKeys.has(key)) {
                const snapshotGeneration = initialEventGenerations.get(key) || 0;
                const currentGeneration = eventGenerations.get(key) || 0;
                if (currentGeneration !== snapshotGeneration && eventActions.get(key) !== "remove")
                    committed.set(key, row);
            }
        }
        for (const row of inspected) {
            const key = `${uid}-${row.Id}`;
            const snapshotGeneration = initialEventGenerations.get(key) || 0;
            const currentGeneration = eventGenerations.get(key) || 0;
            if (currentGeneration !== snapshotGeneration) {
                if (eventActions.get(key) === "remove")
                    continue;
                const current = state.get(key);
                if (current) {
                    committed.set(key, current);
                    continue;
                }
            }
            committed.set(key, row);
        }
        state.clear();
        for (const [key, row] of committed)
            state.set(key, row);
        loaded = true;

        // The scheduler collector runs after the inventory commit. Health
        // events must not invalidate this owner-scoped result while they
        // continue through independent per-container detail requests.
        const schedulerRequest = schedulerGate.begin(uid);
        await sleep(2);
        assert.equal(schedulerGate.isCurrent(uid, schedulerRequest), true);
    });

    // This is the health_status stream that previously invalidated the owner
    // generation on every event. It deliberately never invalidates the owner
    // gate; only the per-container inspect generation changes.
    const events = setInterval(() => {
        const id = ids[eventCount % ids.length];
        eventCount++;
        void emitContainerEvent(id);
    }, 1);
    await entry.promise;
    clearInterval(events);
    await Promise.all([...pendingEvents.values()]);
    assert.equal(loaded, true);
    assert.equal(state.size, rowCount);
    assert.ok(eventCount > 0);
    return { state, eventCount };
};

const eventFloodLoads = await Promise.all([84, 72].map(runEventFloodInitialLoad));
assert.deepEqual(eventFloodLoads.map(result => result.state.size), [84, 72]);
assert.ok(eventFloodLoads.every(result => result.eventCount > 0));

// A remove tombstone must win over the stale inventory row, while an event
// created row absent from that inventory must survive the initial rebuild.
const mergeGate = new SchedulerRequestGate();
const mergeOwner = "event-merge-owner";
const removedId = "a".repeat(64);
const createdId = "b".repeat(64);
const removedKey = `${mergeOwner}-${removedId}`;
const createdKey = `${mergeOwner}-${createdId}`;
const mergeGenerations = new Map();
const mergeActions = new Map();
const mergeState = new Map([[removedKey, { Id: removedId, source: "old" }],
    [createdKey, { Id: createdId, source: "event" }]]);
const mergeSnapshot = new Map(mergeGenerations);
const removedRequest = mergeGate.begin(removedKey);
mergeGenerations.set(removedKey, removedRequest.generation);
mergeActions.set(removedKey, "remove");
const createdRequest = mergeGate.begin(createdKey);
mergeGenerations.set(createdKey, createdRequest.generation);
mergeActions.set(createdKey, "create");
const mergeInventory = [{ Id: removedId }];
const mergedState = new Map();
for (const [key, row] of mergeState) {
    if (mergeInventory.some(container => `${mergeOwner}-${container.Id}` === key))
        continue;
    if ((mergeGenerations.get(key) || 0) !== (mergeSnapshot.get(key) || 0) &&
        mergeActions.get(key) !== "remove")
        mergedState.set(key, row);
}
for (const container of mergeInventory) {
    const key = `${mergeOwner}-${container.Id}`;
    if (mergeActions.get(key) === "remove")
        continue;
    mergedState.set(key, mergeState.get(key) || container);
}
assert.equal(mergeGate.isCurrent(removedKey, removedRequest), true);
assert.equal(mergeGate.isCurrent(createdKey, createdRequest), true);
assert.equal(mergedState.has(removedKey), false);
assert.equal(mergedState.get(createdKey)?.source, "event");

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

const queuedGate = new OwnerRefreshGate(1);
const queuedOwner = queuedGate.start("queued-owner", async () => {
    await new Promise(resolveResponse => setTimeout(resolveResponse, 4));
});
const waitingOwner = queuedGate.start("waiting-owner", async () => undefined);
assert.equal(queuedGate.start("waiting-owner", async () => undefined), waitingOwner);
await Promise.all([queuedOwner.promise, waitingOwner.promise]);

// Shared Podman HTTP calls have a global cap, and a timeout must remove a
// queued call before it can start later and re-saturate the connection.
const requestGate = new RequestConcurrencyGate(1);
let requestStarts = 0;
const heldRequest = requestGate.run(async () => {
    requestStarts++;
    await new Promise(resolveResponse => setTimeout(resolveResponse, 4));
});
const canceledRequest = requestGate.run(async () => {
    requestStarts++;
});
canceledRequest.close("timeout");
await assert.rejects(canceledRequest, /timeout/);
await heldRequest;
assert.equal(requestStarts, 1);

const activeRequestGate = new RequestConcurrencyGate(1);
let activeClosed = false;
const activeCancellable = activeRequestGate.run(() => {
    const request = new Promise(() => {});
    request.close = () => { activeClosed = true; };
    return request;
});
activeCancellable.close("timeout");
await assert.rejects(activeCancellable, /timeout/);
assert.equal(activeClosed, true);

// A container event must not reuse an inspect that began before that event,
// and a reconnect with the same UID must not reuse the old connection's
// promise. Newer requests wait behind a running request for that ID.
const keyedRuns = [];
const keyedGate = new KeyedRequestGate(operation => operation());
const firstConnection = {};
const secondConnection = {};
const keyedRequest = (con, generation, value) => keyedGate.request(
    "owner-container",
    { con, generation },
    async () => {
        keyedRuns.push(value);
        await new Promise(resolveResponse => setTimeout(resolveResponse, 2));
        return value;
    },
    metadata => metadata.con === con && metadata.generation === generation,
);
const preEvent = keyedRequest(firstConnection, 0, "pre-event");
const eventRefresh = keyedRequest(firstConnection, 1, "event");
assert.notEqual(preEvent, eventRefresh);
assert.equal(await preEvent, "pre-event");
assert.equal(await eventRefresh, "event");
const reconnectRefresh = keyedRequest(secondConnection, 0, "reconnect");
assert.notEqual(reconnectRefresh, eventRefresh);
assert.equal(await reconnectRefresh, "reconnect");
assert.deepEqual(keyedRuns, ["pre-event", "event", "reconnect"]);

// The periodic refresh can reach an inspect near its overall deadline while
// an event still has time to consume the same in-flight result. Expiring one
// caller must not cancel another caller's request or manufacture a row error.
const sharedRequests = new KeyedRequestGate(operation => operation());
let finishShared;
let sharedTransportCloses = 0;
let sharedTransportStarts = 0;
const sharedOperation = () => {
    sharedTransportStarts++;
    const request = new Promise(resolveResponse => { finishShared = resolveResponse; });
    request.close = () => { sharedTransportCloses++; };
    return request;
};
const periodicInspect = sharedRequests.request("shared-container", {}, sharedOperation, () => true);
const eventInspect = sharedRequests.request("shared-container", {}, sharedOperation, () => true);
const periodicOutcome = assert.rejects(periodicInspect, /timeout/);
const eventOutcome = assert.doesNotReject(eventInspect);
periodicInspect.close("timeout");
await periodicOutcome;
assert.equal(sharedTransportCloses, 0);
finishShared("fresh-health");
await eventOutcome;
assert.equal(await eventInspect, "fresh-health");
assert.equal(sharedTransportStarts, 1);

// A last caller timeout still cancels the underlying transport. A shared
// queued request survives an unrelated caller's timeout, but cancellation of
// its final caller prevents that queued operation from starting later.
const sharedPool = new RequestConcurrencyGate(1);
const queuedSharedRequests = new KeyedRequestGate(operation => sharedPool.run(operation));
let finishHeld;
const heldSlot = sharedPool.run(() => new Promise(resolveResponse => { finishHeld = resolveResponse; }));
let queuedStarts = 0;
const queuedOperation = () => { queuedStarts++; return Promise.resolve("queued-health"); };
const shortQueued = queuedSharedRequests.request("queued-container", {}, queuedOperation, () => true);
const longQueued = queuedSharedRequests.request("queued-container", {}, queuedOperation, () => true);
const shortOutcome = assert.rejects(shortQueued, /timeout/);
shortQueued.close("timeout");
await shortOutcome;
assert.equal(await shortQueued.started, false);
assert.equal(sharedPool.pending.length, 1);
const longOutcome = assert.rejects(longQueued, /timeout/);
longQueued.close("timeout");
await longOutcome;
assert.equal(await longQueued.started, false);
assert.equal(sharedPool.pending.length, 0);
finishHeld();
await heldSlot;
assert.equal(queuedStarts, 0);

// A close callback can throw while its physical request is still draining.
// Both cancellation and later completion must settle cleanly, and another
// generation of the same container cannot consume a second free pool slot.
const drainingPool = new RequestConcurrencyGate(2);
const drainingKeys = new KeyedRequestGate(operation => drainingPool.run(operation));
let finishDraining;
let drainingStarts = 0;
let drainingActive = 0;
let maximumDrainingActive = 0;
const draining = drainingKeys.request("draining-container", { generation: 0 }, () => {
    drainingStarts++;
    drainingActive++;
    maximumDrainingActive = Math.max(maximumDrainingActive, drainingActive);
    const promise = new Promise(resolveResponse => {
        finishDraining = () => { drainingActive--; resolveResponse("obsolete"); };
    });
    promise.close = () => { throw new Error("bridge already closed"); };
    return promise;
}, metadata => metadata.generation === 0);
const drainingOutcome = assert.rejects(draining, /timeout/);
draining.close("timeout");
await drainingOutcome;
const afterDrain = drainingKeys.request("draining-container", { generation: 1 }, () => {
    drainingStarts++;
    drainingActive++;
    maximumDrainingActive = Math.max(maximumDrainingActive, drainingActive);
    drainingActive--;
    return Promise.resolve("current-health");
}, metadata => metadata.generation === 1);
await new Promise(resolveResponse => setImmediate(resolveResponse));
assert.equal(drainingPool.active, 1);
assert.equal(drainingStarts, 1);
finishDraining();
assert.equal(await withTimeout(afterDrain, 250, "replacement-blocked"), "current-health");
assert.equal(drainingStarts, 2);
assert.equal(maximumDrainingActive, 1);

// Joining an existing inspect must not renew its transport deadline. Use a
// virtual clock so the boundary is deterministic and independent of host load.
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
const transportTimers = new Map();
let clockMs = 0;
let timerId = 0;
globalThis.setTimeout = (callback, delay) => {
    const id = ++timerId;
    transportTimers.set(id, { callback, due: clockMs + delay });
    return id;
};
globalThis.clearTimeout = id => transportTimers.delete(id);
try {
    let finishTransport;
    let transportCloses = 0;
    const deadlineGate = new KeyedRequestGate(operation => operation(), 5000);
    const operation = () => {
        const promise = new Promise(resolveResponse => { finishTransport = resolveResponse; });
        promise.close = () => { transportCloses++; };
        return promise;
    };
    const first = deadlineGate.request("deadline-container", {}, operation, () => true);
    clockMs = 4000;
    const late = deadlineGate.request("deadline-container", {}, operation, () => true);
    const outcomes = [assert.rejects(first, /timeout/), assert.rejects(late, /timeout/)];
    assert.equal(transportTimers.size, 1);
    const timer = [...transportTimers.values()][0];
    assert.equal(timer.due, 5000);
    clockMs = 5000;
    timer.callback();
    await Promise.all(outcomes);
    assert.equal(transportCloses, 1);
    assert.equal(transportTimers.size, 0);
    // A canceled entry cannot be reused while its physical transport drains.
    const replacement = deadlineGate.request("deadline-container", {}, () => Promise.resolve("replacement"), () => true);
    finishTransport("late-obsolete-health");
    assert.equal(await replacement, "replacement");

    // A synchronous last-observer cancellation can precede the pool's start
    // notification. It must not install a timer or release capacity early.
    const pool = new RequestConcurrencyGate(1);
    const keyedPool = new KeyedRequestGate(operation => pool.run(operation), 5000);
    let finishCanceled;
    let canceledCloses = 0;
    const canceled = keyedPool.request("canceled-container", {}, () => {
        const promise = new Promise(resolveResponse => { finishCanceled = resolveResponse; });
        promise.close = () => { canceledCloses++; };
        return promise;
    }, () => true);
    const canceledOutcome = assert.rejects(canceled, /timeout/);
    canceled.close("timeout");
    await canceledOutcome;
    assert.equal(canceledCloses, 1);
    assert.equal(pool.active, 1);
    assert.equal(transportTimers.size, 0);
    let recoveredStarts = 0;
    const recovered = keyedPool.request("next-container", {}, () => {
        recoveredStarts++;
        return Promise.resolve("next-health");
    }, () => true);
    assert.equal(recoveredStarts, 0);
    finishCanceled();
    assert.equal(await recovered, "next-health");
    assert.equal(recoveredStarts, 1);
} finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
}

// Disconnecting an owner terminates all shared and queued observers, including
// a newer event generation; none may leak a result into a replacement context.
const disconnectGate = new KeyedRequestGate(operation => operation());
const oldOwner = {};
let disconnectedCloses = 0;
let obsoleteQueuedStarts = 0;
const disconnectOperation = () => {
    let rejectTransport;
    const promise = new Promise((resolveResponse, rejectResponse) => { rejectTransport = rejectResponse; });
    promise.close = problem => { disconnectedCloses++; rejectTransport(new Error(problem)); };
    return promise;
};
const disconnected = [0, 0, 1, 1].map(generation => disconnectGate.request(
    "owner-container", { con: oldOwner, generation },
    generation === 0 ? disconnectOperation : () => { obsoleteQueuedStarts++; return Promise.resolve("obsolete"); },
    metadata => metadata.con === oldOwner && metadata.generation === generation,
));
const disconnectedOutcomes = disconnected.map(request => assert.rejects(request, /owner-disconnected/));
disconnectGate.closeWhere(metadata => metadata.con === oldOwner, "owner-disconnected");
await Promise.all(disconnectedOutcomes);
assert.equal(disconnectedCloses, 1);
assert.equal(obsoleteQueuedStarts, 0);
assert.equal(await disconnectGate.request("owner-container", { con: {} }, () => Promise.resolve("new-owner"), () => false), "new-owner");

const supersedingGate = new KeyedRequestGate(operation => operation());
let finishEarlierGeneration;
const earlierGeneration = supersedingGate.request("event-container", { generation: 0 },
    () => new Promise(resolveResponse => { finishEarlierGeneration = resolveResponse; }), () => false);
let supersededStarts = 0;
const supersededObservers = [1, 1].map(generation => supersedingGate.request("event-container", { generation },
    () => { supersededStarts++; return Promise.resolve("obsolete"); }, metadata => metadata.generation === generation));
const supersededOutcomes = supersededObservers.map(request => assert.rejects(request, /superseded/));
const newestGeneration = supersedingGate.request("event-container", { generation: 2 },
    () => Promise.resolve("newest-health"), metadata => metadata.generation === 2);
await Promise.all(supersededOutcomes);
assert.deepEqual(await Promise.all(supersededObservers.map(request => request.started)), [false, false]);
finishEarlierGeneration("old-health");
await earlierGeneration;
assert.equal(await newestGeneration, "newest-health");
assert.equal(supersededStarts, 0);
await new Promise(resolveResponse => setImmediate(resolveResponse));
assert.equal(supersedingGate.states.size, 0);
assert.equal(drainingKeys.states.size, 0);
assert.equal(drainingPool.active, 0);

const capacityGate = new RequestConcurrencyGate(8);
let activeRequests = 0;
let maxActiveRequests = 0;
const capacityRuns = Array.from({ length: 156 }, (_, index) => capacityGate.run(async () => {
    activeRequests++;
    maxActiveRequests = Math.max(maxActiveRequests, activeRequests);
    await new Promise(resolveResponse => setTimeout(resolveResponse, index % 3));
    activeRequests--;
    return index;
}));
assert.deepEqual(await Promise.all(capacityRuns), Array.from({ length: 156 }, (_, index) => index));
assert.ok(maxActiveRequests <= 8);

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
