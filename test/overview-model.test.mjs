/* SPDX-License-Identifier: LGPL-2.1-or-later */
import assert from "node:assert/strict";

import { createImageContainerSelector, createLocalImagesSelector } from "../src/overview-model.js";
import { orderHealthRefresh } from "../src/scheduler-request.js";

const selectContainers = createImageContainerSelector();
assert.equal(selectContainers(null), null);
const empty = selectContainers({});
assert.deepEqual(empty, {});
assert.equal(selectContainers({}), empty);

const root = { uid: 0, Id: "same-id", Name: "root", Image: "same-image", State: { Status: "running" } };
const user = { ...root, uid: null, Name: "user" };
const source = { "0-same-id": root, "user-same-id": user };
const selected = selectContainers(source);
assert.deepEqual(Object.keys(selected).sort(), ["0-same-image", "user-same-image"]);
assert.equal(selected["0-same-image"][0].container.Name, "root");
assert.equal(selected["user-same-image"][0].container.Name, "user");

// Health/config/statistics changes leave image rows untouched. Snapshot
// projected values so mutation of an API record cannot fool the comparison.
const changedHealth = { ...root, State: { Status: "running", Health: { Status: "unhealthy" } },
    Config: { Healthcheck: { Test: ["CMD", "probe"] } }, stats: { Cpu: 99 } };
assert.equal(selectContainers({ ...source, "0-same-id": changedHealth }), selected);
assert.equal(selectContainers({ "user-same-id": user, "0-same-id": changedHealth }), selected);
changedHealth.Name = "renamed";
const renamed = selectContainers({ ...source, "0-same-id": changedHealth });
assert.notEqual(renamed, selected);
assert.equal(selected["0-same-image"][0].container.Name, "root");
assert.equal(renamed["0-same-image"][0].container.Name, "renamed");

const stopped = selectContainers({ ...source, "0-same-id": { ...root, State: { Status: "exited" } } });
assert.equal(stopped["0-same-image"][0].container.State.Status, "exited");
const moved = selectContainers({ ...source, "0-same-id": { ...root, uid: 2000, Image: "new-image" } });
assert.equal(moved["0-same-image"], undefined);
assert.equal(moved["2000-new-image"][0].container.Id, root.Id);
const removed = selectContainers({ "user-same-id": user });
assert.deepEqual(Object.keys(removed), ["user-same-image"]);
const added = selectContainers({ ...source, other: { ...root, Id: "other-id" } });
assert.equal(added["0-same-image"].length, 2);
assert.equal(selectContainers(null), null);
assert.deepEqual(selectContainers({}), {});

let imageNameCalls = 0;
const selectImages = createLocalImagesSelector(image => {
    imageNameCalls++;
    return image.RepoTags?.[0] || "untagged";
});
assert.equal(selectImages(null), null);
const image = Object.freeze({ Id: "image", RepoTags: Object.freeze(["registry/image:tag"]) });
const untagged = Object.freeze({ Id: "untagged" });
const images = Object.freeze({ image, untagged });
const local = selectImages(images);
assert.equal(local[0].Index, "registry");
assert.equal(String(local[0]), "registry/image:tag");
assert.equal(local[1].Index, "");
assert.equal(local[1].Name, "untagged");
assert.equal(image.Name, undefined);
assert.equal(image.Index, undefined);
assert.equal(selectImages(images), local);
assert.equal(imageNameCalls, 2);
assert.notEqual(selectImages({ ...images, image: { ...image, RepoTags: ["other/image:tag"] } }), local);
assert.equal(selectImages(null), null);

const inventory = Array.from({ length: 10 }, (_, index) => ({
    Id: String(index).padStart(64, "0"), healthDetailsLoaded: index % 2 === 0,
    // Persistent failure must not earn priority over every successful row.
    collectionError: index % 3 === 0,
}));
const originalOrder = inventory.map(container => container.Id);
let cursor;
const visits = new Map();
for (let pass = 0; pass < 20; pass++) {
    const input = pass % 2 ? [...inventory].reverse() : inventory;
    const ordered = orderHealthRefresh(input, cursor);
    // Model a finite pass that can schedule only three eligible rows.
    for (const container of ordered.slice(0, 3)) {
        cursor = container.Id;
        const previous = visits.get(cursor) || [];
        if (previous.length)
            assert.ok(pass - previous.at(-1) <= 4);
        previous.push(pass);
        visits.set(cursor, previous);
    }
}
assert.equal(visits.size, inventory.length);
assert.deepEqual([...visits.values()].map(passes => passes.length), Array(10).fill(6));
assert.deepEqual(inventory.map(container => container.Id), originalOrder);
const withoutCursor = inventory.filter(container => container.Id !== inventory[4].Id);
assert.equal(orderHealthRefresh(withoutCursor, inventory[4].Id)[0], inventory[5]);
assert.equal(orderHealthRefresh(inventory, inventory[9].Id)[0], inventory[0]);
assert.deepEqual(orderHealthRefresh([], cursor), []);
// Owner cursors are separate inputs; another owner's ring starts at its own
// successor even when both contexts have the same full container IDs.
assert.equal(orderHealthRefresh(inventory, inventory[1].Id)[0], inventory[2]);
assert.equal(orderHealthRefresh(inventory, inventory[7].Id)[0], inventory[8]);

console.log("overview-model fixture: PASS");
