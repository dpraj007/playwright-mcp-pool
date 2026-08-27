#!/usr/bin/env python3
"""Refresh the pool's login seed.

Exports a Playwright storage_state (cookies + localStorage) from ONE logged-in
persistent browser profile into state.json, so every isolated backend the pool
spawns starts already signed in.

    python export_state.py

Source profile resolution (first match wins):
  1. EXPORT_PROFILE env var
  2. config/paths.json -> "browser_profile"

The source profile must be CLOSED while exporting - a running browser holds
Chromium's SingletonLock and the cookie database copies inconsistently.

Sites whose localStorage you also want captured must be visited during the
export; set EXPORT_WARM to a comma-separated list of URLs. Cookies are captured
whether or not a site is warmed.

State flows one way: profile -> state.json -> pool. Pool backends are
--isolated and ephemeral, so a login performed inside a pool session is lost
when that session closes. Sign in on the persistent profile, then re-run this.

Requires: pip install playwright && python -m playwright install chromium
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("EXPORT_OUT", os.path.join(HERE, "state.json"))
WARM = [u.strip() for u in os.environ.get("EXPORT_WARM", "").split(",") if u.strip()]


def resolve_profile():
    p = os.environ.get("EXPORT_PROFILE")
    if p:
        return p
    try:
        cfg = json.load(open(os.path.join(HERE, "config", "paths.json"),
                             encoding="utf-8"))
        return cfg.get("browser_profile")
    except Exception:
        return None


def old_cookie_count():
    try:
        return len(json.load(open(OUT, encoding="utf-8")).get("cookies", []))
    except Exception:
        return None


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: Playwright Python is not installed.\n"
              "  pip install playwright && python -m playwright install chromium",
              file=sys.stderr)
        sys.exit(1)

    profile = resolve_profile()
    if not profile or not os.path.isdir(profile):
        print("ERROR: source profile not found (%s).\n"
              "Set EXPORT_PROFILE=<path> or config/paths.json 'browser_profile' "
              "to the persistent Playwright profile directory you log into "
              "(the same path you pass to @playwright/mcp --user-data-dir)."
              % profile, file=sys.stderr)
        sys.exit(1)

    before = old_cookie_count()
    made_backup = False
    if os.path.exists(OUT):
        shutil.copyfile(OUT, OUT + ".bak")
        made_backup = True
        print("backed up previous -> %s.bak (%s cookies)" % (OUT, before))

    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                profile, headless=True, args=["--force-device-scale-factor=1"])
            try:
                for url in WARM:
                    try:
                        pg = ctx.new_page()
                        pg.goto(url, wait_until="domcontentloaded", timeout=20000)
                        pg.wait_for_timeout(1500)
                        pg.close()
                    except Exception as e:
                        print("  warm %s: %s" % (url, e), file=sys.stderr)
                state = ctx.storage_state(path=OUT)
            finally:
                ctx.close()
    except Exception as e:
        msg = str(e).lower()
        if "in use" in msg or "singletonlock" in msg or "process" in msg:
            print("ERROR: source profile is LOCKED - a browser is using %s.\n"
                  "Close that browser (or its chrome.exe) and re-run." % profile,
                  file=sys.stderr)
        else:
            print("ERROR exporting: %s" % e, file=sys.stderr)
        # Never leave a half-written seed behind - but only restore the backup
        # this run made, or a stale one from an earlier run gets promoted back
        # into place and silently presented as current.
        if made_backup and os.path.exists(OUT + ".bak"):
            shutil.copyfile(OUT + ".bak", OUT)
            print("restored previous state.json from backup", file=sys.stderr)
        sys.exit(1)

    after = len(state.get("cookies", []))
    delta = "" if before is None else "  (was %d, %+d)" % (before, after - before)
    print("wrote %s" % OUT)
    print("  source profile: %s" % profile)
    print("  cookies: %d%s" % (after, delta))
    print("  origins with localStorage: %d" % len(state.get("origins", [])))
    print("Done. New browser_new_session calls carry the refreshed logins; "
          "close and reopen any already-open pool sessions to pick them up.")


if __name__ == "__main__":
    main()
