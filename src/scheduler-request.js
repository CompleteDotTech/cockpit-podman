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
    constructor(maxConcurrentOwners = 2) {
        super();
        this.running = new Map();
        this.pending = [];
        this.queued = new Map();
        this.maxConcurrentOwners = Number.isInteger(maxConcurrentOwners) && maxConcurrentOwners > 0
            ? maxConcurrentOwners
            : 1;
    }

    start(uid, operation) {
        const key = schedulerOwnerKey(uid);
        const running = this.running.get(key);
        const queued = this.queued.get(key);
        // Coalesce only the generation that is still current.  An invalidated
        // operation remains in flight so its late result can be discarded, but
        // a reconnect must still get a fresh operation queued behind it.
        if (running && this.isCurrent(uid, running.request))
            return running;
        if (queued && this.isCurrent(uid, queued.request))
            return queued;
        if (queued)
            this.queued.delete(key);

        const request = this.begin(uid);
        let resolveEntry;
        let rejectEntry;
        const promise = new Promise((resolve, reject) => {
            resolveEntry = resolve;
            rejectEntry = reject;
        });
        const entry = { key, request, operation, promise, resolve: resolveEntry, reject: rejectEntry };
        this.pending.push(entry);
        this.queued.set(key, entry);
        this.drain();
        return entry;
    }

    invalidate(uid) {
        return this.begin(uid);
    }

    drain() {
        while (this.running.size < this.maxConcurrentOwners && this.pending.length > 0) {
            // A fresh generation for an invalidated owner must wait for the
            // old generation to settle even when another global slot is free.
            // Search past blocked owners so unrelated contexts can continue.
            const index = this.pending.findIndex(entry => !this.running.has(entry.key));
            if (index < 0)
                break;
            const [entry] = this.pending.splice(index, 1);
            if (this.queued.get(entry.key) === entry)
                this.queued.delete(entry.key);
            // A connection can disappear while an owner is waiting for a
            // global slot. Resolve the abandoned request without running its
            // operation; the next connection for this UID gets a new
            // generation and cannot be blocked by stale work.
            if (!this.isCurrent(entry.key, entry.request)) {
                entry.resolve(undefined);
                continue;
            }

            this.running.set(entry.key, entry);
            Promise.resolve()
                    .then(() => entry.operation(entry.request))
                    .then(entry.resolve, entry.reject)
                    .finally(() => {
                        if (this.running.get(entry.key)?.request === entry.request)
                            this.running.delete(entry.key);
                        this.drain();
                    });
        }
    }
}

export const withTimeout = (promise, timeoutMs, message, onTimeout = null) => {
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0)
        return Promise.reject(new Error(message));

    let timer;
    const timeout = new Promise((resolve, reject) => {
        timer = setTimeout(() => {
            try {
                onTimeout?.();
            } catch {
                // Timeout cleanup is best effort; the timeout result remains
                // deterministic even if a bridge channel is already closed.
            }
            reject(new Error(message));
        }, timeoutMs);
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
