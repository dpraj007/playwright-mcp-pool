#!/usr/bin/env python3
"""Smart-clone a persistent browser profile, keeping the logins, dropping caches.

Chromium's SingletonLock forbids two live browsers sharing one profile
directory, so the fan-out instances each need their own. Copying a profile
wholesale is wasteful - most of it is cache. This copies only what carries a
session (cookies, tokens, preferences), which in practice is ~10% of the bytes.

    python clone_profile.py <source-profile> <destination-profile>

The SOURCE MUST BE CLOSED. A running browser holds the cookie database open and
it copies inconsistently - you get a profile that looks signed in but is not.

Login state lives in Default/Network/Cookies (not Default/Cookies),
Default/Login Data and Default/Local Storage.

Clones are snapshots and drift as cookies rotate; re-run when a clone goes
stale. Running the same account in several browsers at once can trip a site's
security checks - it is safe across different sites, risky in parallel against
one account.
"""
import fnmatch
import os
import shutil
import sys
import time

# Regenerated on next launch; excluding them is what makes the clone small.
SKIP_DIRS = ["Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
             "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
             "Service Worker", "component_crx_cache", "extensions_crx_cache",
             "*Cache*", "*_crx_cache"]
# Lock files: copying them makes the clone look "already in use".
SKIP_FILES = ["Singleton*", "lockfile", "LOCK", "*.lock"]


def matches(name, patterns):
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def clone(src, dst):
    copied = skipped = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not matches(d, SKIP_DIRS)]
        rel = os.path.relpath(root, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for f in files:
            if matches(f, SKIP_FILES):
                skipped += 1
                continue
            try:
                shutil.copy2(os.path.join(root, f), os.path.join(target, f))
                copied += 1
            except Exception as e:
                skipped += 1
                print("  skip %s: %s" % (f, e), file=sys.stderr)
    return copied, skipped


def size_of(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    src, dst = sys.argv[1], sys.argv[2]
    if not os.path.isdir(src):
        print("ERROR: source profile not found: %s" % src, file=sys.stderr)
        sys.exit(1)
    if os.path.exists(os.path.join(src, "SingletonLock")):
        print("WARNING: SingletonLock present - a browser may still hold %s.\n"
              "Close it first or the cookie database copies inconsistently."
              % src, file=sys.stderr)
    if os.path.exists(dst) and os.listdir(dst):
        print("ERROR: destination exists and is not empty: %s" % dst,
              file=sys.stderr)
        sys.exit(1)

    t0 = time.time()
    copied, skipped = clone(src, dst)
    print("cloned %s -> %s" % (src, dst))
    print("  %d files copied, %d skipped, %.1f MB, %.1fs"
          % (copied, skipped, size_of(dst) / 1e6, time.time() - t0))
    cookies = os.path.join(dst, "Default", "Network", "Cookies")
    print("  cookie DB: %s" % ("present" if os.path.exists(cookies) else "MISSING"))


if __name__ == "__main__":
    main()
