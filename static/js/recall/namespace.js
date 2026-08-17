/* Recall — global namespace. Loaded first; everything attaches here.
   No build step, no ES modules — plain globals under window.Recall. */
(function () {
    'use strict';
    window.Recall = window.Recall || {
        version: '1.0',
        routes: [],          // filled by router.js
        views: {},           // filled by views/*.js  (id -> {render, destroy})
        api: {},             // filled by api.js
        util: {},            // filled by util.js
        ui: {},              // filled by ui.js
        shell: {},           // filled by shell.js
        cmdk: {},            // filled by cmdk.js
        router: {},          // filled by router.js
        state: {             // tiny shared cache (session-scoped)
            categories: null,
            stats: null,
        },
    };
})();
