/* SPDX-License-Identifier: LGPL-2.1-or-later */
import React, { useLayoutEffect, useMemo, useRef } from 'react';

import { DialogsContext, useDialogs } from 'dialogs.jsx';

// WithDialogs creates a new context object when its children change. Keep
// list consumers stable across stats/health renders, while every action
// delegates to the current provider and dialog visibility remains reactive.
const StableDialogs = ({ children }) => {
    const dialogs = useDialogs();
    const current = useRef(dialogs);
    useLayoutEffect(() => { current.current = dialogs }, [dialogs]);
    const active = dialogs.isActive();
    const value = useMemo(() => ({
        show: component => current.current.show(component),
        close: () => current.current.close(),
        run: (component, props) => current.current.run(component, props),
        isActive: () => active,
    }), [active]);
    return <DialogsContext.Provider value={value}>{children}</DialogsContext.Provider>;
};

export default StableDialogs;
