import streamlit as st
import requests
import time
import os
import logging
import collections
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError as e:
    st.error(f"Missing dependencies: {e}")
    st.stop()

# ─── Configuration ────────────────────────────────────────────────────────────
BASE = "https://volunteer.bloomerang.co"
ORG_ID = 5269
EVENT_ID = 51764
LOCAL_TZ = ZoneInfo("America/New_York")

# Cache TTLs — tuned to minimize API load while keeping the board fresh
SHIFT_CACHE_TTL = 300     # shifts rarely change → 5 min
SERVICE_CACHE_TTL = 60    # clock-ins/outs surface here → 1 min
REFRESH_SECS = 120        # auto-rerun interval (UI refresh)

# Status thresholds
UPCOMING_MINUTES = 60     # within this many min of start → "Starting Soon"
LATE_OUT_MINUTES = 30     # clocked in but not out this long after shift end → "Missing Clock-Out"

st.set_page_config(page_title="Refuge Live Board", page_icon="🐾", layout="wide")

# ─── Logging ──────────────────────────────────────────────────────────────────
# Two outputs:
#   1. Python `logging` to stdout — shows up in Streamlit Cloud's Logs panel
#      and any terminal where `streamlit run` is active.
#   2. In-memory ring buffer — last N log records, rendered as an expandable
#      "Debug Log" panel at the bottom of the dashboard. Also downloadable
#      as a single text file.
#
# The ring buffer lives on the Streamlit session-state dict so it survives
# reruns (Streamlit reruns the script top-to-bottom on every interaction).
# We pick a practical cap (~1000 lines) so memory stays bounded even across
# a long-running session.

LOG_BUFFER_SIZE = 1000
LOG_LEVEL = logging.DEBUG  # verbose by default; dial down later if noisy


class _RingBufferHandler(logging.Handler):
    """A logging handler that pushes formatted records into a deque.
    The deque is shared across the whole session (stored on st.session_state)
    so the Debug Log panel always reflects the most recent activity."""

    def __init__(self, buffer):
        super().__init__()
        self.buffer = buffer

    def emit(self, record):
        try:
            msg = self.format(record)
            self.buffer.append(msg)
        except Exception:
            # Never let logging crash the app
            self.handleError(record)


def _init_logging():
    """
    Idempotent logger setup. Safe to call on every Streamlit rerun — only
    adds handlers once. Attaches both a stdout handler and the in-memory
    ring-buffer handler.
    """
    if 'log_buffer' not in st.session_state:
        st.session_state['log_buffer'] = collections.deque(maxlen=LOG_BUFFER_SIZE)

    root = logging.getLogger('refugeboard')
    if getattr(root, '_refuge_configured', False):
        # Re-wire the ring buffer handler in case session_state deque got
        # replaced (shouldn't happen, but cheap defense).
        for h in root.handlers:
            if isinstance(h, _RingBufferHandler):
                h.buffer = st.session_state['log_buffer']
        return root

    root.setLevel(LOG_LEVEL)
    root.propagate = False  # don't double-log through Streamlit's root logger

    fmt = logging.Formatter(
        '%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    stdout_h = logging.StreamHandler()
    stdout_h.setFormatter(fmt)
    stdout_h.setLevel(LOG_LEVEL)
    root.addHandler(stdout_h)

    ring_h = _RingBufferHandler(st.session_state['log_buffer'])
    ring_h.setFormatter(fmt)
    ring_h.setLevel(LOG_LEVEL)
    root.addHandler(ring_h)

    root._refuge_configured = True
    return root


# Shorthand loggers for each subsystem. Makes log filtering easy (grep by name).
_init_logging()
log_auth    = logging.getLogger('refugeboard.auth')
log_api     = logging.getLogger('refugeboard.api')
log_kiosk   = logging.getLogger('refugeboard.kiosk')
log_match   = logging.getLogger('refugeboard.match')
log_render  = logging.getLogger('refugeboard.render')
log_cache   = logging.getLogger('refugeboard.cache')


# ─── CSS ──────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&display=swap');

    html, body, [class*="css"] { font-family: 'Inter', system-ui, -apple-system, sans-serif; }

    .shift-card {
        padding: 1.25rem 1.35rem;
        border-radius: 14px;
        margin-bottom: 1rem;
        border-left: 6px solid #64748b;
        box-shadow: 0 4px 14px rgba(0,0,0,0.3);
        background: #1e2533;
        color: #f1f5f9;
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    .shift-card:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(0,0,0,0.4); }

    .shift-time {
        font-size: 0.85rem; font-weight: 700; color: #94a3b8;
        margin-bottom: 0.55rem;
        font-family: 'JetBrains Mono', 'Courier New', monospace;
        letter-spacing: 0.02em;
    }
    .shift-name {
        font-size: 1.35rem; font-weight: 800;
        margin-bottom: 0.1rem; line-height: 1.15;
        color: #f8fafc !important;
    }
    .shift-role {
        font-size: 0.7rem; text-transform: uppercase;
        color: #94a3b8; font-weight: 700;
        margin-bottom: 0.9rem; letter-spacing: 0.1em;
    }
    .status-badge {
        padding: 0.4rem 0.85rem; border-radius: 20px;
        font-weight: 800; font-size: 0.7rem;
        text-transform: uppercase; display: inline-block;
        letter-spacing: 0.06em;
    }

    /* Status variants — all dark gradients + light text */

    /* Completed — purple */
    .status-completed {
        background: linear-gradient(135deg, #2d1e47 0%, #3a2862 100%);
        border-left-color: #a855f7;
    }
    .status-completed .status-badge { background: #a855f7; color: #1e1033; }

    /* On Shift (rare — only surfaces if cin/no-cout record appears) — green, pulsing */
    .status-checked-in {
        background: linear-gradient(135deg, #0f3d2b 0%, #1a5d44 100%);
        border-left-color: #10b981;
        animation: pulseGreen 2.2s ease-in-out infinite;
    }
    .status-checked-in .status-badge { background: #10b981; color: #052e1d; }
    @keyframes pulseGreen {
        0%, 100% { box-shadow: 0 4px 14px rgba(0,0,0,0.3), 0 0 0 0 rgba(16,185,129,0.3); }
        50%      { box-shadow: 0 4px 14px rgba(0,0,0,0.3), 0 0 0 6px rgba(16,185,129,0); }
    }
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50%      { opacity: 0.4; }
    }

    /* In Progress — amber, neutral (honest: we can't confirm live state) */
    .status-in-progress {
        background: linear-gradient(135deg, #332719 0%, #4d3a28 100%);
        border-left-color: #eab308;
    }
    .status-in-progress .status-badge { background: #eab308; color: #1a0f00; }

    /* Missing Clock-Out — brighter amber */
    .status-alert-amber {
        background: linear-gradient(135deg, #3d2e0f 0%, #5c4515 100%);
        border-left-color: #f59e0b;
    }
    .status-alert-amber .status-badge { background: #f59e0b; color: #1a0f00; }

    /* No Show — red (only after shift is over) */
    .status-alert-red {
        background: linear-gradient(135deg, #3d1a1a 0%, #5d2626 100%);
        border-left-color: #ef4444;
    }
    .status-alert-red .status-badge { background: #ef4444; color: #2d0606; }

    /* Starting Soon — blue */
    .status-upcoming {
        background: linear-gradient(135deg, #1a2e4d 0%, #1e4473 100%);
        border-left-color: #3b82f6;
    }
    .status-upcoming .status-badge { background: #3b82f6; color: #0a1929; }

    /* Scheduled — neutral slate */
    .status-pending {
        background: linear-gradient(135deg, #1e2533 0%, #2a3344 100%);
        border-left-color: #64748b;
    }
    .status-pending .status-badge { background: #64748b; color: #0f172a; }

    .punch-box {
        margin-top: 10px; padding: 8px 12px;
        background: rgba(0,0,0,0.35); border-radius: 8px;
        font-family: 'JetBrains Mono', 'Courier New', monospace;
        font-size: 0.82rem; font-weight: 600;
        color: #e2e8f0;
        border: 1px solid rgba(255,255,255,0.06);
    }

    .meta-bar {
        background: #1e2533; padding: 0.65rem 1.1rem;
        border-radius: 10px; margin-bottom: 1rem;
        color: #cbd5e0; font-size: 0.82rem;
        display: flex; gap: 1.3rem; flex-wrap: wrap; align-items: center;
        border: 1px solid rgba(255,255,255,0.05);
    }
    .meta-bar .stat { display: flex; align-items: baseline; gap: 0.4rem; }
    .meta-bar .stat b { color: #f8fafc; font-size: 1rem; font-weight: 800; }
    .meta-bar .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .dot-green { background: #10b981; }
    .dot-purple { background: #a855f7; }
    .dot-blue { background: #3b82f6; }
    .dot-red { background: #ef4444; }
    .dot-amber { background: #eab308; }
    .dot-gray { background: #64748b; }

    .date-section-header {
        font-size: 1.15rem; font-weight: 800;
        color: #f8fafc;
        margin: 1.8rem 0 0.7rem 0;
        padding-bottom: 0.45rem;
        border-bottom: 2px solid #334155;
        letter-spacing: 0.02em;
    }
    .date-section-header .today-badge {
        display: inline-block;
        font-size: 0.65rem; font-weight: 800;
        padding: 2px 8px; border-radius: 10px;
        background: #10b981; color: #052e1d;
        margin-left: 0.6rem; vertical-align: middle;
        letter-spacing: 0.08em; text-transform: uppercase;
    }
</style>
""", unsafe_allow_html=True)

# ─── Auth ─────────────────────────────────────────────────────────────────────
def authenticate_headless(email, password):
    """Headless Chrome login. Returns auth dict or None."""
    log_auth.info("authenticate_headless start for email=%s", email)
    t0 = time.time()
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,900")
    # Consistent UA reduces device-fingerprint variance across re-auths
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    if os.path.exists("/usr/bin/chromium"):
        options.binary_location = "/usr/bin/chromium"

    driver = None
    try:
        service = (Service("/usr/bin/chromedriver") if os.path.exists("/usr/bin/chromedriver")
                   else Service(ChromeDriverManager().install()))
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, 15)

        driver.get(f"{BASE}/volunteer/#/login")
        time.sleep(3)

        wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//input[@type='email' or @type='text']"))).send_keys(email)
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(translate(., 'NEXT', 'next'), 'next')]"))).click()

        time.sleep(2)
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//input[@type='password']"))).send_keys(password)
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(translate(., 'LOG IN', 'log in'), 'log in')]"))).click()

        try:
            wait.until(EC.presence_of_element_located(
                (By.XPATH, "//*[contains(text(), 'My Shifts') or contains(text(), 'Welcome')]")))
            time.sleep(2)
        except Exception:
            raise Exception("Timeout waiting for dashboard to load.")

        sess = requests.Session()
        for c in driver.get_cookies():
            sess.cookies.set(c['name'], c['value'])

        token = driver.execute_script("""
            for (let i = 0; i < localStorage.length; i++) {
                let key = localStorage.key(i);
                if (key.includes('idToken') || key.includes('accessToken')) {
                    return localStorage.getItem(key);
                }
            }
            return null;
        """)

        log_auth.info(
            "authenticate_headless OK: got %d cookies in %.0fms "
            "(token %s — volunteer portal authenticates via cookies, "
            "so the absent token is expected)",
            len(sess.cookies),
            (time.time() - t0) * 1000,
            'present' if token else 'absent',
        )
        return {"sess": sess, "token": token}
    except Exception as e:
        log_auth.error("authenticate_headless FAILED: %s", str(e)[:300])
        st.error(f"Login failed: {e}")
        return None
    finally:
        if driver:
            driver.quit()


def attempt_silent_reauth():
    """
    Re-login using cached credentials. Called automatically when safe_get_json
    gets a 401. Mutates st.session_state.auth_data in place so all in-flight
    references pick up the new cookies/token.
    """
    log_auth.warning("attempt_silent_reauth triggered")
    creds = st.session_state.get('credentials')
    if not creds:
        return None

    new_auth = authenticate_headless(creds['email'], creds['password'])
    if not new_auth:
        return None

    existing = st.session_state.get('auth_data')
    if existing:
        existing['sess'] = new_auth['sess']
        existing['token'] = new_auth['token']
    else:
        st.session_state.auth_data = new_auth

    st.session_state['last_reauth'] = time.time()
    st.cache_data.clear()  # cached responses may have been built with stale auth
    return st.session_state.auth_data


def safe_get_json(auth, url, params=None, _retried=False):
    """GET with automatic silent re-auth on 401."""
    if not auth:
        return "NO_AUTH"

    sess = auth['sess']
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                     'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Referer': f'{BASE}/volunteer/',
    }
    if auth.get('token'):
        headers['Authorization'] = f"Bearer {auth['token']}"

    try:
        r = sess.get(url, params=params, headers=headers, timeout=15)
        if r.status_code == 401 and not _retried:
            new_auth = attempt_silent_reauth()
            if new_auth:
                return safe_get_json(new_auth, url, params, _retried=True)
            return "AUTH_EXPIRED"
        if r.status_code != 200:
            return f"ERR_{r.status_code}"
        return r.json()
    except Exception as e:
        return f"ERR_REQ: {str(e)}"


# ─── Data Fetching ────────────────────────────────────────────────────────────
@st.cache_data(ttl=SHIFT_CACHE_TTL)
def get_shifts(_auth):
    """All shifts for the event. Returns raw list or error string. Cached 5 min."""
    log_api.info("GET shifts (cache miss — fetching from Bloomerang)")
    t0 = time.time()
    result = safe_get_json(_auth,
        f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/shifts",
        {"includeShiftRoles": "true",
         "includeShiftUsers": "true",
         "take": 500,
         "includePast": "true"})
    if isinstance(result, list):
        log_api.info("GET shifts OK: %d shifts in %.0fms",
                     len(result), (time.time() - t0) * 1000)
    else:
        log_api.error("GET shifts FAILED: %s", str(result)[:200])
    return result


@st.cache_data(ttl=SERVICE_CACHE_TTL)
def get_service_times(_auth, uids_tuple):
    """Parallel fetch of serviceTime for each uid. Cached 60 sec."""
    uids = list(uids_tuple)
    log_api.info(
        "GET serviceTime batch (cache miss): %d uid(s)", len(uids)
    )
    t0 = time.time()
    result = {}
    def fetch(uid):
        r = safe_get_json(_auth,
            f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}/serviceTime")
        return uid, r if isinstance(r, list) else []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch, uid) for uid in uids]
        for f in as_completed(futures):
            uid, data = f.result()
            result[uid] = data
    total_records = sum(len(v) for v in result.values())
    log_api.info(
        "GET serviceTime batch done: %d records across %d uid(s) in %.0fms",
        total_records, len(uids), (time.time() - t0) * 1000,
    )
    return result


# ─── Kiosk live-state client ──────────────────────────────────────────────────
# These endpoints were discovered from the kiosk.bloomerang.co HAR capture.
# They are UNAUTHENTICATED (no session/token required) — they key off the
# volunteer's email. Bloomerang gates access via an IP allowlist instead, so
# these endpoints only respond from whitelisted client networks. Cloud host
# providers (Streamlit Cloud, GCP, AWS) generally get 403 "Host not in
# allowlist". From a residential IP, they return 200. On failure, we fall back
# silently to the serviceTime-based matching and the dashboard still works.
KIOSK_BASE = "https://kiosk.bloomerang.co"
KIOSK_CACHE_TTL = 30          # is_signed_in changes in real time → refresh often
KIOSK_TIMEOUT = 5             # cap per-volunteer request time
KIOSK_WORKERS = 8             # parallel fan-out
KIOSK_PROBE_TIMEOUT = 4       # for initial reachability check


# Browser-like headers for the kiosk endpoints — Bloomerang's API seems to
# reject requests that don't look browser-ish (plain python-requests UA gets
# funky responses). These match what the kiosk SPA sends.
KIOSK_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "app-version": "2.1",
    "Origin": KIOSK_BASE,
    "Referer": f"{KIOSK_BASE}/kiosk/app/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
}


def _kiosk_session_fetch(email, timeout=KIOSK_TIMEOUT):
    """
    The kiosk endpoint pattern requires a two-step handshake:

      1. POST /api/v1/getTodaysShiftsWithClockin with {username: email}
         — this is the "kiosk login" step. Server validates the email and
         sets session cookies identifying the volunteer.

      2. POST /api/v1/events/all/users/logged_in/currentevents
         — the ".../users/logged_in/..." path literally requires an active
         kiosk session from step 1. Without it the server returns
         401 "Invalid Request Parameters" even for valid emails.

    We use a requests.Session() so cookies set by step 1 persist to step 2.

    Returns a dict of the combined result:
      {
        'login_status': int,            # HTTP status from step 1
        'login_body': str,              # raw body from step 1 (for diagnostics)
        'events_status': int or None,   # HTTP status from step 2 (None if we didn't get to it)
        'events_data': list or None,    # parsed list of event states, or None on failure
        'events_body': str,             # raw body from step 2
        'error': str or None,
      }
    """
    result = {
        'login_status': None, 'login_body': '',
        'events_status': None, 'events_data': None, 'events_body': '',
        'error': None,
    }
    if not email:
        result['error'] = 'no email provided'
        log_kiosk.warning("session_fetch called with no email — skipping")
        return result

    log_kiosk.debug("session_fetch start email=%s timeout=%s", email, timeout)
    t0 = time.time()
    sess = requests.Session()
    sess.headers.update(KIOSK_HEADERS)

    try:
        # Step 1 — kiosk "login" via getTodaysShiftsWithClockin
        r1 = sess.post(
            f"{KIOSK_BASE}/api/v1/getTodaysShiftsWithClockin",
            json={"username": email},
            timeout=timeout,
        )
        result['login_status'] = r1.status_code
        result['login_body'] = (r1.text or '')[:400]
        log_kiosk.debug(
            "step1 login email=%s status=%s len=%d (%.0fms)",
            email, r1.status_code, len(r1.text or ''),
            (time.time() - t0) * 1000,
        )

        if r1.status_code != 200:
            log_kiosk.warning(
                "step1 login FAILED email=%s status=%s body=%s",
                email, r1.status_code, result['login_body'][:160]
            )
            return result

        # Step 2 — currentevents, now with session cookies from step 1
        t1 = time.time()
        r2 = sess.post(
            f"{KIOSK_BASE}/api/v1/events/all/users/logged_in/currentevents",
            json={"username": email},
            timeout=timeout,
        )
        result['events_status'] = r2.status_code
        result['events_body'] = (r2.text or '')[:400]
        log_kiosk.debug(
            "step2 currentevents email=%s status=%s len=%d (%.0fms)",
            email, r2.status_code, len(r2.text or ''),
            (time.time() - t1) * 1000,
        )

        if r2.status_code == 200:
            try:
                parsed = r2.json()
                if isinstance(parsed, list):
                    result['events_data'] = parsed
                    # Summarize what we got per-event
                    for entry in parsed:
                        log_kiosk.info(
                            "state email=%s event_id=%s is_signed_in=%s checkin_date=%s",
                            email,
                            entry.get('event_id'),
                            entry.get('is_signed_in'),
                            entry.get('checkin_date'),
                        )
                else:
                    log_kiosk.warning(
                        "step2 email=%s returned non-list JSON: %s",
                        email, str(parsed)[:160]
                    )
            except ValueError:
                result['error'] = 'events response was not JSON'
                log_kiosk.error(
                    "step2 email=%s returned non-JSON body=%s",
                    email, result['events_body'][:160]
                )
        else:
            log_kiosk.warning(
                "step2 currentevents FAILED email=%s status=%s body=%s",
                email, r2.status_code, result['events_body'][:160]
            )
    except requests.Timeout:
        result['error'] = f'timed out after {timeout}s'
        log_kiosk.error("TIMEOUT email=%s after %ss", email, timeout)
    except requests.ConnectionError as e:
        result['error'] = f'connection failed: {str(e)[:150]}'
        log_kiosk.error("CONN ERROR email=%s: %s", email, str(e)[:200])
    except requests.RequestException as e:
        result['error'] = f'request error: {str(e)[:150]}'
        log_kiosk.error("REQ ERROR email=%s: %s", email, str(e)[:200])

    log_kiosk.debug(
        "session_fetch done email=%s total=%.0fms",
        email, (time.time() - t0) * 1000,
    )
    return result


@st.cache_data(ttl=600, show_spinner=False)
def kiosk_probe_status(probe_email):
    """
    Probe the kiosk endpoint and return a structured status so the UI can
    explain exactly what's happening. Cached 10 min per email.

    Uses the two-step session flow (login → currentevents) because
    currentevents alone returns 401 without a kiosk session.

    Returns:
      {
        'reachable': bool,
        'status_code': int or None,     # status of the step that failed (or events if both OK)
        'reason': str,                  # short human label
        'detail': str,                  # longer detail for sidebar/tooltip
      }
    """
    if not probe_email:
        return {
            'reachable': False, 'status_code': None,
            'reason': 'No probe email',
            'detail': 'Log in first — need a real volunteer email to probe with.',
        }

    fetch = _kiosk_session_fetch(probe_email, timeout=KIOSK_PROBE_TIMEOUT)

    # Network / exception failures
    if fetch['error']:
        return {
            'reachable': False, 'status_code': None,
            'reason': fetch['error'],
            'detail': fetch['error'],
        }

    login_sc = fetch['login_status']
    events_sc = fetch['events_status']

    # IP allowlist block (only the specific 403 with "allowlist" body)
    if login_sc == 403 and 'allowlist' in fetch['login_body'].lower():
        return {
            'reachable': False, 'status_code': 403,
            'reason': 'IP blocked',
            'detail': 'Host not in allowlist — run from a whitelisted network.',
        }

    # Login succeeded AND events call succeeded → fully working
    if login_sc == 200 and events_sc == 200:
        return {
            'reachable': True, 'status_code': 200,
            'reason': 'Reachable',
            'detail': 'Kiosk two-step session flow working end-to-end.',
        }

    # Login worked but events didn't — partial issue
    if login_sc == 200 and events_sc is not None:
        return {
            'reachable': False, 'status_code': events_sc,
            'reason': f'Login OK, currentevents HTTP {events_sc}',
            'detail': fetch['events_body'] or 'No response body.',
        }

    # Login failed with something other than 403/200
    return {
        'reachable': False, 'status_code': login_sc,
        'reason': f'Login returned HTTP {login_sc}',
        'detail': fetch['login_body'] or 'No response body.',
    }


def kiosk_is_reachable(probe_email):
    """Boolean convenience wrapper around kiosk_probe_status."""
    return kiosk_probe_status(probe_email)['reachable']


def _fetch_kiosk_state(email):
    """
    Fetch kiosk clock-in state for a single volunteer by email.
    Returns a dict keyed by event_id with { is_signed_in, checkin_date,
    event_user_account_id } — or None on failure.

    Uses the two-step session flow (kiosk login → currentevents) — see
    _kiosk_session_fetch for why that's required.
    """
    if not email:
        return None

    fetch = _kiosk_session_fetch(email, timeout=KIOSK_TIMEOUT)
    data = fetch.get('events_data')
    if not isinstance(data, list):
        return None

    result = {}
    for entry in data:
        eid = entry.get('event_id')
        if eid is None:
            continue
        result[eid] = {
            'is_signed_in': bool(entry.get('is_signed_in')),
            'checkin_date': entry.get('checkin_date'),
            'event_user_account_id': entry.get('event_user_account_id'),
        }
    return result


@st.cache_data(ttl=KIOSK_CACHE_TTL, show_spinner=False)
def get_kiosk_states(emails_tuple):
    """
    Parallel fetch of kiosk clock-in state for each email.
    Cached 30s so rapid reruns don't re-poll.

    Returns: {email.lower() -> {event_id -> {is_signed_in, checkin_date, eua_id}}}
    """
    emails = [e for e in emails_tuple if e]
    if not emails:
        log_kiosk.debug("get_kiosk_states: empty email list")
        return {}

    log_kiosk.info(
        "batch start: polling %d email(s): %s",
        len(emails), ', '.join(emails)
    )
    t0 = time.time()

    result = {}
    success_count = 0
    with ThreadPoolExecutor(max_workers=KIOSK_WORKERS) as pool:
        futures = {pool.submit(_fetch_kiosk_state, e): e for e in emails}
        for f in as_completed(futures):
            email = futures[f]
            try:
                r = f.result() or {}
                result[email] = r
                if r:
                    success_count += 1
            except Exception as e:
                result[email] = {}
                log_kiosk.error(
                    "batch worker EXC email=%s: %s", email, str(e)[:200]
                )

    log_kiosk.info(
        "batch done: %d/%d successful in %.0fms",
        success_count, len(emails), (time.time() - t0) * 1000,
    )
    return result


# ─── Processing ───────────────────────────────────────────────────────────────
def build_roster(shifts_raw, target_dates):
    """Flatten shifts into (person, shift) records for the given date set."""
    if not isinstance(shifts_raw, list):
        return [], []

    roster = []
    uids = set()
    # Dedup key MUST include the date. Bloomerang reuses the same shift id
    # (sid) across multiple days when the shift is part of a recurring series
    # — so dedup'ing only on (sid, uid) collapses "John at 2 PM Friday" and
    # "John at 2 PM Saturday" into a single entry, attributed to whichever
    # we saw first. Including the date separates them correctly.
    seen = set()
    dup_count = 0
    skipped_no_date = 0

    for shift in shifts_raw:
        s_start = _parse_bloomerang_ts(shift.get('startDate'))
        s_end = _parse_bloomerang_ts(shift.get('endDate'))
        if s_start is None or s_end is None:
            skipped_no_date += 1
            continue

        if s_start.date() not in target_dates:
            continue

        sid = shift['id']
        for role in shift.get('roles', []):
            r_name = "Volunteer"
            texts = role.get('eventRoleTexts') or []
            if texts and isinstance(texts, list):
                r_name = texts[0].get('eventRoleName', 'Volunteer')

            for user in role.get('users', []):
                uid = user.get('id')
                if not uid:
                    continue
                # Include the start datetime (to the minute) in the dedup key.
                # Two legitimate shifts for the same user always differ in
                # start time; two duplicate records for the same shift have
                # an identical start.
                key = (sid, uid, s_start.isoformat())
                if key in seen:
                    dup_count += 1
                    continue
                seen.add(key)

                fname = (user.get('firstName') or '').strip()
                lname = (user.get('lastName') or '').strip()
                if not fname or fname.lower() in ('none', 'volunteer'):
                    fname = 'Unknown'
                    lname = f'(ID: {uid})'

                # Capture identifiers needed to cross-reference with the kiosk
                # endpoint: email is the login, eventUserAccountId is the stable
                # per-(event,user) id that kiosk clock-in actions key off of.
                email = (user.get('username') or '').strip().lower() or None
                event_user_account_id = user.get('eventUserAccountId')

                roster.append({
                    'uid': uid, 'sid': sid,
                    'fname': fname, 'lname': lname,
                    'role': r_name,
                    'start': s_start, 'end': s_end,
                    'email': email,
                    'eua_id': event_user_account_id,
                })
                uids.add(uid)

    log_api.info(
        "build_roster: %d entries from %d raw shifts (%d dup records skipped, "
        "%d unparseable dates); target_dates=%s",
        len(roster), len(shifts_raw), dup_count, skipped_no_date,
        sorted(str(d) for d in target_dates),
    )
    return roster, list(uids)


def _parse_bloomerang_ts(raw):
    """
    Parse a Bloomerang timestamp string. Bloomerang's API returns timestamps
    either as `2026-04-11T18:01:23.000Z` (explicit UTC) or as bare
    `2026-04-11T18:01:23` (implicit UTC, no suffix). Python's fromisoformat
    with a naive string returns a naive datetime, which .astimezone() then
    misinterprets using the server's local zone. We normalize by:
      1. Stripping Z and attaching explicit UTC
      2. If still naive after parsing, attaching UTC anyway
      3. Converting to LOCAL_TZ

    Returns a timezone-aware datetime in LOCAL_TZ, or None on failure.
    """
    if not raw:
        return None
    try:
        s = raw.replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ)
    except Exception:
        return None


def assign_punches(user_shifts, user_punches, t_date):
    """
    Given one volunteer's shifts and punches for a single date, decide which
    punch (if any) belongs to which shift.

    Per the Bloomerang API spec (serviceTime schema):
      - startTimestamp / endTimestamp are in UTC with ".000Z" suffix
      - dayDate is a local-timezone calendar date (YYYY-MM-DD)
      - isActive=true means the record is current data; false = superseded
      - checkinTypeId:     1 = general check-in,     2 = shift clock-in
      - serviceTimeTypeId: 1 = realtime (clock-in),  2 = manually entered

    Algorithm:
      1. Drop superseded records AND records from other events — a volunteer
         can have serviceTime entries for multiple events on the same day.
      2. Exact shift-id pass: punch.eventShiftId matches a scheduled shift.
      3. Coverage pass: a single clocked-in session can cover multiple
         consecutive shifts when cin ≤ shift_end and (cout is None OR
         cout ≥ shift_start). This handles the common case of a volunteer
         who signs up for back-to-back hours, clocks in once, works
         continuously, and clocks out at the end.
      4. Proximity fallback for the rare stray punch.

    Returns dict: {str(shift_id) -> punch_record}.
    """
    if not user_punches or not user_shifts:
        return {}

    iso_day = t_date.isoformat()

    # Drop superseded records AND records from other events. A volunteer
    # can have serviceTime entries across many events in the same org;
    # a record stamped with eventId=63116 must not match shifts in our
    # event 51764, even if the time looks right.
    active_punches = []
    for p in user_punches:
        is_active = p.get('isActive')
        if is_active is False or is_active == 0:
            continue
        # Only keep records for OUR event. eventId=None (event-less check-in)
        # is acceptable because those can still be matched to a shift via time
        # coverage — but a record stamped with a DIFFERENT event id is not ours.
        p_event = p.get('eventId')
        if p_event is not None and p_event != EVENT_ID:
            continue
        active_punches.append(p)

    # Filter to today's candidates with parsed anchor times
    candidates = []
    for p in active_punches:
        start_raw = p.get('startTimestamp')
        end_raw = p.get('endTimestamp')

        if start_raw:
            anchor_start = _parse_bloomerang_ts(start_raw)
            if anchor_start is None:
                continue
            if anchor_start.date() != t_date:
                continue
            anchor_end = _parse_bloomerang_ts(end_raw) if end_raw else None
        else:
            # Manually entered record — no timestamps, just dayDate
            day = p.get('dayDate', '')
            if not day.startswith(iso_day):
                continue
            anchor_start = None
            anchor_end = None

        candidates.append({
            'punch': p,
            'start': anchor_start,
            'end': anchor_end,
        })

    if not candidates:
        return {}

    assigned = {}
    shifts_by_sid = {str(s['sid']): s for s in user_shifts}

    # Step 2 — exact shift-id match (authoritative when present)
    shift_id_matched = set()
    for c in candidates:
        p_sid = c['punch'].get('eventShiftId')
        if p_sid is not None and str(p_sid) in shifts_by_sid:
            key = str(p_sid)
            existing = assigned.get(key)
            if existing is None:
                assigned[key] = c['punch']
                shift_id_matched.add(id(c['punch']))
            else:
                if c['punch'].get('startTimestamp') and not existing.get('startTimestamp'):
                    assigned[key] = c['punch']
                    shift_id_matched.add(id(c['punch']))

    # Step 3 — coverage pass: one session covers multiple consecutive shifts
    # when its time range overlaps the shift's time range. A "session" is
    # (cin, cout). For each candidate with a real time range, find every
    # unclaimed shift whose window overlaps, and claim them.
    #
    # "Overlap" here is generous: cin must be within 30 min before shift_end
    # AND cout (if present) must be within 30 min after shift_start. In plain
    # English: the clock-in happened before or during the shift, and the
    # clock-out (if any) happened during or after.
    for c in candidates:
        if c['start'] is None:
            continue  # manually entered, no time range
        cin = c['start']
        cout = c['end']  # may be None if still clocked in

        # How late-start are we willing to forgive? 30 min of grace.
        latest_valid_cin = lambda s: s['end'] + timedelta(minutes=30)
        # How early-finish are we willing to forgive? If cout is before the
        # shift even started by more than 30 min, it didn't cover the shift.
        earliest_valid_cout = lambda s: s['start'] - timedelta(minutes=30)

        for s in user_shifts:
            sid_key = str(s['sid'])
            if sid_key in assigned:
                continue  # already claimed

            # Did this session cover this shift?
            if cin > latest_valid_cin(s):
                continue  # clocked in too late (after shift was already over + 30min)
            if cout is not None and cout < earliest_valid_cout(s):
                continue  # clocked out before shift began (+30min grace)

            # Session covers the shift.
            assigned[sid_key] = c['punch']

    # Step 4 — proximity fallback for any still-unclaimed shifts and any
    # remaining punches that weren't used in coverage (e.g., because they
    # were brief or fell entirely outside any shift's window).
    unclaimed_shifts = [s for s in user_shifts if str(s['sid']) not in assigned]
    used_punch_ids = {id(p) for p in assigned.values()}
    remaining_candidates = [c for c in candidates if id(c['punch']) not in used_punch_ids]

    scored = []
    for c in remaining_candidates:
        if c['start'] is None:
            continue
        for s in unclaimed_shifts:
            if c['start'] < s['start'] - timedelta(minutes=30):
                continue
            if c['start'] > s['end'] + timedelta(minutes=60):
                continue
            distance = abs((c['start'] - s['start']).total_seconds())
            scored.append((distance, c, s))

    scored.sort(key=lambda t: t[0])

    used_in_proximity = set()
    for distance, c, s in scored:
        sid_key = str(s['sid'])
        if sid_key in assigned:
            continue
        pid = id(c['punch'])
        if pid in used_in_proximity:
            continue
        assigned[sid_key] = c['punch']
        used_in_proximity.add(pid)

    return assigned


def find_punch(user_punches, shift_info, t_date, _cache=None):
    """
    Return the punch for this shift on this date, or None.

    This is a thin wrapper around assign_punches; the real work is one-to-one
    allocation across a user's whole day. A cache keyed by (uid, date) avoids
    re-running the allocator for each shift belonging to the same user.
    """
    return None  # overridden below — this function is now only called with pre-computed results


def needs_kiosk_poll(shift_info, punch, now):
    """
    Decide whether a given shift needs a fresh kiosk-state fetch right now.

    Polling strategy: CONTINUOUS during the shift's active window. A prior
    version had two separate windows (clock-in watch + clock-out watch) with
    a "dead zone" gap in between, which caused shifts to incorrectly flip to
    No Show between start+30min and end-10min when the kiosk state cache
    expired during that gap.

    Windows:
      • [start - 10m, end + 30m]   — continuous live polling for the whole
        expected presence window. Covers clock-in watch, shift in progress,
        and clock-out watch as one unbroken range.
      • [end + 85m, end + 95m]     — final late-clock-out check for anyone
        whose endTimestamp never arrived.
    """
    start, end = shift_info['start'], shift_info['end']

    # Complete punch records — no polling needed
    if punch:
        cin_raw = punch.get('startTimestamp')
        cout_raw = punch.get('endTimestamp')
        if not cin_raw and not cout_raw:
            return False  # manager-fix, finalized
        if cin_raw and cout_raw:
            return False  # cin + cout both present, finalized
        # cin only: fall through — keep polling through end+30 to catch cout

    # Primary continuous window — from 10 min before start through 30 min
    # after end. No gap. If the shift is active right now (or just about to
    # be, or just wrapped up), we want live data.
    if start - timedelta(minutes=10) <= now <= end + timedelta(minutes=30):
        return True

    # Final clock-out check at end + 90m (±5 min window so the Streamlit
    # refresh cadence reliably catches it). Only if we still don't have
    # endTimestamp on record.
    if end + timedelta(minutes=85) <= now <= end + timedelta(minutes=95):
        if not punch or not punch.get('endTimestamp'):
            return True

    return False


def classify(shift_info, punch, now, kiosk_state=None):
    """
    Public wrapper around _classify_raw that logs each classification.
    See _classify_raw for the full decision tree.
    """
    status, css, cin, cout = _classify_raw(shift_info, punch, now, kiosk_state)

    # Summarize inputs briefly for the log line
    punch_desc = 'none'
    if punch:
        p_cin = punch.get('startTimestamp')
        p_cout = punch.get('endTimestamp')
        p_sid = punch.get('eventShiftId')
        punch_desc = f"cin={p_cin} cout={p_cout} shiftId={p_sid}"

    kiosk_desc = 'none'
    if kiosk_state:
        kiosk_desc = (
            f"is_signed_in={kiosk_state.get('is_signed_in')} "
            f"checkin_date={kiosk_state.get('checkin_date')}"
        )

    log_match.debug(
        "classify uid=%s sid=%s name=%s date=%s shift=%s..%s → %s | punch=%s | kiosk=%s",
        shift_info.get('uid'),
        shift_info.get('sid'),
        f"{shift_info.get('fname','')} {shift_info.get('lname','')}".strip(),
        shift_info['start'].strftime('%Y-%m-%d'),
        shift_info['start'].strftime('%H:%M'),
        shift_info['end'].strftime('%H:%M'),
        status,
        punch_desc,
        kiosk_desc,
    )
    return status, css, cin, cout


def _classify_raw(shift_info, punch, now, kiosk_state=None):
    """
    Determine shift status. Uses these sources in priority order:

    1. Completed punch record (cin + cout) → Completed
    2. Manager-fix record (both timestamps null) → Completed (Fixed)
    3. Clock-in-only punch + kiosk says is_signed_in → On Shift
    4. Clock-in-only punch + past end + 90m → Did Not Clock Out (final)
    5. Clock-in-only punch + past end + 30m → Missing Clock-Out
    6. Clock-in-only punch within shift window → On Shift
    7. No punch + kiosk is_signed_in=True (today) → On Shift (live)
    8. No punch + kiosk is_signed_in=False, but checkin_date is today
       and falls in this shift's window → Completed (pending serviceTime
       sync — handles the gap between someone clocking out on the kiosk
       and Bloomerang's serviceTime API reflecting that with a full record)
    9. Past shift start by 30m+, no evidence → No Show (final)
    10. Past shift start, no clock-in visible → Late
    11. Future / in-window → Scheduled / Starting Soon / In Progress

    Returns (status_label, css_class, clock_in_dt, clock_out_dt).
    """
    start, end = shift_info['start'], shift_info['end']

    # Completed or manager-fix punch records are authoritative history
    if punch:
        cin_raw = punch.get('startTimestamp')
        cout_raw = punch.get('endTimestamp')
        cin = _parse_bloomerang_ts(cin_raw)
        cout = _parse_bloomerang_ts(cout_raw)

        # Manager-fix entry — hours credited manually
        if not cin and not cout:
            return 'Completed (Fixed)', 'status-completed', None, None

        if cin and cout:
            return 'Completed', 'status-completed', cin, cout

        if cin and not cout:
            # Clocked in but not out. Kiosk state wins when available.
            if kiosk_state and kiosk_state.get('is_signed_in'):
                return 'On Shift', 'status-checked-in', cin, None
            # Final lock-in: 90+ min past end with no clock-out = definitely failed to clock out
            if now > end + timedelta(minutes=90):
                return 'Did Not Clock Out', 'status-alert-amber', cin, None
            if now > end + timedelta(minutes=LATE_OUT_MINUTES):
                return 'Missing Clock-Out', 'status-alert-amber', cin, None
            return 'On Shift', 'status-checked-in', cin, None

    # ── No punch record — fall back to live kiosk state ──────────────────
    # Important cases to handle here:
    #
    #  A. is_signed_in=True AND we're well within shift window or just past
    #     end → they are currently clocked in, show On Shift live. Don't
    #     bound too narrowly: a volunteer can legitimately clock in more
    #     than an hour before their shift (e.g. they arrived early for dog
    #     walking and were already clocked in when their 2 PM shift began).
    #
    #  A'. is_signed_in=True BUT we're 30m+ past shift end → they clocked
    #     in but never clocked out. Show Missing Clock-Out / Did Not Clock
    #     Out based on how far past we are. Same thresholds as when we
    #     have a punch record with cin-only.
    #
    #  B. is_signed_in=False AND checkin_date is from today AND matches
    #     THIS shift's window → they clocked in AND out for this shift,
    #     but the serviceTime API hasn't written the record yet (there's
    #     observable lag — many minutes). Show as Completed with just the
    #     clock-in time so we don't falsely flip to No Show during the
    #     sync gap.
    if kiosk_state:
        is_in = kiosk_state.get('is_signed_in')
        kiosk_cin = _parse_bloomerang_ts(kiosk_state.get('checkin_date'))
        kiosk_cin_is_today = bool(kiosk_cin and kiosk_cin.date() == now.date())
        # Is the live check-in plausibly for THIS shift (roughly within
        # window)? Used to avoid attributing a morning clock-in that's
        # still active to an unrelated afternoon shift.
        kiosk_cin_near_shift = bool(
            kiosk_cin and
            start - timedelta(hours=2) <= kiosk_cin <= end + timedelta(minutes=30)
        )

        # Case A / A': currently signed in
        if is_in and kiosk_cin_is_today and kiosk_cin_near_shift:
            # Past end + 90m and still signed in → definitely didn't clock out
            if now > end + timedelta(minutes=90):
                return 'Did Not Clock Out', 'status-alert-amber', kiosk_cin, None
            # Past end + 30m → starting to worry about clock-out
            if now > end + timedelta(minutes=LATE_OUT_MINUTES):
                return 'Missing Clock-Out', 'status-alert-amber', kiosk_cin, None
            # Within normal shift envelope → On Shift
            return 'On Shift', 'status-checked-in', kiosk_cin, None

        # Case B: signed out, last check-in was today, and falls within
        # this shift's window. Completed-pending-sync.
        if (not is_in and kiosk_cin_is_today
                and start - timedelta(minutes=30) <= kiosk_cin <= end + timedelta(minutes=30)
                and now >= start):
            return 'Completed', 'status-completed', kiosk_cin, None

    # ── No evidence of attendance ─────────────────────────────────────────
    if now < start - timedelta(minutes=UPCOMING_MINUTES):
        return 'Scheduled', 'status-pending', None, None
    if now < start:
        return 'Starting Soon', 'status-upcoming', None, None
    # Shift has started but nothing visible
    if now <= start + timedelta(minutes=10):
        # Grace period — they may just be clocking in right now
        return 'In Progress', 'status-in-progress', None, None
    if now <= start + timedelta(minutes=30):
        return 'Late', 'status-alert-amber', None, None
    return 'No Show', 'status-alert-red', None, None


# ─── Rendering ────────────────────────────────────────────────────────────────
def render_meta_bar(counts, total, sync_time=None, kiosk_status=None, show_kiosk=False):
    """
    Render the status bar above the card grid.

    kiosk_status: dict from kiosk_probe_status() — tells us reachable/not and why
    show_kiosk: whether to display the kiosk status chip (only for today's section)
    """
    done = counts.get('Completed', 0) + counts.get('Completed (Fixed)', 0)
    on = counts.get('On Shift', 0)
    inprog = counts.get('In Progress', 0)
    up = counts.get('Starting Soon', 0)
    sched = counts.get('Scheduled', 0)
    late = counts.get('Late', 0)
    miss = counts.get('Missing Clock-Out', 0)
    dnc = counts.get('Did Not Clock Out', 0)
    ns = counts.get('No Show', 0)
    alerts = late + miss + dnc + ns

    parts = [f'<div class="stat"><b>{total}</b> shifts</div>']
    if done:   parts.append(f'<div class="stat"><span class="dot dot-purple"></span><b>{done}</b> completed</div>')
    if on:     parts.append(f'<div class="stat"><span class="dot dot-green"></span><b>{on}</b> on shift</div>')
    if inprog: parts.append(f'<div class="stat"><span class="dot dot-amber"></span><b>{inprog}</b> in progress</div>')
    if up:     parts.append(f'<div class="stat"><span class="dot dot-blue"></span><b>{up}</b> starting soon</div>')
    if sched:  parts.append(f'<div class="stat"><span class="dot dot-gray"></span><b>{sched}</b> scheduled</div>')
    if alerts: parts.append(f'<div class="stat"><span class="dot dot-red"></span><b>{alerts}</b> needs attention</div>')

    # Kiosk status chip — explicit, always shown for today regardless of whether
    # it's reachable. No guessing.
    if show_kiosk and kiosk_status is not None:
        if kiosk_status.get('reachable'):
            parts.append(
                '<div class="stat" style="color:#10b981;" '
                f'title="{kiosk_status.get("detail","")}">'
                '<span class="dot dot-green" '
                'style="animation:pulse 1.5s ease-in-out infinite;"></span>'
                '<b>LIVE</b> clock-in state</div>'
            )
        else:
            sc = kiosk_status.get('status_code')
            reason = kiosk_status.get('reason', 'Unavailable')
            detail = (kiosk_status.get('detail') or '').replace('"', "'")
            sc_str = f" ({sc})" if sc else ""
            parts.append(
                f'<div class="stat" style="color:#f59e0b;" title="{detail}">'
                f'<span class="dot dot-amber"></span>'
                f'<b>Kiosk:</b>&nbsp;{reason}{sc_str}</div>'
            )

    if sync_time:
        parts.append(f'<div class="stat" style="margin-left:auto; color:#64748b;">'
                     f'Last sync: {sync_time.strftime("%I:%M:%S %p")}</div>')

    st.markdown(f'<div class="meta-bar">{"".join(parts)}</div>', unsafe_allow_html=True)


def render_card(card, debug=False):
    v = card['v']
    status, css = card['status'], card['css']
    cin, cout = card['cin'], card['cout']
    full_name = f"{v['fname']} {v['lname']}".strip()

    if cin or cout:
        cin_str = cin.strftime('%I:%M %p') if cin else '--'
        cout_str = cout.strftime('%I:%M %p') if cout else '--'
        punch_box = f'<div class="punch-box">🕒 In: {cin_str} → Out: {cout_str}</div>'
    elif status == 'Completed (Fixed)':
        punch_box = '<div class="punch-box">✎ Hours manually credited</div>'
    else:
        punch_box = ''

    time_str = f"{v['start'].strftime('%I:%M %p')} — {v['end'].strftime('%I:%M %p')}"

    # Debug footer: show shift's own ID and the matched punch's shift ID
    debug_footer = ''
    if debug:
        matched_sid = card.get('matched_sid', 'none')
        via_proximity = card.get('matched_via_proximity', False)
        available = card.get('available_sids', [])

        if via_proximity:
            marker = '≈ matched by time proximity (punch had no eventShiftId)'
        elif matched_sid == v['sid'] or str(matched_sid) == str(v['sid']):
            marker = '✓ exact id match'
        elif matched_sid == 'none':
            marker = '∅ no match'
        else:
            marker = '⚠︎ MISMATCH'

        shift_sid_display = f"{v['sid']} ({type(v['sid']).__name__})"
        match_display = f"{matched_sid} ({type(matched_sid).__name__})"

        avail_display = ''
        if matched_sid == 'none' and available:
            pairs = [f"{s} ({type(s).__name__})" for s in available]
            avail_display = f'<br/>today\'s punches had eventShiftIds: {", ".join(pairs)}'

        debug_footer = (
            f'<div style="margin-top:8px; padding:6px 10px; background:rgba(0,0,0,0.4); '
            f'border-radius:6px; font-family:JetBrains Mono,monospace; font-size:0.68rem; '
            f'color:#94a3b8; line-height:1.4;">shift.id={shift_sid_display}<br/>'
            f'matched={match_display}<br/>{marker}{avail_display}</div>'
        )

    # Single-line HTML — no indentation or blank lines that would confuse
    # Streamlit's markdown-to-HTML passthrough into rendering fragments as text.
    return (
        f'<div class="shift-card {css}">'
        f'<div class="shift-time">{time_str}</div>'
        f'<div class="shift-name">{full_name}</div>'
        f'<div class="shift-role">{v["role"]}</div>'
        f'{punch_box}'
        f'<div style="margin-top:12px;"><span class="status-badge">{status}</span></div>'
        f'{debug_footer}'
        f'</div>'
    )


# ─── UI ───────────────────────────────────────────────────────────────────────
if 'auth_data' not in st.session_state:
    st.session_state.auth_data = None
if 'credentials' not in st.session_state:
    st.session_state.credentials = None

today = datetime.now(LOCAL_TZ).date()

with st.sidebar:
    st.title("🐾 Staff Access")

    if st.session_state.auth_data is None:
        with st.form("auth_form"):
            u = st.text_input("Email")
            p = st.text_input("Password", type="password")
            if st.form_submit_button("Log In"):
                with st.spinner("Logging in..."):
                    auth = authenticate_headless(u, p)
                if auth:
                    st.session_state.auth_data = auth
                    st.session_state.credentials = {'email': u, 'password': p}
                    st.rerun()
    else:
        st.success("Connected")
        last_reauth = st.session_state.get('last_reauth')
        if last_reauth:
            ago_min = int((time.time() - last_reauth) / 60)
            if ago_min < 60:
                st.caption(f"🔑 Auth refreshed {ago_min} min ago")

        st.divider()
        st.markdown("**Date range**")
        default_range = (today, today + timedelta(days=2))
        date_sel = st.date_input(
            "Dates",
            value=st.session_state.get('_date_selection', default_range),
            help="Pick a single day or a range",
            label_visibility="collapsed",
            key='date_input_widget',
        )

        # Quick presets
        c1, c2, c3 = st.columns(3)
        if c1.button("Today", use_container_width=True):
            st.session_state['_date_selection'] = (today, today)
            st.rerun()
        if c2.button("Next 3d", use_container_width=True):
            st.session_state['_date_selection'] = (today, today + timedelta(days=2))
            st.rerun()
        if c3.button("Week", use_container_width=True):
            st.session_state['_date_selection'] = (today, today + timedelta(days=6))
            st.rerun()

        st.divider()
        if st.button("🔄 Refresh Now", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        if st.button("Logout", use_container_width=True):
            for k in ('auth_data', 'credentials', 'last_reauth', '_date_selection'):
                st.session_state.pop(k, None)
            st.rerun()
        st.session_state['debug_mode'] = st.checkbox(
            "🔬 Show shift IDs on cards",
            value=st.session_state.get('debug_mode', False),
            help="Displays each shift's id and the eventShiftId of its matched punch, "
                 "so you can verify shift→punch attribution.",
        )
        st.caption(f"Auto-refreshes every {REFRESH_SECS}s")

        # Live kiosk diagnostic — always shown so there's no guessing whether
        # live state is active. Cached probe (10min), so no extra API cost.
        # Uses the logged-in user's own email as a probe so the server gets
        # a valid username and we can distinguish "IP blocked" from "server
        # rejecting invalid input."
        st.divider()
        st.caption("**Live Kiosk Status**")
        _probe_email = (st.session_state.get('credentials') or {}).get('email')
        _probe = kiosk_probe_status(_probe_email)
        if _probe['reachable']:
            st.success(f"✅ {_probe['reason']}")
            st.caption(f"HTTP {_probe['status_code']} · {_probe['detail'][:200]}")
        else:
            st.warning(f"⚠️ {_probe['reason']}")
            sc = _probe.get('status_code')
            sc_str = f"HTTP {sc}" if sc else "no response"
            st.caption(f"{sc_str} · {(_probe.get('detail') or '')[:200]}")
            if sc == 403:
                st.caption(
                    "The kiosk endpoint only accepts requests from allowlisted "
                    "networks. Running this app from Streamlit Cloud (GCP) is "
                    "blocked. Run it locally (`streamlit run app.py`) from your "
                    "home network to enable live state."
                )

# ─── Main ─────────────────────────────────────────────────────────────────────
if not st.session_state.auth_data:
    st.info("Staff Login Required.")
    st.stop()

# Normalize the date selection into (start, end)
if isinstance(date_sel, tuple) and len(date_sel) == 2:
    start_date, end_date = date_sel
elif isinstance(date_sel, (list, tuple)) and len(date_sel) == 1:
    start_date = end_date = date_sel[0]
elif isinstance(date_sel, date):
    start_date = end_date = date_sel
else:
    start_date = end_date = today

if start_date > end_date:
    start_date, end_date = end_date, start_date

dates_in_range = []
d = start_date
while d <= end_date:
    dates_in_range.append(d)
    d += timedelta(days=1)

# Title
if len(dates_in_range) == 1:
    title_str = dates_in_range[0].strftime('%A, %B %d')
else:
    title_str = f"{dates_in_range[0].strftime('%b %d')} — {dates_in_range[-1].strftime('%b %d, %Y')}"
st.title(f"🐾 Refuge Roster — {title_str}")

# Fetch shifts
with st.spinner("Syncing Bloomerang..."):
    shifts_raw = get_shifts(st.session_state.auth_data)

if isinstance(shifts_raw, str):
    if shifts_raw == "AUTH_EXPIRED":
        st.session_state.auth_data = None
        st.error("Session expired. Please log in again.")
        st.stop()
    st.error(f"API Error: {shifts_raw}")
    st.stop()

# Build roster for date range
roster, uids = build_roster(shifts_raw, set(dates_in_range))

if not roster:
    st.info(f"No volunteers scheduled for {title_str}.")
else:
    now = datetime.now(LOCAL_TZ)
    # Only fetch serviceTime if at least one date in range is today or earlier.
    # Future-only ranges don't need it — no records exist yet.
    need_service = any(d <= now.date() for d in dates_in_range)
    if need_service:
        with st.spinner("Syncing shift history..."):
            punches = get_service_times(
                st.session_state.auth_data, tuple(sorted(uids)))
    else:
        punches = {uid: [] for uid in uids}

    # Live kiosk state — only meaningful when TODAY is in the range.
    # We ONLY poll the kiosk for volunteers who have at least one shift
    # currently in an active "check window" (±10min of start, ±30min of end,
    # etc. — see needs_kiosk_poll). Shifts outside those windows have stable
    # state, so polling them is wasted API traffic.
    today_in_range = any(d == now.date() for d in dates_in_range)
    kiosk_states_by_email = {}
    kiosk_status = None       # structured probe result for UI display
    kiosk_available = False

    if today_in_range:
        _probe_email = (st.session_state.get('credentials') or {}).get('email')
        kiosk_status = kiosk_probe_status(_probe_email)
        kiosk_available = kiosk_status['reachable']
        log_kiosk.info(
            "probe result: reachable=%s reason=%s",
            kiosk_available, kiosk_status.get('reason'),
        )

    # Group roster by date first — we need allocations before deciding who to poll
    by_date = {}
    for v in roster:
        d_key = v['start'].date()
        by_date.setdefault(d_key, []).append(v)

    # Build today's allocations now (rather than per-section in the render loop)
    # so we can make the "who needs polling" decision with full information.
    allocations_by_date = {}
    for date_key, shifts_for_date in by_date.items():
        shifts_by_uid = {}
        for v in shifts_for_date:
            shifts_by_uid.setdefault(v['uid'], []).append(v)
        allocations_by_date[date_key] = {}
        for uid, user_shifts in shifts_by_uid.items():
            user_punches = punches.get(uid, []) if need_service else []
            allocations_by_date[date_key][uid] = assign_punches(
                user_shifts, user_punches, date_key)

    # Figure out which emails need a live kiosk fetch right now
    if today_in_range and kiosk_available:
        today_shifts = by_date.get(now.date(), [])
        emails_to_poll = set()
        shifts_in_window = 0
        for v in today_shifts:
            if not v.get('email'):
                continue
            alloc = allocations_by_date[now.date()].get(v['uid'], {})
            punch_for_shift = alloc.get(str(v['sid']))
            if needs_kiosk_poll(v, punch_for_shift, now):
                emails_to_poll.add(v['email'])
                shifts_in_window += 1

        log_kiosk.info(
            "poll decision: %d shift(s) in active window → %d unique email(s) to poll",
            shifts_in_window, len(emails_to_poll),
        )

        if emails_to_poll:
            with st.spinner("Checking live clock-in state..."):
                kiosk_states_by_email = get_kiosk_states(
                    tuple(sorted(emails_to_poll)))
        else:
            log_kiosk.debug("no shifts need polling right now — skipping batch")

    # Render each date section
    for section_idx, date_key in enumerate(sorted(by_date.keys())):
        shifts_for_date = by_date[date_key]
        is_today = (date_key == now.date())
        allocation_by_uid = allocations_by_date[date_key]

        cards = []
        counts = {}
        for v in shifts_for_date:
            alloc = allocation_by_uid.get(v['uid'], {})
            p = alloc.get(str(v['sid']))

            # Pull kiosk state for this volunteer/event if available
            kiosk_state = None
            if is_today and kiosk_available and v.get('email'):
                per_event = kiosk_states_by_email.get(v['email'], {})
                kiosk_state = per_event.get(EVENT_ID)

            status, css, cin, cout = classify(v, p, now, kiosk_state)
            counts[status] = counts.get(status, 0) + 1

            # Debug: list all today's eventShiftIds in this user's punches
            available_sids = []
            if st.session_state.get('debug_mode') and need_service:
                user_punches = punches.get(v['uid'], [])
                for pp in user_punches:
                    st_raw = pp.get('startTimestamp')
                    if st_raw:
                        dt = _parse_bloomerang_ts(st_raw)
                        if dt and dt.date() == date_key:
                            available_sids.append(pp.get('eventShiftId'))

            cards.append({
                'v': v, 'status': status, 'css': css, 'cin': cin, 'cout': cout,
                'matched_sid': p.get('eventShiftId') if p else 'none',
                'available_sids': available_sids,
                'matched_via_proximity': p is not None and p.get('eventShiftId') is None,
                'kiosk_state': kiosk_state,
            })

        cards.sort(key=lambda c: c['v']['start'])

        # Section header
        today_badge = '<span class="today-badge">Today</span>' if is_today else ''
        st.markdown(
            f'<div class="date-section-header">{date_key.strftime("%A, %B %d")}'
            f'{today_badge}</div>',
            unsafe_allow_html=True
        )
        # Show sync time only on the first section to avoid clutter.
        # Kiosk status chip shows on today's section — green "LIVE" when up,
        # amber "Kiosk: <reason>" when blocked, so it's never ambiguous.
        render_meta_bar(
            counts, len(cards),
            sync_time=now if section_idx == 0 else None,
            kiosk_status=kiosk_status,
            show_kiosk=is_today,
        )

        # Card grid
        cols = st.columns(4)
        for idx, card in enumerate(cards):
            with cols[idx % 4]:
                st.markdown(render_card(card, debug=st.session_state.get('debug_mode', False)),
                            unsafe_allow_html=True)

# ─── Debug Log panel ──────────────────────────────────────────────────────────
# Collapsible at the bottom of the board. Shows the last ~1000 log lines
# from this Streamlit session. Download button lets you save a snapshot
# for sharing/debugging.
buf = st.session_state.get('log_buffer')
if buf is not None:
    st.divider()
    with st.expander(f"🔬 Debug Log ({len(buf)} lines)", expanded=False):
        if not buf:
            st.caption("No log entries yet.")
        else:
            log_text = '\n'.join(buf)
            # Newest-first so you see recent activity without scrolling
            reversed_text = '\n'.join(reversed(list(buf)))
            c1, c2 = st.columns([1, 4])
            with c1:
                st.download_button(
                    "⬇️ Download log.txt",
                    data=log_text,
                    file_name=f"refugeboard-{datetime.now(LOCAL_TZ).strftime('%Y%m%d-%H%M%S')}.log",
                    mime="text/plain",
                    use_container_width=True,
                )
                if st.button("🗑 Clear", use_container_width=True):
                    buf.clear()
                    st.rerun()
            with c2:
                st.caption(
                    f"Showing most recent first. Buffer capped at "
                    f"{LOG_BUFFER_SIZE} lines."
                )
            st.code(reversed_text, language='log')

# ─── Auto-refresh ─────────────────────────────────────────────────────────────
time.sleep(REFRESH_SECS)
st.rerun()
