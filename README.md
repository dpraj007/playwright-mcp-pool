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

Env changes take effect when the client restarts the server - a running pool
keeps the environment it started with.

## Tools

Three pool tools, plus every upstream `@playwright/mcp` tool with a required
`session` argument added and `[pool]` prefixed to its description.

| Tool | Purpose |
|---|---|
| `browser_new_session` | allocate a browser, returns `sN` |
| `browser_close_session` | release it, frees a slot |
| `browser_list_sessions` | max, active ids, free slots, whether a login seed is loaded |

Sessions are allocated on demand and closed on `browser_close_session`, on the
idle timeout, or when the server exits. Requesting one past `MAX` is refused
with a message rather than queued.

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
