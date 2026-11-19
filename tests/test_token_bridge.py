"""postMessage bridge that lets the mini-tabs parent hold the keep-unlocked bearer.

The page script runs under node with the parent origin stubbed. A Chrome harness
(tests/token_bridge_harness.mjs) iframes the real page from a different origin.
Loopback is a secure context, so that harness still has crypto.subtle. The runtime
suite also runs the same bridge with subtle removed.
"""

import hashlib
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text()
STUB_ORIGIN = "http://127.0.0.1:47300"


def page_script(html=HTML):
    start = html.index("<script>", html.index("<body"))
    return html[start + len("<script>"):html.rindex("</script>")]


def extract(html, name):
    match = re.search(rf"^( *)(?:async )?function {name}\(.*?^\1\}}$", html, re.M | re.S)
    if not match:
        raise AssertionError(f"missing function {name}")
    return match.group(0)


def bridge_slice(html, origin):
    start = html.index("        const TOKEN_KEY")
    end = html.index("        function esc(")
    chunk = html[start:end]
    match = re.search(r"const PARENT_ORIGIN_SHA256 = '([0-9a-f]{64})';", chunk)
    if not match or chunk.count(match.group(1)) != 1:
        raise AssertionError("parent origin hash was not unique in the bridge slice")
    return chunk.replace(match.group(1), hashlib.sha256(origin.encode()).hexdigest(), 1)


RUNNER = r"""
const listeners = new Set();
const posts = [];
const allPosts = [];
const locks = [];
const fetches = [];
let inits = 0;
let localInsights = null;
let currentData = { marker: 'dashboard' };
const PIN = '246801';
const BEARER = 'bearer-session';
const parent = {
    postMessage(message, targetOrigin) {
        const entry = { message, targetOrigin };
        posts.push(entry);
        allPosts.push(entry);
    },
};
const other = { id: 'not-the-parent' };
function makeStore() {
    const map = new Map();
    return {
        getItem(key) { return map.has(key) ? map.get(key) : null; },
        setItem(key, value) { map.set(key, String(value)); },
        removeItem(key) { map.delete(key); },
        clear() { map.clear(); },
    };
}
localStorage = makeStore();
sessionStorage = makeStore();
window = {
    addEventListener(type, fn) { if (type === 'message') listeners.add(fn); },
    removeEventListener(type, fn) { listeners.delete(fn); },
    parent,
};

function showLock(message) { locks.push(message || ''); }
async function loadLocalInsights() { throw new Error('insights should stay idle'); }
async function init() { inits += 1; }

function reset() {
    posts.length = 0;
    locks.length = 0;
    fetches.length = 0;
    inits = 0;
    parentTokenListening = false;
    frameLinksBound = false;
    verifiedParentOrigin = '';
    parentOriginTask = null;
    parentOriginPromise = null;
    parentOriginResolve = null;
    sawParentSource = false;
    listeners.clear();
    location.ancestorOrigins = [EXPECTED_ORIGIN];
    localStorage.clear();
    sessionStorage.clear();
    window.parent = parent;
    currentData = { marker: 'dashboard' };
}

function deliver(origin, source, data) {
    const event = { origin, source, data };
    for (const fn of [...listeners]) fn(event);
}

function assert(cond, message) {
    if (!cond) throw new Error(message);
}

const results = [];
async function scenario(name, fn) {
    reset();
    try {
        await fn();
        results.push({ name, ok: true });
    } catch (error) {
        results.push({ name, ok: false, error: String(error && error.stack || error) });
    }
}

const pending = (async () => {
    assert(/^[0-9a-f]{64}$/.test(PARENT_ORIGIN_SHA256), PARENT_ORIGIN_SHA256);
    assert(PARENT_ORIGIN_SHA256 !== EXPECTED_ORIGIN, 'the hash is not the origin');
    assert(EXPECTED_ORIGIN.indexOf('*') < 0, 'stub origin');
    assert(!!(crypto && crypto.subtle) === SUBTLE, 'subtle presence');

    await scenario('request uses the parent origin and never a star', async () => {
        await requestParentToken();
        assert(posts.length === 1, 'expected one request');
        assert(posts[0].targetOrigin === EXPECTED_ORIGIN, 'target ' + posts[0].targetOrigin);
        assert(posts[0].targetOrigin !== '*', 'wildcard target');
        assert(JSON.stringify(posts[0].message) === JSON.stringify({ type: 'ad-token-request' }), JSON.stringify(posts[0].message));
        assert(Object.keys(posts[0].message).join(',') === 'type', 'extra request fields');
    });

    await scenario('only the parent origin and parent window can supply a token', async () => {
        await requestParentToken();
        const badOrigins = [
            EXPECTED_ORIGIN + '.evil.example',
            'http://127.0.0.1:47301',
            'https://127.0.0.1:47300',
            'http://localhost:47300',
            'http://127.0.0.1:473000',
            'null',
            '*',
            EXPECTED_ORIGIN + '/',
        ];
        for (const origin of badOrigins) {
            deliver(origin, parent, { type: 'ad-token', token: 'stolen-bearer' });
        }
        deliver(EXPECTED_ORIGIN, other, { type: 'ad-token', token: 'stolen-bearer' });
        deliver(EXPECTED_ORIGIN, window, { type: 'ad-token', token: 'stolen-bearer' });
        deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token', token: 246801 });
        deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token', token: { pin: PIN } });
        deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token-save', token: BEARER });
        deliver(EXPECTED_ORIGIN, parent, null);
        deliver(EXPECTED_ORIGIN, parent, 'ad-token');
        deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token' });
        assert(getToken() === '', 'rejected message stored ' + getToken());
        assert(inits === 0, 'rejected message booted the page');
        assert(listeners.size === 1, 'stopped listening early');
        deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token', token: '' });
        assert(getToken() === '', 'empty token stored');
        assert(listeners.size === 0, 'empty token should answer the request');
        deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token', token: 'late-bearer' });
        assert(getToken() === '', 'late token stored after the answer');
    });

    await scenario('a parent bearer is stored like keep-unlocked', async () => {
        await requestParentToken();
        deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token', token: BEARER });
        assert(getToken() === BEARER, 'getToken');
        assert(localStorage.getItem('ad.token') === BEARER, 'localStorage');
        assert(sessionStorage.getItem('ad.token') === null, 'sessionStorage');
        assert(inits === 1, 'init count ' + inits);
        assert(JSON.stringify(posts).indexOf(PIN) < 0, 'pin appeared in a bridge message');
    });

    await scenario('a usable local token skips the request', async () => {
        setSession('already-bearer', true);
        posts.length = 0;
        await requestParentToken();
        assert(posts.length === 0, 'requested despite a stored token');
        assert(getToken() === 'already-bearer', 'stored token changed');
    });

    await scenario('keep-unlocked posts the bearer and never the pin', async () => {
        fetch = async (url, opts) => {
            fetches.push({ url, body: JSON.parse(opts.body) });
            return { ok: true, status: 200, json: async () => ({ token: BEARER }) };
        };
        await unlockWithPin(PIN, true);
        assert(fetches.length === 1, 'unlock fetch');
        assert(fetches[0].url.indexOf('/auth/unlock') > 0, fetches[0].url);
        assert(fetches[0].body.pin === PIN, 'API body lost the pin');
        assert(fetches[0].body.keepUnlocked === true, 'keep flag');
        assert(posts.length === 1, 'save posts ' + posts.length);
        assert(JSON.stringify(posts[0].message) === JSON.stringify({ type: 'ad-token-save', token: BEARER }), JSON.stringify(posts[0].message));
        assert(posts[0].targetOrigin === EXPECTED_ORIGIN, posts[0].targetOrigin);
        assert(posts[0].targetOrigin !== '*', 'wildcard save');
        assert(!('pin' in posts[0].message), 'save carried a pin field');
        assert(JSON.stringify(posts).indexOf(PIN) < 0, 'pin leaked into postMessage');
        assert(getToken() === BEARER, 'stored token');
    });

    await scenario('a session-only unlock does not post the token or the pin', async () => {
        fetch = async () => ({ ok: true, status: 200, json: async () => ({ token: BEARER }) });
        await unlockWithPin(PIN, false);
        assert(posts.length === 0, 'posted ' + JSON.stringify(posts));
        assert(getToken() === BEARER, 'session token missing');
        assert(localStorage.getItem('ad.token') === null, 'wrote a durable token');
    });

    await scenario('a direct visit posts nothing', async () => {
        window.parent = window;
        fetch = async () => ({ ok: true, status: 200, json: async () => ({ token: BEARER }) });
        await requestParentToken();
        await unlockWithPin(PIN, true);
        await lockDashboard();
        assert(posts.length === 0, 'direct visit posted ' + JSON.stringify(posts));
        assert(getToken() === '', 'lock did not clear the direct session');
    });

    await scenario('lock and a 401 clear the parent token', async () => {
        setSession(BEARER, true);
        posts.length = 0;
        await lockDashboard();
        assert(posts.length === 1, 'lock posts');
        assert(JSON.stringify(posts[0].message) === JSON.stringify({ type: 'ad-token-clear' }), JSON.stringify(posts[0].message));
        assert(posts[0].targetOrigin === EXPECTED_ORIGIN && posts[0].targetOrigin !== '*', posts[0].targetOrigin);
        assert(!('pin' in posts[0].message) && !('token' in posts[0].message), 'clear carried extra fields');
        assert(getToken() === '', 'token remained after lock');
        assert(currentData === null, 'dashboard data remained');

        setSession('stale-bearer', true);
        posts.length = 0;
        locks.length = 0;
        fetch = async (url, opts) => {
            fetches.push({ url, authorization: opts.headers.Authorization });
            return { ok: false, status: 401, json: async () => ({ error: 'nope' }) };
        };
        const data = await loadData();
        assert(data === null, '401 returned data');
        assert(getToken() === '', '401 left the token in place');
        assert(fetches[0].url.indexOf('/v1/grades/latest') > 0, fetches[0].url);
        assert(fetches[0].authorization === 'Bearer stale-bearer', fetches[0].authorization);
        assert(posts.length === 1 && posts[0].message.type === 'ad-token-clear', JSON.stringify(posts));
        assert(posts[0].targetOrigin === EXPECTED_ORIGIN, posts[0].targetOrigin);
        assert(locks[0] === 'Your session expired. Type your PIN again.', locks[0]);
        assert(JSON.stringify(posts).indexOf('stale-bearer') < 0, '401 echoed the bearer');
        assert(JSON.stringify(posts).indexOf(PIN) < 0, '401 echoed a pin');
    });

    await scenario('whitespace is not a token', async () => {
        for (const blank of [' ', '   ', '\n', '\t', ' \n\t ']) {
            setSession(blank, true);
            assert(getToken() === '', 'stored ' + JSON.stringify(blank));
            assert(localStorage.getItem('ad.token') === null, 'local write ' + JSON.stringify(blank));
            assert(sessionStorage.getItem('ad.token') === null, 'session write ' + JSON.stringify(blank));
        }
        localStorage.setItem('ad.token', '   ');
        assert(getToken() === '', 'whitespace counted as usable');
        await requestParentToken();
        assert(posts.length === 1 && posts[0].message.type === 'ad-token-request', 'no request for a blank token');
        deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token', token: '   ' });
        assert(getToken() === '', 'parent whitespace was stored');
        assert(inits === 0, 'whitespace token booted the page');
        assert(posts.length === 1, 'whitespace was posted onward');
        fetch = async () => ({ ok: true, status: 200, json: async () => ({ token: '   ' }) });
        posts.length = 0;
        await unlockWithPin(PIN, true);
        assert(posts.length === 0, 'unlock posted ' + JSON.stringify(posts));
        assert(getToken() === '', 'unlock stored whitespace');
        assert(JSON.stringify(allPosts).indexOf(PIN) < 0, 'pin leaked');
        posts.length = 0;
        fetch = async () => ({ ok: true, status: 200, json: async () => ({ token: '  bearer-session  ' }) });
        await unlockWithPin(PIN, true);
        assert(posts.length === 1, 'non-blank token was dropped');
        assert(posts[0].message.token === '  bearer-session  ', JSON.stringify(posts[0].message));
        assert(posts[0].targetOrigin === EXPECTED_ORIGIN && posts[0].targetOrigin !== '*', posts[0].targetOrigin);
        assert(JSON.stringify(posts).indexOf(PIN) < 0, 'pin leaked beside a real token');
    });

    function fakeLink(href, target) {
        const attrs = {};
        if (href != null) attrs.href = href;
        if (target != null) attrs.target = target;
        const link = {
            getAttribute(key) { return Object.prototype.hasOwnProperty.call(attrs, key) ? attrs[key] : null; },
            setAttribute(key, value) { attrs[key] = String(value); },
        };
        link.closest = () => link;
        return link;
    }

    await scenario('framed links open outside the dashboard frame', async () => {
        window.parent = parent;
        for (const href of ['https://example.com/leave', '/aeries-dashboard/other', 'http://localhost:8765/']) {
            const link = fakeLink(href);
            onFrameLinkClick({ target: link });
            const target = (link.getAttribute('target') || '').toLowerCase();
            assert(target === '_blank' || target === '_top', href + ' target ' + target);
        }
        const topped = fakeLink('https://example.com/top', '_top');
        onFrameLinkClick({ target: topped });
        assert(topped.getAttribute('target') === '_top', 'rewrote _top');
        const blank = fakeLink('https://example.com/tab', '_blank');
        onFrameLinkClick({ target: blank });
        assert(blank.getAttribute('target') === '_blank', 'rewrote _blank');
        const fragment = fakeLink('#standing');
        onFrameLinkClick({ target: fragment });
        assert(fragment.getAttribute('target') == null, 'fragment was retargeted');
    });

    await scenario('a direct visit does not retarget links', async () => {
        window.parent = window;
        const link = fakeLink('https://example.com/leave');
        onFrameLinkClick({ target: link });
        assert(link.getAttribute('target') == null, 'direct target ' + link.getAttribute('target'));
    });

    await scenario('a hinted origin that does not hash is never used', async () => {
        location.ancestorOrigins = ['http://evil.example'];
        await requestParentToken();
        assert(posts.length === 0, 'posted to an unverified origin');
        deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token', token: BEARER });
        assert(getToken() === '', 'message origin overrode a bad hint');
    });

    await scenario('the first parent message supplies the origin when there is no hint', async () => {
        location.ancestorOrigins = [];
        const waiting = requestParentToken();
        deliver(EXPECTED_ORIGIN, other, { type: 'ad-token', token: 'stolen-bearer' });
        deliver('http://evil.example', parent, { type: 'ad-token', token: BEARER });
        await waiting;
        assert(posts.length === 0, 'unverified request ' + JSON.stringify(posts));
        assert(getToken() === '', 'wrong first origin was stored');
        deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token', token: BEARER });
        assert(getToken() === '', 'a later origin was accepted');
    });

    await scenario('a matching parent message verifies the origin and stores the bearer', async () => {
        location.ancestorOrigins = [];
        const waiting = requestParentToken();
        deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token', token: BEARER });
        await waiting;
        assert(getToken() === BEARER, 'bearer from the parent message');
        assert(posts.length === 0, 'request after the parent already answered');
        assert(JSON.stringify(allPosts).indexOf(PIN) < 0, 'pin leaked');
    });

    if (SUBTLE) {
        await scenario('subtle is preferred over the fallback', async () => {
            let calls = 0;
            const real = sha256Bytes;
            sha256Bytes = (bytes) => {
                calls += 1;
                return real(bytes);
            };
            try {
                await requestParentToken();
                assert(calls === 0, 'fallback ran beside subtle');
                assert(posts.length === 1, 'request missing');
                assert(posts[0].targetOrigin === EXPECTED_ORIGIN && posts[0].targetOrigin !== '*', posts[0].targetOrigin);
                deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token', token: BEARER });
                assert(getToken() === BEARER, 'bearer was not stored');
            } finally {
                sha256Bytes = real;
            }
        });
    } else {
        await scenario('without subtle the origin digest still matches', async () => {
            assert(!(crypto && crypto.subtle), 'subtle still present');
            assert(await originDigest('abc') === ABC_HASH, 'abc digest');
            assert(await originDigest(LONG_TEXT) === LONG_HASH, 'long digest');
            assert(await originDigest(EXPECTED_ORIGIN) === PARENT_ORIGIN_SHA256, 'origin digest');
            assert(await originDigest('') === '', 'empty origin');
            const unrelated = await originDigest('http://evil.example');
            assert(unrelated !== PARENT_ORIGIN_SHA256, 'unrelated origin matched');
            assert(/^[0-9a-f]{64}$/.test(unrelated), unrelated);
        });

        await scenario('without subtle the bearer round-trips', async () => {
            assert(!(crypto && crypto.subtle), 'subtle still present');
            let calls = 0;
            const real = sha256Bytes;
            sha256Bytes = (bytes) => {
                calls += 1;
                return real(bytes);
            };
            try {
                await requestParentToken();
                assert(calls > 0, 'fallback did not hash');
                assert(posts.length === 1, 'request missing');
                assert(posts[0].targetOrigin === EXPECTED_ORIGIN, 'target ' + posts[0].targetOrigin);
                assert(posts[0].targetOrigin !== '*', 'wildcard target');
                assert(JSON.stringify(posts[0].message) === JSON.stringify({ type: 'ad-token-request' }), JSON.stringify(posts[0].message));
                deliver(EXPECTED_ORIGIN, parent, { type: 'ad-token', token: BEARER });
                assert(getToken() === BEARER, 'bearer was not stored');
                assert(localStorage.getItem('ad.token') === BEARER, 'localStorage');
                assert(sessionStorage.getItem('ad.token') === null, 'sessionStorage');
                assert(inits === 1, 'init count ' + inits);
                posts.length = 0;
                fetch = async () => ({ ok: true, status: 200, json: async () => ({ token: 'round-trip-bearer' }) });
                await unlockWithPin(PIN, true);
                assert(posts.length === 1, 'save posts ' + posts.length);
                assert(JSON.stringify(posts[0].message) === JSON.stringify({ type: 'ad-token-save', token: 'round-trip-bearer' }), JSON.stringify(posts[0].message));
                assert(posts[0].targetOrigin === EXPECTED_ORIGIN && posts[0].targetOrigin !== '*', posts[0].targetOrigin);
                assert(!('pin' in posts[0].message), 'save carried a pin field');
                assert(JSON.stringify(posts).indexOf(PIN) < 0, 'pin leaked into postMessage');
                assert(getToken() === 'round-trip-bearer', 'stored token');
            } finally {
                sha256Bytes = real;
            }
        });
    }

    const targets = [...new Set(allPosts.map((entry) => entry.targetOrigin))];
    report({
        ok: results.every((item) => item.ok) && targets.length === 1 && targets[0] === EXPECTED_ORIGIN,
        results,
        targets,
        subtle: !!(crypto && crypto.subtle),
    });
})();
pending
"""


def run_bridge(origin=STUB_ORIGIN, subtle=True):
    program = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const hostCrypto = crypto;
const ctx = {
    EXPECTED_ORIGIN: input.origin,
    SUBTLE: input.subtle,
    ABC_HASH: input.abcHash,
    LONG_HASH: input.longHash,
    LONG_TEXT: input.longText,
    console, JSON, Object, Array, Promise, String, Error, Date, Math, Number, Boolean, RegExp,
    crypto: input.subtle ? hostCrypto : { subtle: undefined },
    TextEncoder,
    location: { hostname: '127.0.0.1', protocol: 'http:', href: 'http://127.0.0.1/', ancestorOrigins: [] },
    document: { getElementById() { return { innerHTML: '', hidden: false, textContent: '' }; }, addEventListener() {} },
    fetch: async () => { throw new Error('fetch was not stubbed'); },
    window: {},
    localStorage: {},
    sessionStorage: {},
};
ctx.report = (value) => { ctx.output = value; };
vm.createContext(ctx);
const source = [input.slice, input.lock, input.load, input.runner].join('\n');
const pending = vm.runInContext('(function () {\n' + source + '\nreturn pending;\n})()', ctx);
pending.then(() => {
    process.stdout.write(JSON.stringify(ctx.output));
}).catch((error) => {
    console.error(error && error.stack || error);
    process.exit(1);
});
"""
    payload = {
        "origin": origin,
        "subtle": subtle,
        "abcHash": hashlib.sha256(b"abc").hexdigest(),
        "longHash": hashlib.sha256(
            b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"
        ).hexdigest(),
        "longText": "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
        "slice": bridge_slice(HTML, origin),
        "lock": extract(HTML, "lockDashboard"),
        "load": extract(HTML, "loadData"),
        "runner": RUNNER,
    }
    done = subprocess.run(
        ["node", "-e", program],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise AssertionError(done.stderr or done.stdout)
    return json.loads(done.stdout)


class TokenBridgeSourceTests(unittest.TestCase):
    def test_parent_origin_is_a_hash_with_no_wildcard(self):
        match = re.search(r"const PARENT_ORIGIN_SHA256 = '([0-9a-f]{64})';", HTML)
        self.assertIsNotNone(match)
        script = page_script()
        self.assertEqual(script.count("postMessage("), 1)
        self.assertIn("window.parent.postMessage(message, verifiedParentOrigin);", script)
        self.assertIsNone(re.search(r"postMessage\([^)]*['\"]\*['\"]", script))
        bridge = bridge_slice(HTML, STUB_ORIGIN)
        self.assertNotIn("'*'", bridge)
        self.assertNotIn('"*"', bridge)
        self.assertNotIn(match.group(1), bridge)
        self.assertIn(hashlib.sha256(STUB_ORIGIN.encode()).hexdigest(), bridge)
        self.assertNotIn("ts" + ".net", HTML)
        self.assertNotIn("casa" + "smacmini", HTML)

    def test_origin_check_is_exact_equality(self):
        handler = extract(HTML, "onParentTokenMessage")
        self.assertIn("if (event.source !== window.parent) return;", handler)
        self.assertIn("event.origin !== verifiedParentOrigin", handler)
        self.assertNotIn("startsWith", handler)
        self.assertNotIn("includes", handler)
        self.assertNotIn("indexOf", handler)
        self.assertIn("data.type !== 'ad-token'", extract(HTML, "takeParentToken"))
        request = extract(HTML, "requestParentToken")
        self.assertIn("if (!inParentFrame() || parentTokenListening || getToken()) return Promise.resolve();", request)
        self.assertIn("postParent({ type: 'ad-token-request' });", request)

    def test_save_sends_the_bearer_and_the_pin_stays_on_the_unlock_request(self):
        unlock = extract(HTML, "unlockWithPin")
        self.assertIn("JSON.stringify({ pin, keepUnlocked })", unlock)
        self.assertIn("postParent({ type: 'ad-token-save', token: data.token });", unlock)
        self.assertIn("usableToken(data.token)", unlock)
        self.assertNotIn("token: pin", unlock)
        self.assertIn("value.trim() !== ''", extract(HTML, "usableToken"))
        stamp = extract(HTML, "stampFrameLink")
        self.assertIn("current === '_top' || current === '_blank'", stamp)
        self.assertIn("link.setAttribute('target', '_blank')", stamp)
        self.assertIn("keepDashboardFrame();", extract(HTML, "init"))
        self.assertIn("stampFrameLinks(document);", extract(HTML, "paint"))
        self.assertNotIn("ad-token-save', pin", unlock)
        self.assertIn("postParent({ type: 'ad-token-clear' });", extract(HTML, "lockDashboard"))
        self.assertIn("postParent({ type: 'ad-token-clear' });", extract(HTML, "loadData"))
        init = extract(HTML, "init")
        self.assertIn("requestParentToken();", init)
        self.assertIn("if (!getToken())", init)

    def test_origin_digest_falls_back_only_when_subtle_is_missing(self):
        digest = extract(HTML, "originDigest")
        self.assertIn("if (!origin) return '';", digest)
        self.assertIn("if (crypto && crypto.subtle)", digest)
        self.assertIn("crypto.subtle.digest('SHA-256', bytes)", digest)
        self.assertIn("return hexDigest(sha256Bytes(bytes));", digest)
        self.assertNotIn("!crypto.subtle) return ''", digest)
        fallback = extract(HTML, "sha256Bytes")
        self.assertNotIn("crypto", fallback)
        self.assertNotIn("fetch(", fallback)
        self.assertNotIn("http://", fallback)
        self.assertNotIn("https://", fallback)

    def test_page_script_parses(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        program = (
            "const fs=require('fs'); const vm=require('vm');"
            "const html=fs.readFileSync(0,'utf8');"
            "const start=html.indexOf('<script>', html.indexOf('<body'));"
            "const src=html.slice(start+8, html.lastIndexOf('</script>'));"
            "new vm.Script(src);"
        )
        done = subprocess.run(["node", "-e", program], input=HTML, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TokenBridgeRuntimeTests(unittest.TestCase):
    def test_origin_target_and_pin_stay_in_bounds(self):
        result = run_bridge()
        failed = [item for item in result["results"] if not item["ok"]]
        self.assertEqual(failed, [], failed)
        self.assertEqual(result["targets"], [STUB_ORIGIN])
        self.assertTrue(result["ok"])
        self.assertTrue(result["subtle"])
        names = [item["name"] for item in result["results"]]
        self.assertIn("only the parent origin and parent window can supply a token", names)
        self.assertIn("request uses the parent origin and never a star", names)
        self.assertIn("keep-unlocked posts the bearer and never the pin", names)
        self.assertIn("whitespace is not a token", names)
        self.assertIn("framed links open outside the dashboard frame", names)
        self.assertIn("a direct visit does not retarget links", names)
        self.assertIn("subtle is preferred over the fallback", names)

    def test_bearer_round_trips_without_subtle(self):
        result = run_bridge(subtle=False)
        failed = [item for item in result["results"] if not item["ok"]]
        self.assertEqual(failed, [], failed)
        self.assertFalse(result["subtle"])
        self.assertEqual(result["targets"], [STUB_ORIGIN])
        self.assertTrue(result["ok"])
        names = [item["name"] for item in result["results"]]
        self.assertIn("without subtle the bearer round-trips", names)
        self.assertIn("without subtle the origin digest still matches", names)
        self.assertNotIn("subtle is preferred over the fallback", names)
        self.assertIn("only the parent origin and parent window can supply a token", names)
        self.assertIn("request uses the parent origin and never a star", names)
        self.assertIn("keep-unlocked posts the bearer and never the pin", names)
        self.assertIn("whitespace is not a token", names)
        self.assertIn("a direct visit posts nothing", names)
        self.assertIn("a hinted origin that does not hash is never used", names)
        self.assertIn("framed links open outside the dashboard frame", names)


@unittest.skipUnless(shutil.which("node") and shutil.which("google-chrome"), "node or chrome missing")
class TokenBridgeHarnessTests(unittest.TestCase):
    def test_parent_page_iframe_bridge(self):
        done = subprocess.run(
            ["node", str(ROOT / "tests" / "token_bridge_harness.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + "\n" + done.stderr)
        self.assertIn("harness ok", done.stdout)


if __name__ == "__main__":
    unittest.main()
