/* Recall — transcript version compare (pure, no DOM). Runs in the browser
   (window.Recall.compare) and under node (module.exports) for tests.
   align(a, b): two segment lists -> rows [{a: cell|null, b: cell|null}] by time overlap.
   diffWords(x, y): word-level LCS diff -> {a: [{text, diff}], b: [{text, diff}]}. */
(function () {
    'use strict';

    function segEnd(segs, i) {
        var s = segs[i];
        if (s.end != null) return s.end;
        return segs[i + 1] ? segs[i + 1].start : (s.start || 0) + 5;
    }

    function norm(segs) {
        return (segs || []).map(function (s, i) {
            return { start: s.start || 0, end: segEnd(segs, i), text: s.text || '' };
        }).sort(function (x, y) { return x.start - y.start; });
    }

    function cell(list) {
        if (!list.length) return null;
        return {
            start: list[0].start,
            end: list.reduce(function (m, s) { return Math.max(m, s.end); }, 0),
            text: list.map(function (s) { return String(s.text).trim(); }).join(' ').trim(),
        };
    }

    // Cluster segments of both sides whose time ranges chain-overlap; each cluster is one row.
    function align(a, b) {
        var A = norm(a), B = norm(b), i = 0, j = 0, rows = [];
        var ca = [], cb = [], clusterEnd = -Infinity;
        function flush() {
            if (ca.length || cb.length) rows.push({ a: cell(ca), b: cell(cb) });
            ca = []; cb = []; clusterEnd = -Infinity;
        }
        while (i < A.length || j < B.length) {
            var takeA = j >= B.length || (i < A.length && A[i].start <= B[j].start);
            var s = takeA ? A[i++] : B[j++];
            if ((ca.length || cb.length) && s.start >= clusterEnd - 1e-6) flush();
            (takeA ? ca : cb).push(s);
            clusterEnd = Math.max(clusterEnd, s.end);
        }
        flush();
        return rows;
    }

    function key(w) {
        return w.toLowerCase().replace(/[.,!?;:"'`«»“”„()\[\]\-—–…]/g, '');
    }

    function diffWords(x, y) {
        var wa = String(x || '').split(/\s+/).filter(Boolean);
        var wb = String(y || '').split(/\s+/).filter(Boolean);
        var ka = wa.map(key), kb = wb.map(key);
        var n = wa.length, m = wb.length, k, l;
        var dp = [];
        for (k = 0; k <= n; k++) { dp.push(new Array(m + 1).fill(0)); }
        for (k = n - 1; k >= 0; k--) {
            for (l = m - 1; l >= 0; l--) {
                dp[k][l] = ka[k] === kb[l] ? dp[k + 1][l + 1] + 1 : Math.max(dp[k + 1][l], dp[k][l + 1]);
            }
        }
        var ra = wa.map(function (w) { return { text: w, diff: true }; });
        var rb = wb.map(function (w) { return { text: w, diff: true }; });
        k = 0; l = 0;
        while (k < n && l < m) {
            if (ka[k] === kb[l]) { ra[k].diff = false; rb[l].diff = false; k++; l++; }
            else if (dp[k + 1][l] >= dp[k][l + 1]) k++;
            else l++;
        }
        return { a: ra, b: rb };
    }

    var api = { align: align, diffWords: diffWords };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (typeof window !== 'undefined' && window.Recall) window.Recall.compare = api;
})();
