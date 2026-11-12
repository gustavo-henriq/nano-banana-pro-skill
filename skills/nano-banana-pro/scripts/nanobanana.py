#!/usr/bin/env python3
"""Generate or edit images with Nano Banana Pro through the Gemini web app.

It reuses the Google session of the logged-in user (cookies), through the
unofficial `gemini-webapi` library. It is NOT the official API.

Usage:
  python nanobanana.py "<prompt>" -o out.png [--aspect 3:4] [--ref a.png --ref b.png] [-n 1]
  python nanobanana.py --prompt-file spec.json -o out.png --aspect 4:5 --model gemini-carbon
  python nanobanana.py --quota            # show the remaining daily credits and exit

Credentials, in this order:
  1. env vars GEMINI_1PSID and GEMINI_1PSIDTS
  2. a cookies file: $NANO_BANANA_HOME/cookies.json or ~/.nano-banana-pro/cookies.json
     (also accepted: .cookies.json next to the skill folder)
     format: {"__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."}
  3. --browser chrome|edge|firefox|brave  (reads cookies through browser-cookie3)

Output (stdout), one item per line:
  MODEL <name>              the model that was used
  SAVED <path>              one line per saved image
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


def load_cookies(browser):
    psid = os.environ.get("GEMINI_1PSID")
    psidts = os.environ.get("GEMINI_1PSIDTS")
    if psid:
        return psid, psidts

    for cookie_file in COOKIE_FILES:
        if cookie_file.exists():
            data = json.loads(cookie_file.read_text(encoding="utf-8"))
            psid = data.get("__Secure-1PSID")
            if psid:
                return psid, data.get("__Secure-1PSIDTS")

    if browser:
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
            jar = {c.name: c.value for c in loader(domain_name="google.com")}
        except Exception as e:
            print(
                f"Could not read the {browser} cookies ({type(e).__name__}: {e}). "
                "Use a cookies file or environment variables instead.",
                file=sys.stderr,
            )
            sys.exit(2)
        if jar.get("__Secure-1PSID"):
            return jar["__Secure-1PSID"], jar.get("__Secure-1PSIDTS")

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

    if prompt.startswith(("{", "[")):
        prompt = (
            "Generate ONE image now. Output only the finished image, no text, no explanation "
            "and no rewritten prompt. Follow this JSON specification exactly:\n" + prompt
        )
    elif not prompt.lower().startswith(("create an image", "generate an image")):
        prompt = f"Create an image of: {prompt}"
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
    client = GeminiClient(psid, psidts)
    try:
        await client.init(timeout=90, auto_refresh=True)
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

    images = response.images
    if not images:
        message = (response.text or "").strip()
        if "limit" in message.lower() and "reset" in message.lower():
            print(f"LIMIT_REACHED {message}")
            sys.exit(4)
        print(f"No image generated. Model replied: {message[:500]}", file=sys.stderr)
        sys.exit(1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"MODEL {model}")
    for i, img in enumerate(images[: args.n]):
        name = out.name if i == 0 else f"{out.stem}_{i + 1}{out.suffix}"
        saved = await img.save(path=str(out.parent), filename=name, verbose=False)
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
    p.add_argument("-o", "--out", help="Output file path (.png)")
    p.add_argument("--aspect", help="Aspect ratio, e.g. 1:1, 16:9, 9:16, 4:5")
    p.add_argument("--ref", action="append", help="Reference/edit image (repeatable)")
    p.add_argument("-n", type=int, default=1, help="How many images to save (default 1)")
    p.add_argument("--model", help=f"gemini-carbon (default), gemini-pro, gemini-flash, gemini-flash-lite")
    p.add_argument("--browser", help="chrome | edge | firefox | brave (read cookies from the browser)")
    p.add_argument("--timeout", type=int, default=300, help="Seconds to wait for the image (default 300)")
    p.add_argument("--quota", action="store_true", help="Print the remaining credits and exit")
    p.add_argument("--verbose", action="store_true", help="Print the elapsed time")
    args = p.parse_args()
    if not args.quota and not args.out:
        p.error("-o/--out is required unless --quota is used")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
