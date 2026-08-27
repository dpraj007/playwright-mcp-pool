#!/usr/bin/env python3
"""Wire this pool (and optionally the fan-out instances) into an MCP client config.

    python install.py                 # pool + 3 fan-out instances, headless pool
    python install.py --no-fanout     # pool only
    python install.py --headed --max 3
    python install.py --dry-run       # print the resulting mcpServers block

Writes to ~/.claude.json by default (override with --config). The file is
round-tripped through Python's json module - editing it with PowerShell's
ConvertFrom-Json is lossy when the object contains case-duplicate keys. A
timestamped backup is written before any change.

Existing entries with the same names are replaced; everything else in the file
is left untouched. Restart the MCP client afterwards - servers are registered
at client startup only.
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
DPI_CONFIG = os.path.join(HERE, "config", "playwright-mcp.json")


def default_profile_root():
    """Where to keep persistent browser profiles for the fan-out instances."""
    if os.name == "nt":
        return os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support")
    return os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))


def build_servers(args):
    profile_root = args.profile_root or default_profile_root()
    pkg = args.package
    env = {"BROWSERPOOL_MAX": str(args.max),
           "BROWSERPOOL_HEADLESS": "0" if args.headed else "1"}
    if args.no_tile:
        env["BROWSERPOOL_TILE"] = "0"
    if args.window_size:
        env["BROWSERPOOL_WINDOW_SIZE"] = args.window_size
    if args.tile_cols:
        env["BROWSERPOOL_TILE_COLS"] = str(args.tile_cols)
    if args.screen:
        env["BROWSERPOOL_SCREEN"] = args.screen

    servers = {
        "browserpool": {
            "type": "stdio",
            "command": args.python,
            "args": [SERVER],
            "env": env,
        }
    }
    if not args.no_fanout:
        p1 = os.path.join(profile_root, "playwright-mcp-profile")
        p2 = os.path.join(profile_root, "playwright-mcp-profile-2")
        servers["playwright"] = {
            "type": "stdio", "command": "npx",
            "args": ["-y", pkg, "--user-data-dir", p1, "--config", DPI_CONFIG],
            "env": {},
        }
        servers["playwright-2"] = {
            "type": "stdio", "command": "npx",
            "args": ["-y", pkg, "--user-data-dir", p2, "--config", DPI_CONFIG],
            "env": {},
        }
        servers["playwright-3"] = {
            "type": "stdio", "command": "npx",
            "args": ["-y", pkg, "--isolated", "--headless",
                     "--config", DPI_CONFIG],
            "env": {},
        }
    return servers


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.expanduser("~/.claude.json"),
                    help="MCP client config to edit (default: ~/.claude.json)")
    ap.add_argument("--max", type=int, default=5, help="pool size (default 5)")
    ap.add_argument("--headed", action="store_true",
                    help="pool browsers visible instead of headless")
    ap.add_argument("--window-size", default=None, metavar="WxH",
                    help="tiled window size; default fits the screen")
    ap.add_argument("--tile-cols", type=int, default=None,
                    help="tiled windows per row; default fits the screen")
    ap.add_argument("--screen", default=None, metavar="WxH",
                    help="screen size to fit the grid into; default is measured "
                         "from a real browser at startup")
    ap.add_argument("--no-tile", action="store_true",
                    help="stack headed windows instead of tiling them")
    ap.add_argument("--no-fanout", action="store_true",
                    help="install the pool only, skip playwright/-2/-3")
    ap.add_argument("--profile-root", default=None,
                    help="directory to hold the fan-out browser profiles")
    ap.add_argument("--package", default="@playwright/mcp@latest",
                    help="backend package spec")
    ap.add_argument("--python", default=sys.executable or "python",
                    help="interpreter used to run server.py")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the block instead of writing it")
    args = ap.parse_args()

    servers = build_servers(args)
    if args.dry_run:
        print(json.dumps({"mcpServers": servers}, indent=2))
        return

    path = args.config
    cfg = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except ValueError as e:
            print("ERROR: %s is not valid JSON (%s).\n"
                  "Fix or move it, then re-run. Nothing was written."
                  % (path, e), file=sys.stderr)
            sys.exit(1)
        backup = "%s.bak.%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copyfile(path, backup)
        print("backed up -> %s" % backup)

    cfg.setdefault("mcpServers", {}).update(servers)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    print("wrote %s" % path)
    for name in servers:
        print("  + mcpServers.%s" % name)
    print("\nRestart your MCP client to register the new servers.")
    if not os.path.exists(os.path.join(HERE, "state.json")):
        print("No state.json yet - pool browsers will start signed out. "
              "See the README for creating the login seed.")


if __name__ == "__main__":
    main()
