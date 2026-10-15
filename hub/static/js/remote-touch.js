// Touch input for the remote viewer (roadmap #2, issue #133): a phone driving a remote PC.
//
// remote.js only ever spoke mouse. On a phone that left two things broken at once. Pinch did
// nothing useful -- at best the browser zoomed the whole framed page -- so a 1080p desktop
// stayed squeezed into a 390px-wide picture. And a tap became a synthesized click AT THE
// FINGER, which on that picture is a forty-pixel blob over icons ten pixels wide: the close
// box, a tray icon or a screen corner could not be hit on purpose at all.
//
// So a touch drives a VIRTUAL CURSOR, the way Microsoft's Remote Desktop app does in its
// "mouse pointer" mode -- the owner's choice on the issue. The finger is a touchpad, not a
// pointer:
//   * one finger dragged moves the cursor by however far the finger moved, so the finger
//     never covers what it is aiming at and a corner is reachable from anywhere;
//   * a tap clicks at the CURSOR, not at the finger; a two-finger tap right-clicks;
//   * press and hold still, then drag, holds the left button down (select, move a window);
//   * pinch zooms the PICTURE about the fingers, and two fingers dragged pan it. The zoom is
//     local -- a CSS transform on the <video> -- and nothing is sent to the PC for it.
//
// Rejected: tap-where-you-touch plus pinch-zoom alone ("direct touch"). It is what the
// browser does already, and it only becomes usable at a zoom where the operator spends the
// session panning; the cursor is what makes small targets hittable at ANY zoom. Also
// rejected: tap-then-touch-again to drag, which is the laptop touchpad gesture. Two taps
// close together must stay a DOUBLE-CLICK -- it is how a desktop icon is opened -- and the
// only way to tell the two apart is to hold every tap back for the double-click window, which
// would put a third of a second of lag on every click of the session.
//
// Why the zoom is a transform rather than a bigger element: remote.js maps a mouse click
// through getBoundingClientRect(), and a transformed element reports its TRANSFORMED box --
// so a mouse on a zoomed picture (an iPad with a trackpad) still lands on the pixel under it
// with no change to that mapping.
//
// Exposed as window.RemoteTouch.create(...), a factory like RemoteRelay and RemoteRecorder.
// The geometry is pure and exported on its own (RemoteTouch.geometry) so it can be checked
// without a browser -- see tests/test_remote_touch.py.
(function () {
    'use strict';

    const MAX_ZOOM = 5;
    // A touch that ends this soon, having moved less than TAP_SLOP_PX, is a tap. Ten pixels is
    // about a quarter of a fingertip: less and a tap with a slightly rolling finger becomes a
    // tiny cursor nudge that clicks nothing.
    const TAP_MS = 300;
    const TAP_SLOP_PX = 10;
    // Hold this long without moving and the left button goes down for a drag.
    const HOLD_MS = 500;
    const MOVE_THROTTLE_MS = 40;   // same budget as the mouse path in remote.js
    // iOS still fires the legacy mouse events for a tap, after the touch ones. remote.js's
    // mouse handlers ask recentlyTouched() and drop them, or every tap would click twice --
    // once at the cursor, and again at the finger.
    const MOUSE_AFTER_TOUCH_MS = 1000;
    // Keep the cursor at least this far inside the visible picture while zoomed, so moving it
    // towards an edge pans the view instead of losing the cursor off-screen.
    const FOLLOW_MARGIN_PX = 24;

    // ---- Geometry (pure) -------------------------------------------------------------------
    // Three coordinate spaces, all in CSS pixels relative to the video element's UNZOOMED box:
    //   remote   0..1 across the captured picture -- what the agent is sent;
    //   local    where that point is drawn before zoom (the picture is letterboxed by
    //            object-fit: contain, so this is NOT just remote * element size);
    //   screen   local after the view transform: view.tx + view.scale * local.

    /** Where the letterboxed picture sits inside a box of `size`, or null before video. */
    function pictureBox(size, vw, vh) {
        if (!vw || !vh || !size.w || !size.h) return null;
        const fit = Math.min(size.w / vw, size.h / vh);
        const w = vw * fit;
        const h = vh * fit;
        return { x: (size.w - w) / 2, y: (size.h - h) / 2, w, h };
    }

    /** Keep the zoomed element covering its box: no zooming out past 1, no panning the
     *  picture away from an edge to show the stage behind it. */
    function clampView(view, size) {
        const scale = Math.min(MAX_ZOOM, Math.max(1, view.scale));
        const clamp = (t, extent) => Math.min(0, Math.max(extent - extent * scale, t));
        return { scale, tx: clamp(view.tx, size.w), ty: clamp(view.ty, size.h) };
    }

    /** Zoom by `factor` keeping the point under (px, py) -- the pinch midpoint -- still. */
    function zoomAbout(view, size, factor, px, py) {
        const scale = Math.min(MAX_ZOOM, Math.max(1, view.scale * factor));
        const lx = (px - view.tx) / view.scale;
        const ly = (py - view.ty) / view.scale;
        return clampView({ scale, tx: px - lx * scale, ty: py - ly * scale }, size);
    }

    /** Pan just enough that local point (lx, ly) is on screen with `margin` to spare. */
    function followPoint(view, size, lx, ly, margin) {
        const pan = (t, l, extent) => {
            const at = t + view.scale * l;
            if (at < margin) return t + (margin - at);
            if (at > extent - margin) return t - (at - (extent - margin));
            return t;
        };
        return clampView({
            scale: view.scale,
            tx: pan(view.tx, lx, size.w),
            ty: pan(view.ty, ly, size.h),
        }, size);
    }

    // ---- The controller --------------------------------------------------------------------

    /** Wire touch input onto one viewer's stage.
     *
     *  opts.stage     the .remote-stage, which clips the zoomed video and holds the cursor
     *  opts.video     the <video> carrying the picture
     *  opts.cursor    the cursor element drawn over it
     *  opts.size      () => {w, h} of the captured picture, for before the video knows it
     *  opts.send      (msg) => void, the viewer's input path -- already view-only gated
     *  opts.onFirstTouch  () => void, once, so the viewer can say what the gestures do
     */
    function create(opts) {
        const { stage, video, cursor } = opts;
        let view = { scale: 1, tx: 0, ty: 0 };
        let pos = [0.5, 0.5];          // the cursor, in remote coordinates
        let lastTouch = -Infinity;
        let touched = false;
        let lastMove = 0;

        const pointers = new Map();    // pointerId -> {x, y}
        let gesture = null;

        function elementSize() {
            return { w: video.clientWidth, h: video.clientHeight };
        }

        function box() {
            const captured = opts.size();
            return pictureBox(elementSize(),
                video.videoWidth || captured.w, video.videoHeight || captured.h);
        }

        function render() {
            video.style.transformOrigin = '0 0';
            video.style.transform = view.scale === 1 && !view.tx && !view.ty ? ''
                : `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})`;
            const b = box();
            cursor.hidden = !touched || !b;
            if (cursor.hidden) return;
            // The cursor is a sibling of the video inside the stage, so it is placed in stage
            // coordinates: the video's own offset, then the view transform of the local point.
            const lx = b.x + pos[0] * b.w;
            const ly = b.y + pos[1] * b.h;
            cursor.style.left = `${video.offsetLeft + view.tx + view.scale * lx}px`;
            cursor.style.top = `${video.offsetTop + view.ty + view.scale * ly}px`;
        }

        function stagePoint(p) {
            const rect = stage.getBoundingClientRect();
            return { x: p.x - rect.left - video.offsetLeft, y: p.y - rect.top - video.offsetTop };
        }

        function sendMove(force) {
            const now = performance.now();
            if (!force && now - lastMove < MOVE_THROTTLE_MS) return;
            lastMove = now;
            opts.send({ t: 'm', x: pos[0], y: pos[1] });
        }

        function button(kind, b) {
            sendMove(true);   // the PC's own pointer must be where the click is
            opts.send({ t: kind, b, x: pos[0], y: pos[1] });
        }

        /** Move the cursor by a finger's travel in screen pixels. 1:1 with what is on screen,
         *  so at 3x zoom the same swipe is a third of the distance on the PC -- the precision
         *  the zoom was for. */
        function moveCursor(dx, dy) {
            const b = box();
            if (!b) return;
            pos = [
                Math.min(1, Math.max(0, pos[0] + dx / (b.w * view.scale))),
                Math.min(1, Math.max(0, pos[1] + dy / (b.h * view.scale))),
            ];
            if (view.scale > 1) {
                view = followPoint(view, elementSize(),
                    b.x + pos[0] * b.w, b.y + pos[1] * b.h, FOLLOW_MARGIN_PX);
            }
            render();
            sendMove(false);
        }

        function spread() {
            const [a, b] = Array.from(pointers.values());
            return {
                dist: Math.hypot(a.x - b.x, a.y - b.y) || 1,
                mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
            };
        }

        function clearHold() {
            if (gesture && gesture.holdTimer) {
                clearTimeout(gesture.holdTimer);
                gesture.holdTimer = null;
            }
        }

        function onDown(e) {
            if (e.pointerType !== 'touch') return;
            e.preventDefault();
            lastTouch = performance.now();
            if (!touched) {
                touched = true;
                render();
                if (opts.onFirstTouch) opts.onFirstTouch();
            }
            try { video.setPointerCapture(e.pointerId); } catch (err) { /* already gone */ }
            pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

            if (pointers.size === 1) {
                gesture = {
                    start: performance.now(), startX: e.clientX, startY: e.clientY,
                    fingers: 1, moved: false, holding: false, holdTimer: null,
                };
                const g = gesture;
                g.holdTimer = setTimeout(() => {
                    g.holdTimer = null;
                    if (gesture !== g || g.moved || g.fingers !== 1) return;
                    g.holding = true;
                    button('d', 0);
                }, HOLD_MS);
            } else if (pointers.size === 2 && gesture) {
                // A second finger turns whatever the first was doing into a pinch. A drag
                // already holding the button lets go first, or the pinch would drag with it.
                clearHold();
                if (gesture.holding) { button('u', 0); gesture.holding = false; }
                const s = spread();
                gesture.fingers = 2;
                gesture.pinch = { view, dist: s.dist, mid: stagePoint(s.mid) };
            }
        }

        function onMove(e) {
            if (e.pointerType !== 'touch' || !pointers.has(e.pointerId) || !gesture) return;
            e.preventDefault();
            lastTouch = performance.now();
            const prev = pointers.get(e.pointerId);
            pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

            if (Math.hypot(e.clientX - gesture.startX, e.clientY - gesture.startY) > TAP_SLOP_PX) {
                gesture.moved = true;
                if (!gesture.holding) clearHold();
            }
            if (gesture.fingers === 1) {
                moveCursor(e.clientX - prev.x, e.clientY - prev.y);
            } else if (pointers.size === 2 && gesture.pinch) {
                const s = spread();
                const mid = stagePoint(s.mid);
                const p = gesture.pinch;
                // The slop test above only watches the first finger; a pinch that moves only
                // the second must not end as a two-finger tap (a right-click).
                if (Math.abs(s.dist - p.dist) > TAP_SLOP_PX
                        || Math.hypot(mid.x - p.mid.x, mid.y - p.mid.y) > TAP_SLOP_PX) {
                    gesture.moved = true;
                }
                const zoomed = zoomAbout(p.view, elementSize(), s.dist / p.dist, p.mid.x, p.mid.y);
                view = clampView({
                    scale: zoomed.scale,
                    tx: zoomed.tx + (mid.x - p.mid.x),
                    ty: zoomed.ty + (mid.y - p.mid.y),
                }, elementSize());
                render();
            }
            // Pointers left over from a pinch (one finger lifted, one still down) do nothing
            // until every finger is up: a pinch ending must not fling the cursor.
        }

        function onUp(e) {
            if (e.pointerType !== 'touch' || !pointers.has(e.pointerId)) return;
            e.preventDefault();
            lastTouch = performance.now();
            pointers.delete(e.pointerId);
            if (pointers.size || !gesture) return;

            clearHold();
            const g = gesture;
            gesture = null;
            const quick = performance.now() - g.start < TAP_MS;
            if (g.holding) {
                button('u', 0);
            } else if (e.type === 'pointercancel' || g.moved || !quick) {
                // A swipe, a pinch, or the browser took the touch back: no click. But the last
                // few moves of a swipe usually fell inside the throttle, so the PC's pointer
                // would stop short of the cursor drawn here -- send where it ended.
                if (g.fingers === 1) sendMove(true);
            } else if (g.fingers === 2) {
                button('d', 2);
                button('u', 2);
            } else {
                button('d', 0);
                button('u', 0);
            }
        }

        // touch-action: none on the stage (machine.css) is what stops the browser panning or
        // zooming the page under a gesture. iOS Safari has honoured it only partly over the
        // years, so the raw touch and gesture events are refused as well.
        const refuse = (e) => e.preventDefault();
        video.addEventListener('pointerdown', onDown);
        video.addEventListener('pointermove', onMove);
        video.addEventListener('pointerup', onUp);
        video.addEventListener('pointercancel', onUp);
        stage.addEventListener('touchmove', refuse, { passive: false });
        stage.addEventListener('gesturestart', refuse);

        // The element changes size under a zoomed view -- fullscreen, a rotated phone -- and a
        // translate in pixels is meaningless at the new size. Re-clamp rather than reset, so a
        // rotation keeps the zoom.
        const resize = window.ResizeObserver
            ? new ResizeObserver(() => { view = clampView(view, elementSize()); render(); })
            : null;
        if (resize) resize.observe(video);

        return {
            /** True for a short while after any touch: the mouse event being handled is iOS
             *  replaying the tap, not a mouse. */
            recentlyTouched: () => performance.now() - lastTouch < MOUSE_AFTER_TOUCH_MS,
            /** Back to an unzoomed picture with the cursor centred, for a new session. */
            reset() {
                view = { scale: 1, tx: 0, ty: 0 };
                pos = [0.5, 0.5];
                render();
            },
            dispose() {
                if (resize) resize.disconnect();
                clearHold();
            },
        };
    }

    window.RemoteTouch = {
        create,
        geometry: { pictureBox, clampView, zoomAbout, followPoint, MAX_ZOOM },
    };
})();
