/* SPDX-License-Identifier: LGPL-2.1-or-later */

/*
 * Keep health interpretation independent from React.  Apart from making the
 * row renderer easier to audit, this gives the browser tests a deterministic
 * contract for missing, stale, and collection-error states.
 */

export const healthStates = Object.freeze({
    healthy: "healthy",
    unhealthy: "unhealthy",
    starting: "starting",
    missing: "missing",
    stale: "stale",
    unknown: "unknown",
    error: "error",
    stopped: "stopped",
    completed: "completed",
});

// Error details from Podman, systemd, and Cockpit can contain command lines,
// URLs, or health output.  The display only needs an allowlisted reason; the
// existing health-log detail view remains the place for an operator who has
// deliberately opened the raw Podman log.
const HEALTH_COLLECTION_ERROR = "health-collection-failed";
const SCHEDULER_ERROR = "scheduler-metadata-unavailable";

const durationToMilliseconds = duration => {
    if (!Number.isFinite(duration) || duration <= 0)
        return 0;

    // Podman reports health timing fields as nanoseconds.
    return duration / 1000000;
};

const timestampToMilliseconds = timestamp => {
    if (timestamp instanceof Date)
        return timestamp.getTime();
    if (typeof timestamp === "number") {
        // Accept milliseconds for fixtures while handling Unix seconds too.
        return timestamp < 100000000000 ? timestamp * 1000 : timestamp;
    }
    if (typeof timestamp !== "string" || timestamp.length === 0)
        return null;

    const parsed = Date.parse(timestamp);
    return Number.isNaN(parsed) ? null : parsed;
};

export const healthConfig = container => container?.Config?.Healthcheck ?? container?.Config?.Health ?? null;

export const hasHealthCheck = container => {
    const config = healthConfig(container);
    if (!config || typeof config !== "object" || Object.keys(config).length === 0)
        return false;

    const test = config.Test;
    // Podman's health configuration is only actionable when Test is a
    // non-empty argv array.  Treat partial/malformed objects as missing so an
    // interval-only response can never produce a configured or passing badge.
    return Array.isArray(test) && test.length > 0 && test[0] !== "NONE" &&
        test.every(item => typeof item === "string" && item.length > 0);
};

export const healthState = container => container?.State?.Health ?? container?.State?.Healthcheck ?? null;

export const healthLogTime = log => timestampToMilliseconds(log?.End) ?? timestampToMilliseconds(log?.Start);

export const lastHealthCheck = state => {
    const logs = Array.isArray(state?.Log) ? state.Log : [];
    return logs.reduce((last, log) => {
        const timestamp = healthLogTime(log);
        return timestamp !== null && (last === null || timestamp > last) ? timestamp : last;
    }, null);
};

// Podman may retain State.Health.Status as "healthy" while a later retry is
// still represented by a failed log entry. Select the log by its timestamp,
// rather than trusting the API's array order, so the row reflects the latest
// observed health run.
export const latestHealthLog = state => {
    const logs = Array.isArray(state?.Log) ? state.Log : [];
    return logs.reduce((latest, log) => {
        const timestamp = healthLogTime(log);
        if (timestamp === null)
            return latest;
        if (!latest || timestamp >= latest.timestamp)
            return { log, timestamp };
        return latest;
    }, null)?.log || null;
};

const isTrueMarker = value => value === true ||
    (typeof value === "string" && ["1", "true", "yes"].includes(value.toLowerCase()));

// Podman exposes the exit code for every exited container, but an exit code of
// zero does not say that a workload was a one-shot job.  Only classify an
// exited container as completed when the inspect payload carries explicit
// one-shot metadata.  The direct fields also support normalized API fixtures;
// the labels cover the common Compose and Kubernetes sources that persist this
// distinction in Podman inspect output.
export const isCompletedContainer = container => {
    const state = container?.State;
    const config = container?.Config;
    const labels = config?.Labels || container?.Labels || {};
    return isTrueMarker(container?.Completed) ||
        isTrueMarker(container?.OneShot) ||
        isTrueMarker(state?.Completed) ||
        isTrueMarker(state?.OneShot) ||
        isTrueMarker(config?.Completed) ||
        isTrueMarker(config?.OneShot) ||
        isTrueMarker(labels["com.docker.compose.oneoff"]);
};

export const observedHealthInterval = state => {
    const logs = (Array.isArray(state?.Log) ? state.Log : [])
            .map(healthLogTime)
            .filter(timestamp => timestamp !== null)
            .sort((a, b) => a - b);
    if (logs.length < 2)
        return null;

    const intervals = [];
    for (let index = 1; index < logs.length; index++) {
        const interval = logs[index] - logs[index - 1];
        if (interval > 0)
            intervals.push(interval);
    }
    if (intervals.length === 0)
        return null;

    // Keep the observed cadence available for diagnostics. It never drives a
    // stale decision: external schedules can be irregular or have no second
    // run yet, so the scheduler collector is the freshness authority.
    intervals.sort((a, b) => a - b);
    return intervals[Math.floor(intervals.length / 2)];
};

const schedulerError = scheduler => {
    if (!scheduler)
        return null;

    const errors = Array.isArray(scheduler.collector_errors)
        ? scheduler.collector_errors
        : [];
    const coverageStatus = scheduler.coverage_status;
    const coverageCount = scheduler.active_coverage_count;
    if (coverageStatus !== undefined &&
        !["covered", "covered-with-errors", "uncovered", "error"].includes(coverageStatus))
        return SCHEDULER_ERROR;
    if (scheduler.error)
        return SCHEDULER_ERROR;
    if (coverageStatus === "error" || coverageStatus === "covered-with-errors")
        return SCHEDULER_ERROR;
    if (coverageCount !== undefined &&
        (!Number.isInteger(coverageCount) || coverageCount < 0 || coverageCount > 1))
        return SCHEDULER_ERROR;
    if (errors.length > 0)
        return SCHEDULER_ERROR;
    return null;
};

const schedulerFreshness = (config, scheduler) => {
    const effective = scheduler;
    if (!effective)
        return {
            staleAfterMs: null,
            cadenceSource: "scheduler-unavailable",
            schedulerError: SCHEDULER_ERROR,
            reason: "scheduler-unavailable",
        };

    const error = schedulerError(effective);
    if (effective.coverage_status === "uncovered" || effective.active_coverage_count === 0)
        return {
            staleAfterMs: null,
            cadenceSource: effective.effective_interval_source || "scheduler-coverage-missing",
            schedulerError: error,
            reason: "scheduler-coverage-missing",
        };
    const interval = Number(effective.effective_interval_seconds);
    if (!Number.isFinite(interval) || interval <= 0)
        return {
            staleAfterMs: null,
            cadenceSource: effective.effective_interval_source || "scheduler-unknown",
            schedulerError: error || (effective.coverage_status === "covered" ? SCHEDULER_ERROR : null),
            reason: effective.coverage_status === "uncovered" ? "scheduler-coverage-missing" : "scheduler-unknown",
        };

    const jitter = Number(effective.jitter_seconds);
    const accuracy = Number(effective.accuracy_seconds);
    const timeout = durationToMilliseconds(config?.Timeout);
    const jitterMs = Number.isFinite(jitter) && jitter >= 0 ? jitter * 1000 : 0;
    const accuracyMs = Number.isFinite(accuracy) && accuracy >= 0 ? accuracy * 1000 : 0;
    const source = effective.schedule_source || effective.effective_interval_source || "systemd";
    return {
        staleAfterMs: error ? null : Math.max(1000, interval * 1000 + jitterMs + accuracyMs + timeout),
        cadenceSource: source,
        schedulerError: error,
        reason: "schedule-known",
    };
};

export const staleAfter = (config, _state, scheduler = null) => schedulerFreshness(config, scheduler).staleAfterMs;

export const healthAssessment = (container, now = Date.now(), collectionError = null, scheduler = null) => {
    const config = healthConfig(container);
    const state = healthState(container);
    const lastChecked = lastHealthCheck(state);
    const latestLog = latestHealthLog(state);
    const parsedLatestExitCode = latestLog === null ? null : Number(latestLog.ExitCode);
    const latestExitCode = Number.isFinite(parsedLatestExitCode) ? parsedLatestExitCode : null;
    const ageMs = lastChecked === null ? null : Math.max(0, now - lastChecked);
    const effectiveScheduler = scheduler;
    const freshness = schedulerFreshness(config, effectiveScheduler);
    const staleAfterMs = freshness.staleAfterMs;
    const freshWindowMs = null;
    const nativeStatus = state?.Status || null;

    const base = extra => ({
        rawStatus: nativeStatus,
        latestExitCode,
        configured: hasHealthCheck(container),
        lastChecked,
        ageMs,
        staleAfterMs,
        freshWindowMs,
        cadenceSource: freshness.cadenceSource,
        schedule: effectiveScheduler || null,
        ...extra,
    });

    if (collectionError) {
        return base({
            status: healthStates.error,
            reason: "collection-error",
            error: HEALTH_COLLECTION_ERROR,
        });
    }

    const lifecycle = String(container?.State?.Status || "").toLowerCase();
    if (lifecycle && lifecycle !== "running") {
        const completed = lifecycle === "exited" && Number(container?.State?.ExitCode) === 0 &&
            isCompletedContainer(container);
        return base({
            status: completed ? healthStates.completed : healthStates.stopped,
            reason: completed ? "completed" : "stopped",
        });
    }

    if (!hasHealthCheck(container)) {
        return base({
            status: healthStates.missing,
            configured: false,
            reason: "missing",
        });
    }

    if (nativeStatus === "unhealthy") {
        return base({
            status: healthStates.unhealthy,
            reason: "unhealthy",
        });
    }

    if (nativeStatus === "starting") {
        return base({
            status: healthStates.starting,
            reason: "starting",
        });
    }

    // A transient failed run is still a confirmed failure even if Podman's
    // aggregate status has not crossed its retry threshold yet. Keep
    // rawStatus above so callers can distinguish the native aggregate state
    // from this latest-run result.
    if (latestExitCode !== null && latestExitCode !== 0) {
        return base({
            status: healthStates.unhealthy,
            reason: "latest-check-failed",
        });
    }

    if (freshness.schedulerError) {
        return base({
            status: healthStates.error,
            reason: "scheduler-error",
            error: SCHEDULER_ERROR,
        });
    }

    if (nativeStatus === "healthy" && lastChecked !== null && staleAfterMs !== null && ageMs > staleAfterMs) {
        return base({
            status: healthStates.stale,
            reason: "stale",
        });
    }

    if (nativeStatus === "healthy" && lastChecked !== null && staleAfterMs !== null) {
        return base({
            status: healthStates.healthy,
            reason: "healthy",
        });
    }

    return base({
        status: healthStates.unknown,
        reason: nativeStatus === "healthy" ? freshness.reason : "no-result",
    });
};
