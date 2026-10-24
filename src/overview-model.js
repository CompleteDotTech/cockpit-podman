/* SPDX-License-Identifier: LGPL-2.1-or-later */

// The image view uses container identity, name and running state. Health
// payloads and streamed statistics must not rebuild its entire table.
export const createImageContainerSelector = () => {
    let previous = new Map();
    let result = null;
    return containers => {
        if (containers === null) {
            previous.clear();
            result = null;
            return null;
        }
        const entries = Object.entries(containers);
        const next = new Map();
        let changed = result === null || entries.length !== previous.size;
        for (const [key, container] of entries) {
            const imageKey = `${container.uid ?? "user"}-${container.Image}`;
            const old = previous.get(key);
            const status = container.State?.Status;
            const same = old && old.imageKey === imageKey && old.container.Id === container.Id &&
                old.container.Name === container.Name && old.container.State.Status === status;
            const entry = same
                ? old
                : {
                    imageKey,
                    container: { Id: container.Id, Name: container.Name, State: { Status: status } },
                };
            next.set(key, entry);
            changed ||= !same;
        }
        previous = next;
        if (changed) {
            result = {};
            for (const entry of next.values()) {
                if (!result[entry.imageKey])
                    result[entry.imageKey] = [];
                result[entry.imageKey].push({ container: entry.container });
            }
        }
        return result;
    };
};

// Container dialogs need search fields on images. Keep that derived view
// stable between image updates without mutating the shared API records.
export const createLocalImagesSelector = imageName => {
    let previous;
    let result = null;
    return images => {
        if (images !== previous) {
            previous = images;
            result = images === null
                ? null
                : Object.values(images).map(image => ({
                    ...image,
                    Index: image.RepoTags?.[0] ? image.RepoTags[0].split('/')[0] : "",
                    Name: imageName(image),
                    toString() { return this.Name },
                }));
        }
        return result;
    };
};
