#!/usr/bin/env python3
"""Generate or edit images (and, with --video, videos) with Nano Banana Pro through the Gemini web app.

It reuses the Google session of the logged-in user (cookies), through the
unofficial `gemini-webapi` library. It is NOT the official API.

Usage:
  python nanobanana.py "<prompt>" -o out.png [--aspect 3:4] [--ref a.png --ref b.png] [-n 1]
  python nanobanana.py --prompt-file spec.json -o out.png --aspect 4:5 --model gemini-carbon
  python nanobanana.py "<prompt>" -o out.mp4 --video [--aspect 16:9] [--ref first_frame.png]
  python nanobanana.py --quota            # show the remaining daily credits and exit

Credentials, in this order:
  1. env vars GEMINI_1PSID and GEMINI_1PSIDTS
  2. a cookies file: $NANO_BANANA_HOME/cookies.json or ~/.nano-banana-pro/cookies.json
     (also accepted: .cookies.json next to the skill folder)
     format: {"__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."}
  3. --browser chrome|edge|firefox|brave  (reads cookies through browser-cookie3)

Output (stdout), one item per line:
  MODEL <name>              the model that was used
  SAVED <path>              one line per saved image (or .mp4 with --video)
  THUMB <path>              video thumbnail (.jpg), only with --video
  RETRY n/m                 (stderr) video retry after Google returned no video
  QUOTA <text>              only when a threshold (50%, 25%, 10%, 5% left) is crossed
  LIMIT_REACHED <text>      the daily limit is over (exit code 4)

Exit codes: 0 ok, 1 no image / refused, 2 authentication, 3 missing dependency,
            4 usage limit reached, 5 timeout.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SKILL_DIR = Path(__file__).resolve().parent.parent
HOME_DIR = Path(os.environ.get("NANO_BANANA_HOME", Path.home() / ".nano-banana-pro"))
COOKIE_FILES = [HOME_DIR / "cookies.json", SKILL_DIR / ".cookies.json"]
STATE_FILE = HOME_DIR / "quota_state.json"

# gemini-webapi keeps a copy of the refreshed cookies in a cache folder (by default the shared
# system temp dir, with the session id in the file name). Keep it inside our private folder.
os.environ.setdefault("GEMINI_COOKIE_PATH", str(HOME_DIR / "cache"))

# Announce once per reset window when the remaining share drops to each of these (percent).
THRESHOLDS = (50, 25, 10, 5)
DEFAULT_MODEL = "gemini-carbon"
# Video: on retry the script alternates to this model (the one that returned a video in testing).
FALLBACK_VIDEO_MODEL = "gemini-flash-lite"
# Quota buckets reported by the web app: 4 = Pro-class models, 11 = Flash-class models.
PRO_ACTION, FLASH_ACTION = 4, 11


# --------------------------------------------------------------------------- credentials
def harden_home():
    """Make ~/.nano-banana-pro readable only by the current user (cookies live there).

    POSIX: chmod 700 on folders and 600 on files. Windows: remove inherited permissions with
    icacls and grant full control to the current user only. Runs once (a marker file records it).
    """
    marker = HOME_DIR / ".hardened"
    try:
        (HOME_DIR / "cache").mkdir(parents=True, exist_ok=True)
        if marker.exists():
            return
        if os.name == "nt":
            user = os.environ.get("USERNAME")
            if not user:
                return
            # 1) the folder keeps only an explicit entry for the current user (no inheritance)
            subprocess.run(
                ["icacls", str(HOME_DIR), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                capture_output=True, check=True, timeout=30,
            )
            # 2) existing files/folders go back to inheriting from it. Never use "/inheritance:r"
            #    on them: that would leave them with no entry at all and lock you out.
            for child in HOME_DIR.iterdir():
                subprocess.run(
                    ["icacls", str(child), "/reset", "/T", "/C"],
                    capture_output=True, check=True, timeout=30,
                )
        else:
            for p in [HOME_DIR, *HOME_DIR.rglob("*")]:
                os.chmod(p, 0o700 if p.is_dir() else 0o600)
        for p in HOME_DIR.rglob("*"):  # safety check: we must still be able to read our own files
            if p.is_file():
                with open(p, "rb") as fh:
                    fh.read(1)
        marker.write_text("permissions restricted to the current user\n", encoding="utf-8")
    except Exception as e:  # never block image generation because of this
        print(
            f"Warning: could not restrict permissions on {HOME_DIR} ({type(e).__name__}). "
            "Restrict it yourself: chmod -R go-rwx (macOS/Linux) or icacls /inheritance:r (Windows).",
            file=sys.stderr,
        )


# Since Gemini web build 20261119, __Secure-1PSID/1PSIDTS alone redirect to the Google sign-in
# page. The full set of Google cookies of a signed-in profile is needed, each one kept on its own
# domain (OSID/__Secure-OSID are host-only cookies of gemini.google.com; putting them on
# .google.com breaks image downloads with 403). See HanaokaYuzu/Gemini-API issue #381.
FULL_COOKIES = []  # [(domain, name, value)]


def _keep(domain, name, value):
    if "google.com" in (domain or "") and name and value is not None:
        FULL_COOKIES.append((domain, name, value))


def _load_browser_cookies(browser):
    """Read the full Google cookie set from a local browser profile; None if not signed in."""
    try:
        import browser_cookie3
    except ImportError:
        print("browser-cookie3 is not installed: pip install browser-cookie3", file=sys.stderr)
        sys.exit(3)
    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        print(f"Unknown browser: {browser}", file=sys.stderr)
        sys.exit(3)
    try:
        cookies = list(loader(domain_name="google.com"))
    except Exception as e:
        print(
            f"Could not read the {browser} cookies ({type(e).__name__}: {e}). "
            "Use a cookies file or environment variables instead.",
            file=sys.stderr,
        )
        sys.exit(2)
    for c in cookies:
        _keep(c.domain, c.name, c.value)
    jar = {c.name: c.value for c in cookies}
    if jar.get("__Secure-1PSID"):
        return jar["__Secure-1PSID"], jar.get("__Secure-1PSIDTS")
    FULL_COOKIES.clear()
    print(f"No Google session found in {browser}: log in to gemini.google.com there first.",
          file=sys.stderr)
    return None


def load_cookies(browser):
    psid = os.environ.get("GEMINI_1PSID")
    psidts = os.environ.get("GEMINI_1PSIDTS")
    if psid:
        return psid, psidts

    # An explicit --browser wins over a cookies file, so a stale file cannot shadow it.
    if browser:
        found = _load_browser_cookies(browser)
        if found:
            return found

    for cookie_file in COOKIE_FILES:
        if cookie_file.exists():
            data = json.loads(cookie_file.read_text(encoding="utf-8-sig"))
            if isinstance(data, list):  # Cookie-Editor export: [{name, value, domain, ...}]
                for c in data:
                    _keep(c.get("domain"), c.get("name"), c.get("value"))
                data = {n: v for _, n, v in FULL_COOKIES}
            psid = data.get("__Secure-1PSID")
            if psid:
                if len(FULL_COOKIES) < 3:
                    print("Warning: only __Secure-1PSID/1PSIDTS found. Google now needs the full cookie "
                          "set: use --browser firefox, or export all cookies of gemini.google.com "
                          "(Cookie-Editor > Export > JSON).", file=sys.stderr)
                return psid, data.get("__Secure-1PSIDTS")

    print(
        "No credentials. Set GEMINI_1PSID/GEMINI_1PSIDTS, create "
        f"{COOKIE_FILES[0]} or use --browser.",
        file=sys.stderr,
    )
    sys.exit(2)


# --------------------------------------------------------------------------- prompt
def read_prompt_file(path):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")  # files saved by old Windows tools
    return text.strip()


def build_prompt(args):
    if args.prompt_file:
        prompt = read_prompt_file(args.prompt_file)
    elif args.prompt:
        prompt = args.prompt
    else:
        print("Give a prompt or --prompt-file", file=sys.stderr)
        sys.exit(1)

    kind = "video" if args.video else "image"
    if prompt.startswith(("{", "[")):
        prompt = (
            f"Generate ONE {kind} now. Output only the finished {kind}, no text, no explanation "
            "and no rewritten prompt. Follow this JSON specification exactly:\n" + prompt
        )
    elif not prompt.lower().startswith((f"create a {kind}", f"create an {kind}",
                                        f"generate a {kind}", f"generate an {kind}")):
        prompt = f"Create a {kind} of: {prompt}" if args.video else f"Create an image of: {prompt}"
    if args.aspect:
        prompt += f"\nAspect ratio: {args.aspect}."
    return prompt


# --------------------------------------------------------------------------- quota
def _quota_buckets(client):
    """Return the quota buckets that have a real total, as dicts."""
    buckets = []
    for key, q in (getattr(client, "quotas", None) or {}).items():
        if key in ("extra", "usage_info") or not isinstance(q, dict):
            continue
        total, remaining = q.get("total"), q.get("remaining")
        if isinstance(total, (int, float)) and total > 0 and isinstance(remaining, (int, float)):
            buckets.append(q)
    return buckets


def pick_bucket(client, model):
    buckets = _quota_buckets(client)
    if not buckets:
        return None
    want = FLASH_ACTION if "flash" in (model or "") else PRO_ACTION
    for b in buckets:
        if b.get("action_id") == want:
            return b
    return min(buckets, key=lambda b: b["remaining"] / b["total"])


def describe(bucket):
    pct = bucket["remaining"] / bucket["total"] * 100
    text = f"{pct:.0f}% left ({int(bucket['remaining'])}/{int(bucket['total'])} credits)"
    reset = bucket.get("reset_time")
    if reset:
        try:
            text += f", resets at {datetime.fromtimestamp(reset).strftime('%H:%M')}"
        except (OverflowError, OSError, ValueError):
            pass
    return pct, text


def crossed_threshold(pct_left, reset_key, state):
    """Return the threshold to announce now (or None) and the updated state."""
    if state.get("reset_key") != reset_key:
        state = {"reset_key": reset_key, "announced": []}
    due = [t for t in THRESHOLDS if pct_left <= t and t not in state["announced"]]
    if not due:
        return None, state
    hit = min(due)  # the most severe threshold reached
    state["announced"] = sorted(set(state["announced"]) | {t for t in THRESHOLDS if t >= hit})
    return hit, state


def announce_quota(client, model, force=False):
    bucket = pick_bucket(client, model)
    if not bucket:
        if force:
            print("QUOTA unavailable (the account did not report credits)")
        return
    pct, text = describe(bucket)
    if force:
        print(f"QUOTA {text}")
        return
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    except Exception:
        state = {}
    hit, state = crossed_threshold(pct, bucket.get("reset_time"), state)
    try:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    if hit is not None:
        print(f"QUOTA {text}  (crossed the {hit}% mark)")


# --------------------------------------------------------------------------- run
def _patch_gemini_webapi(GeminiClient, gex):
    """Workarounds for gemini-webapi 2.1.1 against Gemini web build 20261119 (issue #381)."""
    if getattr(GeminiClient, "_nb_patched", False):
        return
    from gemini_webapi.constants import GRPC
    from gemini_webapi.types import RPCData
    orig = GeminiClient._batch_execute

    async def _batch_execute(self, payloads, *a, **kw):
        # The sign-in page also carries an SNlM0e token, so init "succeeds" on it: treat as auth failure.
        if "identityfrontend" in (getattr(self, "build_label", "") or ""):
            raise gex.AuthError("Redirected to the Google sign-in page: cookies are incomplete or expired.")
        # GET_USER_STATUS now expects payload [1]; [] returns HTTP 400.
        payloads = [RPCData(rpcid=p.rpcid, payload="[1]", identifier=p.identifier)
                    if p.rpcid == GRPC.GET_USER_STATUS and p.payload == "[]" else p for p in payloads]
        return await orig(self, payloads, *a, **kw)

    GeminiClient._batch_execute = _batch_execute
    GeminiClient._nb_patched = True


async def run(args):
    try:
        from gemini_webapi import GeminiClient, set_log_level
        import gemini_webapi.exceptions as gex
    except ImportError:
        print("gemini-webapi is not installed: pip install gemini-webapi", file=sys.stderr)
        sys.exit(3)
    set_log_level("CRITICAL")  # keep the output short; we print our own quota lines

    harden_home()
    psid, psidts = load_cookies(args.browser)
    _patch_gemini_webapi(GeminiClient, gex)
    client = GeminiClient(psid, psidts)
    if FULL_COOKIES:
        from curl_cffi.requests import Cookies
        jar = Cookies()
        for domain, name, value in FULL_COOKIES:
            jar.set(name, value, domain=domain, path="/", secure=True)
        client._cookies = jar
    try:
        # init() can deadlock forever on a non-200 init RPC (issue #381): always bound it.
        await asyncio.wait_for(client.init(timeout=90, auto_refresh=True), 60)
    except asyncio.TimeoutError:
        print("Authentication failed: Gemini init did not answer in 60s (usually incomplete or "
              "expired cookies; see 'Authentication' in SKILL.md).", file=sys.stderr)
        sys.exit(2)
    except Exception as e:  # AuthError and friends
        print(f"Authentication failed ({type(e).__name__}): {e}", file=sys.stderr)
        sys.exit(2)

    model = args.model or DEFAULT_MODEL
    if args.quota:
        announce_quota(client, model, force=True)
        return

    prompt = build_prompt(args)
    kwargs = {"model": model}
    if args.ref:
        kwargs["files"] = [str(Path(r)) for r in args.ref]

    limit_error = getattr(gex, "UsageLimitExceededError", ())
    started = time.time()
    attempts = 1 + (max(args.retries, 0) if args.video else 0)
    media = []
    for attempt in range(attempts):
        if attempt:  # video retry: Google often answers "at full capacity" or a generic error
            print(f"RETRY {attempt}/{attempts - 1} (no video returned)", file=sys.stderr)
            if not args.model:  # alternate to the model that worked in testing
                kwargs["model"] = model = FALLBACK_VIDEO_MODEL if model == DEFAULT_MODEL else DEFAULT_MODEL
            await asyncio.sleep(10)
        try:
            response = await asyncio.wait_for(client.generate_content(prompt, **kwargs), args.timeout)
        except asyncio.TimeoutError:
            print(
                f"Timed out after {args.timeout}s. This often means the daily limit is over: "
                "run with --quota to check.",
                file=sys.stderr,
            )
            sys.exit(5)
        except limit_error as e:
            print(f"LIMIT_REACHED {e}")
            sys.exit(4)

        media = response.videos if args.video else response.images
        if media:
            break
        message = (response.text or "").strip()
        if "limit" in message.lower() and "reset" in message.lower():
            print(f"LIMIT_REACHED {message}")
            sys.exit(4)

    kind = "video" if args.video else "image"
    if not media:
        hint = ""
        if not args.video and response.videos:
            hint = " (the model returned a VIDEO: rerun with --video)"
        print(f"No {kind} generated{hint}. Model replied: {message[:500]}", file=sys.stderr)
        sys.exit(1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"MODEL {model}")
    for i, item in enumerate(media[: args.n]):
        name = out.name if i == 0 else f"{out.stem}_{i + 1}{out.suffix}"
        if args.video:
            # Video links answer 206 while rendering; save() polls every 10s, so cap the wait.
            saved = await asyncio.wait_for(
                item.save(path=str(out.parent), filename=name, verbose=False), args.timeout
            )
            print(f"SAVED {saved['video']}")
            if saved.get("video_thumbnail"):
                print(f"THUMB {saved['video_thumbnail']}")
        else:
            saved = await item.save(path=str(out.parent), filename=name, verbose=False)
            print(f"SAVED {saved}")

    try:  # refresh the credits after the generation, then announce threshold crossings
        await client._fetch_quota()
    except Exception:
        pass
    announce_quota(client, model)
    if args.verbose:
        print(f"TIME {time.time() - started:.0f}s")


def main():
    p = argparse.ArgumentParser(description="Nano Banana Pro through the Gemini web app")
    p.add_argument("prompt", nargs="?", help="Detailed prompt (English works best)")
    p.add_argument("--prompt-file", help="Read the prompt from a file (use it for JSON or long text)")
    p.add_argument("-o", "--out", help="Output file path (.png, or .mp4 with --video)")
    p.add_argument("--video", action="store_true",
                   help="Generate a VIDEO (Veo) instead of an image; saves .mp4 plus a .jpg thumbnail")
    p.add_argument("--retries", type=int, default=2,
                   help="Video only: extra attempts when Google returns no video (default 2)")
    p.add_argument("--aspect", help="Aspect ratio, e.g. 1:1, 16:9, 9:16, 4:5")
    p.add_argument("--ref", action="append", help="Reference/edit image (repeatable)")
    p.add_argument("-n", type=int, default=1, help="How many images to save (default 1)")
    p.add_argument("--model", help=f"gemini-carbon (default), gemini-pro, gemini-flash, gemini-flash-lite")
    p.add_argument("--browser", help="chrome | edge | firefox | brave (read cookies from the browser)")
    p.add_argument("--timeout", type=int, default=None,
                   help="Seconds to wait (default 300 for images, 600 for video)")
    p.add_argument("--quota", action="store_true", help="Print the remaining credits and exit")
    p.add_argument("--verbose", action="store_true", help="Print the elapsed time")
    args = p.parse_args()
    if args.timeout is None:
        args.timeout = 600 if args.video else 300
    if not args.quota and not args.out:
        p.error("-o/--out is required unless --quota is used")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
