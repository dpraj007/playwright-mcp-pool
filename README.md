# playwright-mcp-pool

Parallel, signed-in browsers for [`@playwright/mcp`](https://github.com/microsoft/playwright-mcp).

One `@playwright/mcp` server is one browser with one tab and no session keys, so
two concurrent tool calls fight over the same page and an agent can only do one
browser task at a time. This is a small MCP server that sits in front of a pool
of official `@playwright/mcp` backends and hands out session handles:

```
browser_new_session()             -> "s1"        pool picks a free browser
browser_navigate(session="s1", …)                routed to that browser
browser_close_session("s1")                      frees the slot
```

Ask for five sessions and you get five real browsers running at once, all
seeded from the same `--storage-state`, so they are all signed into the same
sites. Nothing is forked or patched: backends are plain
`npx @playwright/mcp@latest --isolated`, so upstream releases and every upstream
tool keep working as-is.

## Why not just run several servers

You can, and this repo installs three named instances for exactly that
(see [Fan-out](#fan-out-instances)). But named instances are fixed and manual:
the agent has to pick one, remember which is busy, and they cannot share a
profile directory - Chromium's `SingletonLock` refuses two live browsers on one
`--user-data-dir`. The pool solves both: allocation is automatic, and
`--isolated` plus `--storage-state` seeds the same logins into unlimited fresh
contexts with no lock to contend for.

Reusing upstream backends rather than driving Playwright directly is deliberate.
The snapshot/ref engine that lets an agent click element `e42` has no public
equivalent in Playwright Python, so a from-scratch server would have to
reimplement it. This one re-exports upstream's tools untouched, with a single
required `session` argument added to each.

## Install

Requires Python 3.8+ and Node (for `npx`).

```bash
git clone https://github.com/dpraj007/playwright-mcp-pool
cd playwright-mcp-pool
python install.py            # pool + three fan-out instances
python install.py --dry-run  # just show the config block
```

`install.py` edits `~/.claude.json` (override with `--config`), backing it up
first. Restart your MCP client afterwards - MCP servers are registered at client
startup only.

To register it by hand instead:

```json
{
  "mcpServers": {
    "browserpool": {
      "type": "stdio",
      "command": "python",
      "args": ["/path/to/playwright-mcp-pool/server.py"],
      "env": { "BROWSERPOOL_MAX": "5", "BROWSERPOOL_HEADLESS": "1" }
    }
  }
}
```

## Signing the pool in

Pool backends are `--isolated`, so they start empty and stay ephemeral: a login
you perform inside a pool session is gone when the session closes. State flows
one way.

```
persistent profile  ->  export_state.py  ->  state.json  ->  pool backends
```

1. `pip install playwright && python -m playwright install chromium`
2. Sign into the sites you want, in a persistent profile - the `playwright-2`
   fan-out instance below works well as a dedicated "login vault".
3. **Close that browser.** The export needs the profile unlocked.
4. Point the exporter at it and run:

```bash
EXPORT_PROFILE=/path/to/playwright-mcp-profile-2 \
EXPORT_WARM=https://example.com,https://another.example \
python export_state.py
```

`EXPORT_WARM` is optional and only matters for sites that keep auth in
localStorage - those origins have to be visited during the export. Cookies are
captured either way. The script backs up the previous seed, restores it if the
export fails, and reports the cookie delta.

Re-run it whenever you add a login or cookies go stale. New
`browser_new_session` calls pick the seed up immediately; already-open sessions
need closing and reopening.

## Fan-out instances

`install.py` also registers three ordinary `@playwright/mcp` servers, which
remain useful alongside the pool:

| Instance | Profile | Mode | Use |
|---|---|---|---|
| `playwright` | `playwright-mcp-profile` | headed | interactive and sensitive work, CAPTCHA-gated sites |
| `playwright-2` | `playwright-mcp-profile-2` | headed | second login, and the login vault the pool is seeded from |
| `playwright-3` | isolated | headless | disposable anonymous scraping |

Each gets its own `--user-data-dir` to avoid the profile lock, and all three
inherit the DPI fix below. Tools namespace per instance
(`mcp__playwright-2__browser_navigate`), so an agent can fire calls at different
instances in one message - but never two concurrent calls at the *same*
instance.

To give an instance the same logins as another, clone the profile rather than
sharing the directory:

```bash
python clone_profile.py /path/to/playwright-mcp-profile /path/to/playwright-mcp-profile-2
```

The clone skips cache directories and lock files, which is usually a ~10x size
reduction. Close the source browser first, or the cookie database copies
inconsistently and the clone looks signed in without being signed in.

## The DPI fix

On a display with fractional or 2x OS scaling, headed Chromium opens at
`devicePixelRatio: 2` and every screenshot comes back zoomed. It is not page
zoom, so no zoom setting fixes it - the browser needs
`--force-device-scale-factor=1`, and `@playwright/mcp` has no CLI flag for
launch arguments. It has to go through `--config`:

```json
{ "browser": { "launchOptions": { "args": ["--force-device-scale-factor=1"] } } }
```

That file is `config/playwright-mcp.json`, and everything here passes it by
default.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `BROWSERPOOL_MAX` | `5` | max concurrent browsers |
| `BROWSERPOOL_HEADLESS` | `1` | `0` for visible windows |
| `BROWSERPOOL_STATE` | `./state.json` | login seed; absent means anonymous browsers |
| `BROWSERPOOL_CONFIG` | `./config/playwright-mcp.json` | `--config` passed to backends |
| `BROWSERPOOL_IDLE_TIMEOUT` | `3600` | seconds before an idle session is reaped; `0` disables |
| `BROWSERPOOL_PACKAGE` | `@playwright/mcp@latest` | backend package spec |
| `BROWSERPOOL_TILE` | `1` | tile headed windows into a grid; `0` stacks them |
| `BROWSERPOOL_TILE_COLS` | fit to screen | tiled windows per row |
| `BROWSERPOOL_WINDOW_SIZE` | fit to screen | tiled window size, `WxH` |
| `BROWSERPOOL_SCREEN` | measured | screen size to fit into, `WxH` |

Env changes take effect when the client restarts the server - a running pool
keeps the environment it started with.

## Tools

Five pool tools, plus every upstream `@playwright/mcp` tool with a required
`session` argument added and `[pool]` prefixed to its description.

| Tool | Purpose |
|---|---|
| `browser_new_session` | allocate a browser, returns `sN` |
| `browser_close_session` | release it, frees a slot |
| `browser_list_sessions` | max, active ids, grid slots, window states, free slots, login seed |
| `browser_bring_to_front` | show one session's window to the human (headed only) |
| `browser_send_to_back` | put that window back in the background, tab still live |

Sessions are allocated on demand and closed on `browser_close_session`, on the
idle timeout, or when the server exits. Requesting one past `MAX` is refused
with a message rather than queued.

## Watching what the browsers are doing

Two separate things get called "seeing the tab".

**What the agent sees** is the same whether the pool is headless or headed, and
needs no visible window: `browser_snapshot` (the accessibility tree, and the
only view whose refs `browser_click` can act on), `browser_take_screenshot` for
pixels, `browser_evaluate` for pulling values straight out of the page. Headless
is not blind - a screenshot is how you look at a headless session.

**What a human sees** needs `BROWSERPOOL_HEADLESS=0`. Headed pools tile their
windows into a grid, because the alternative is `MAX` windows stacked exactly on
top of each other: `@playwright/mcp` accepts launch arguments only through
`--config`, and every backend is handed the same one, so the pool generates a
per-session config with its own `--window-position` instead. Slots are reused as
sessions close, so windows do not wander off-screen. `BROWSERPOOL_TILE=0` opts
out.

The grid **fits itself to your screen**. It picks the column count that fills the
display best for `MAX` windows, scored by usable area but penalised for extreme
aspect ratios - otherwise five 512x1504 slivers would win on raw area and be
useless to look at. A 2560x1504 desktop with `MAX=5` becomes a 3x2 grid of
853x752 windows; 1920x1080 becomes 3x2 of 640x540. Set `BROWSERPOOL_WINDOW_SIZE`
or `BROWSERPOOL_TILE_COLS` to override either half of that.

The screen size is **measured by asking a real browser** (`screen.availWidth`)
at startup, not the OS. On a scaled Windows desktop the OS answers the process in
logical pixels - 1280x800 - while Chromium, launched with
`--force-device-scale-factor=1`, positions windows in physical pixels across
2560x1600. Trusting the OS there crams the grid into the top-left quarter of the
screen. `BROWSERPOOL_SCREEN=WxH` overrides the measurement.

### Moving one session between background and foreground

The same tab, without reloading it or losing any state:

```
browser_bring_to_front(session="s3")   # restore + raise + focus, for a human
browser_send_to_back(session="s3")     # minimise it again, tab keeps running
```

Upstream has neither tool. `bring_to_front` reaches `page.bringToFront()` and
`send_to_back` drives CDP `Browser.setWindowBounds`, both through the backend's
run-code capability, so an agent never has to invoke an RCE-equivalent tool by
hand just to move a window. Restore-then-raise is deliberate: `bringToFront()`
alone does not un-minimise. Both report a no-op when the pool is headless, and
`browser_list_sessions` reports every session's window state.

### The recipe for agents

Paste into `CLAUDE.md`, or any agent's instructions:

> Browser work runs in the **background** by default - you do not need a visible
> window to read or drive a page. Allocate with `browser_new_session`, then pass
> `session="sN"` to every `browser_*` call. Read the page with `browser_snapshot`
> (the only view whose refs `browser_click` can use) or `browser_evaluate`, and
> use `browser_take_screenshot` to look at pixels. All of this works identically
> whether the pool is headless or headed.
>
> Only when a person needs to watch or take over, call
> `browser_bring_to_front(session)` - it raises that exact tab, unchanged - and
> `browser_send_to_back(session)` when you are done, so the window stops covering
> their screen. Never close and reopen a session just to change its visibility.
> Release with `browser_close_session` when the task is finished.

## Caveats

- `storage-state` covers cookies and localStorage only. A site that keeps auth
  solely in IndexedDB or a service worker will not carry over.
- The seed is a snapshot and drifts as cookies rotate. Re-export periodically.
- Driving one account from many browsers simultaneously can trip a site's
  security checks. Parallelism across *different* sites and tasks is the safe
  shape; hammering a single account in parallel is not.
- Headed mode means up to `MAX` visible windows, and is marginally more
  detectable than headless on anti-bot sites. Sites with aggressive bot
  detection are usually better handled on the headed `playwright` instance.
- `state.json` is your cookie jar. It is gitignored here; keep it that way.

## Layout

```
server.py          the pool MCP server (stdio JSON-RPC, no dependencies)
export_state.py    rebuild the login seed from a persistent profile
clone_profile.py   copy a signed-in profile without its caches
install.py         register the pool and fan-out instances in an MCP config
config/            the DPI --config file, and a paths.json example
```

## License

MIT
