/* SPDX-License-Identifier: LGPL-2.1-or-later */

// Cockpit refreshes can overlap when a Podman event arrives during an
// inventory refresh.  Keep one monotonically increasing generation per owner
// context so a late response cannot replace newer coverage.

export const schedulerOwnerKey = uid => uid === null ? "user" : String(uid);

export class SchedulerRequestGate {
    constructor() {
        this.generations = new Map();
    }

    begin(uid) {
        const key = schedulerOwnerKey(uid);
        const generation = (this.generations.get(key) || 0) + 1;
        this.generations.set(key, generation);
        return { key, generation };
    }

    isCurrent(uid, request) {
        return Boolean(request) && request.key === schedulerOwnerKey(uid) &&
            this.generations.get(request.key) === request.generation;
    }
}

// A recurring owner refresh must not start a second Podman/API or scheduler
// collection while the previous one is still in flight.  Invalidating an
// owner advances its generation without cancelling the underlying promise;
// callers can then discard the late result safely.
export class OwnerRefreshGate extends SchedulerRequestGate {
    constructor() {
        super();
        this.running = new Map();
    }

    start(uid, operation) {
        const key = schedulerOwnerKey(uid);
        const current = this.running.get(key);
        if (current)
            return current;

        const request = this.begin(uid);
        const promise = Promise.resolve()
                .then(() => operation(request))
                .finally(() => {
                    if (this.running.get(key)?.request === request)
                        this.running.delete(key);
                });
        const entry = { key, request, promise };
        this.running.set(key, entry);
        return entry;
    }

    invalidate(uid) {
        return this.begin(uid);
    }
}

export const withTimeout = (promise, timeoutMs, message) => {
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0)
        return Promise.reject(new Error(message));

    let timer;
    const timeout = new Promise((resolve, reject) => {
        timer = setTimeout(() => reject(new Error(message)), timeoutMs);
    });
    return Promise.race([Promise.resolve(promise), timeout])
            .finally(() => clearTimeout(timer));
};

export const mapWithConcurrency = async (items, worker, concurrency) => {
    const results = new Array(items.length);
    let nextIndex = 0;
    const run = async () => {
        while (nextIndex < items.length) {
            const index = nextIndex++;
            results[index] = await worker(items[index], index);
        }
    };
    const requestedWorkers = Number.isFinite(concurrency) ? Math.floor(concurrency) : 1;
    const workers = Math.min(Math.max(1, requestedWorkers), items.length);
    await Promise.all(Array.from({ length: workers }, run));
    return results;
};
