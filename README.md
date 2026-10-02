# rbk

Faster, smaller Rust builds when many git worktrees of one repo build on the
same machine. One Python 3 file, standard library only, no install step.
Plain Cargo does the building: no compiler wrapper, no daemon, no cache store.

## The problem

I run several coding agents in parallel, each in its own git worktree of the
same Rust repo, inside one small Linux container (4 cores, about 24 GB of
disk). Two things kept going wrong:

- **Disk ran out.** Every worktree compiled its own copy of the same few
  hundred dependencies, about 2 GB each.
- **Jobs waited on each other.** Builds competed for 4 cores, and each fresh
  worktree spent minutes recompiling third-party code another worktree had
  already built. In one measured afternoon, 45% of build time was lock wait
  and 33 of 56 builds recompiled the same two slow build scripts.

The obvious fix, one shared `CARGO_TARGET_DIR`, is wrong: it silently links
another worktree's code (see below). `rbk` shares only what is safe to share.

## What it does

1. **Seed.** Hardlinks already-compiled third-party dependencies from a
   "golden" layer and from sibling worktrees into a new worktree's target dir.
   Your own workspace crates are never shared.
2. **Lock and queue.** One machine-wide lock for heavy builds, with a FIFO
   queue and a priority lane. Builds with nothing to compile skip the queue.
   Tests compile under the lock and run outside it.
3. **Track and sweep.** Records which artifacts each build used and trims all
   worktrees to a disk budget, least recently used first.
4. **Time.** Prints where each build's time went (seed, lock wait, cargo,
   tests, sweep) and keeps a log, per job.

## Results

Measured in the container above, median of 3 runs, same test results and
byte-identical wasm output with and without rbk (with lighter dev debug info):

| | plain cargo | with rbk |
|---|---|---|
| `cargo check` of the app, fresh second worktree | 241.7 s | 28.2 s |
| Engine tests, fresh second or third worktree | 41.0 s | 21.1 s |
| Engine tests, disk for 3 worktrees | 6.39 GB | 2.26 GB |

A load test and fuzzer then found six bugs in rbk itself (one deadlock), each
fixed with a regression test. See [docs/LOADTEST.md](docs/LOADTEST.md).

How it was built, including what AI agents did and what I did:
[docs/HOW_THIS_WAS_BUILT.md](docs/HOW_THIS_WAS_BUILT.md).

## Usage

```bash
alias rbk=/path/to/rbk/rbk             # or put the directory on PATH

rbk golden build                       # once per session: the shared dependency layer (below)
rbk cargo test -p my-engine            # instead of `cargo test ...`
rbk seed                               # seed / top up this worktree from golden + the richest sibling
rbk sweep --budget 6G                  # trim all worktrees of this repo to 6 GB
rbk sweep --max-age-days 14 --dry-run
rbk du                                 # disk per worktree target, shared bytes once, golden layer
rbk timings                            # recent builds: seed / lock wait / cargo / tests / sweep
rbk timings --job <branch>             # one job's per-stage summary
rbk doctor
```

What `rbk cargo` does, in order:

1. **Seed.** If this worktree's target lacks dependency units that the
   golden layer or a sibling worktree has, `rbk` hardlinks them in: registry
   and git crates, their build-script outputs and their fingerprints. Cargo
   then finds those units fresh and compiles only the workspace's own crates.
   The same bytes serve every worktree, so a new one costs almost no extra
   disk for dependencies.
   - Sources, best first: the golden layer, then every sibling target, the
     one with the most units in the profile this build needs first (until
     2026-09-30 it was one sibling, "the newest `.rustc_info.json`", which
     every seeded target shares with its source: in practice the
     alphabetically last sibling).
   - A target that already has units is **topped up** with the missing ones
     (a budget sweep evicted them, or it was seeded from a thinner sibling),
     unless a cargo is building in it right now. Only the requested profile
     (plus the host profile with `--target`) is seeded.
   - Never seeded: your own workspace crates, and registry crates that depend on them
     (for example a GUI crate built over a `[patch]`ed local crate: built against one
     worktree's sources). A `deps/` file is seeded only if its hash has a
     seedable `.fingerprint/<package>-<hash>` entry, so your own test
     binaries (`deps/conformance-<hash>`) are never copied.
   - A custom `CARGO_TARGET_DIR` is fine if only one worktree uses it: rbk
     records it (`$RBK_STATE_DIR/targets.json`), so it seeds siblings, is
     seeded, swept and shown by `du`. A target dir that two worktrees build
     into gets a loud warning and is never seeded.
   - **Unit by unit, consistently (2026-10-01).** A unit is seeded only
     with the dependency builds it was compiled against (from the same
     source, or byte-identical ones here), never under a unit here that was
     compiled against another build of it, and its fingerprint goes in last,
     after every output its dep-info (`deps/<name>-<hash>.d`) names. Why:
     crates that `include!()` a build-script output (serde, serde_core,
     serde_json, ...) get a different crate hash (SVH) in every target dir,
     because the `OUT_DIR` path is part of it, while their unit hash (file
     names) is the same everywhere. Mixing golden's serde_core with a
     sibling's serde_json (compiled against the sibling's own serde_core)
     gave `E0460 found possibly newer version of crate serde_core` /
     `E0463 can't find crate for serde`: the seeded files keep their source's
     older mtimes, so cargo calls everything fresh (seen in a real checkout,
     2026-10-01). Units held back this way are compiled by cargo; the seed
     line says how many. A target already broken this way: `cargo clean -p
     <crate>` (or delete its target).
   - After the build, rbk counts the dependency units it compiled that the
     golden layer or a sibling already had, and warns at 5 or more
     (`rbk timings` marks the row `!`): usually a different
     `RUSTFLAGS` or a build-script env var, or `RBK_SEED=0`.
2. **Lock.** Heavy commands (`build`, `test`, `check`, `run`, `clippy` and
   the like) take an exclusive `flock` on `RBK_LOCK`. The default is
   `/home/user/.heavy-build.lock` in cloud containers, the same lock the
   `flock` CLI and older rbk take, so two sessions never compile at the same
   time. With `RBK_LOAD_MAX=1.5` it also waits until the machine is quiet.
   - **Nothing to compile, no queue.** When the lock is busy, rbk runs
     cargo without it, in its own process group, and kills the group at the
     first `Compiling`/`Checking` line (`-q` is taken off for this run and
     the status lines are hidden again). A fresh build, `test -- --list` on
     built tests, or a gate with nothing new finishes at once; otherwise
     rbk queues and reruns. The thrown-away probe costs cargo's start-up
     (~1-2 s for my-app), shown as `probe` in the timing line.
     `RBK_PROBE=0` turns it off.
   - **Tests run outside the lock.** `rbk cargo test` / `bench` compile with
     `--no-run` under the lock, release it, then run `cargo test` again,
     which only runs the binaries (if it finds work, e.g. an edit in
     between, the probe stops it and it reruns locked). With `-q`, or a
     terminal on stdin, the run step is not probed: it runs unlocked. Not
     split: `--no-run`, `--doc`, `RBK_SPLIT_TESTS=0`.
   - **Priority.** `RBK_PRIORITY=lead` (the lead session's release gates) goes
     before waiting jobs: a waiting lead holds a shared flock on
     `<lock>.lead`, and jobs don't take the lock while one does, for at most
     `RBK_LEAD_MAX_DEFER` seconds each (default 900), so leads cannot starve
     a job. Jobs queue first-come first-served (`<lock>.queue/`, one flocked
     ticket each; a dead job's ticket is dropped). An older rbk or `flock`
     still takes the same lock but ignores the queue.
   - The waiting line names the holder (from `/proc/locks`):
     `waiting for … (another heavy build is running: pid 123 in wt-x/my-workspace: rbk cargo check -p my-app)`.
3. **Track.** It runs Cargo with JSON messages, re-renders them so the output
   looks like plain Cargo, and records every unit the build used, whether
   built or found fresh, in `target/.rbk-used.json`.
4. **Sweep.** With `RBK_BUDGET=6G` set, after a successful build it trims all
   worktrees' targets back under the budget. It drops stale incremental
   sessions first, then the least-recently-used units. A unit (keyed by its
   16-hex hash, so an rlib named unlike its package is not a separate unit)
   shared by hardlinks is one unit, and it is evicted from every worktree
   together. Bytes the golden layer also holds are not counted, and units
   linked from it are never evicted (that would free nothing).

5. **Time.** After every heavy command it prints where the time went, then
   cargo's ten slowest crates, and appends the numbers to a log:

   ```
   rbk: timing: 52.4s = seed 0.3s + lock wait 14.9s + cargo 36.8s + sweep 0.4s; 12 built, 830 fresh
   rbk: slowest crates: my-app 21.3s, my-engine 5.1s, my-export 3.9s, ...
   ```

   - **seed**: hardlinking a sibling's dependency layer (only on an empty target).
   - **lock wait**: time spent waiting for another session's build to release
     the heavy-build lock (plus **load wait** when `RBK_LOAD_MAX` held it back).
   - **cargo**: the cargo command itself. **built / fresh**: units compiled vs
     found up to date (from cargo's JSON messages).
   - **sweep**: trimming the worktrees back under `RBK_BUDGET`.
   - The crate list comes from `cargo --timings`, which `rbk` adds to `build`,
     `test`, `check`, `run` and `bench`. It changes neither fingerprints nor
     output (checked: same binary and rlibs with and without it, and adding it
     to a finished build recompiles nothing). The full HTML report is
     `target/cargo-timings/cargo-timing.html`; `rbk` keeps the newest five
     timestamped copies. `RBK_TIMINGS=0` turns the flag and the list off.

   `rbk timings [-n 20] [--here]` prints the log (`~/.cache/rbk/timings.jsonl`,
   or `RBK_TIMINGS_LOG`): one row per build with the worktree, the times,
   units built, dependency units built (`deps`, `!` when a seed missed them)
   and the slowest crate, then the total minutes spent waiting for the lock.
   `--here` keeps only this worktree's builds. Use it to see whether a slow
   day was compiling or queueing behind other jobs.

   `rbk timings --job [<branch>]` (default: this worktree's branch) sums one
   job's rows into a paste-ready block for its handoff:

   ```
   rbk timings for job claude/x (wt-x), 2026-09-30T19:02 to 2026-09-30T21:10: 12 rbk cargo runs, 48.0 min
     compile 21.0 min | lock wait 22.1 min | tests 3.1 min | seed 0.4 min | sweep 0.2 min
     cold dependency builds: 1; seed missed: 0; failed runs: 2; slowest unit: webrtc-sys [build.rs] 185s
   ```

   Rows logged before 2026-09-30 carry no branch; they are matched by
   worktree (`--job wt-x` works too).

## Golden layer (`rbk golden`)

A per-session dependency layer that every new target is seeded from, so no
job compiles `webrtc-sys` (build.rs ~185 s) or `aws-lc-sys` (~105 s) again:

```bash
cd my-workspace && rbk golden build   # the commands in .rbk-golden.txt
rbk golden status                # age, commands, size, how many files worktrees link
rbk golden seed                  # seed / top up this worktree from it now
rbk golden rm                    # delete it (never done by any sweep; waits for seeds reading it)
rbk golden refresh               # bring it up to this worktree's Cargo.lock + list now (normally automatic)
rbk golden build -- check -p my-app ';' test --no-run -p my-engine   # ad hoc
```

- Lives at `/home/user/.rbk-golden/<repo>/<workspace>/target`
  (`RBK_GOLDEN_DIR`). `golden build` runs each command through `rbk cargo`
  with `CARGO_TARGET_DIR` pointed there (seeded from the richest sibling
  first, so building it in a session with a warm worktree is quick), then
  prunes everything but third-party units, and writes `rbk-golden.json`
  (commands, env such as `RUSTFLAGS`, the `Cargo.lock` it saw).
- `setenv K=V` lines in `.rbk-golden.txt` set K when unset. Jobs must build
  with the same value, or the dependency's build script reruns; rbk prints
  which variable differs when it seeds from golden.
- Seeds skip it while `golden build`/`refresh` writes it (an exclusive
  flock on `.building`); a seed reading it holds `.building` shared, so the
  layer is never rewritten or removed (`golden rm`) under a seed. Units of
  older `Cargo.lock` states stay (`lock_shas` in `rbk-golden.json` lists
  them), so branches still on an older lock keep seeding.
- **Auto-refresh (2026-10-01).** When a successful `rbk cargo` sees a
  `Cargo.lock` the layer does not cover (a new dependency), or a
  `.rbk-golden.txt` command the layer was never built with (the list grew or
  a line changed; a removed line is no reason, its units just stay), it
  starts `rbk golden refresh` in the background (log: `$RBK_STATE_DIR/golden-refresh.log`).
  The refresh queues for the heavy lock as a **job** (leads go first), takes
  `.building` only once it holds the lock, tops the layer up from the sibling
  targets (the job just compiled the new units), runs the same commands with
  the layer's own env, incrementally, in place, and adds the lock to
  `lock_shas`. While it holds the heavy lock it watches for a waiting lead
  build and **stops its cargo within seconds** for it (rc 75 in
  `refresh.json`; retried after the next build). One refresh at a time; a
  failed one is retried after 30 min (`RBK_GOLDEN_RETRY_S`). It never creates
  a layer that doesn't exist. `RBK_GOLDEN_AUTO=0` turns it off.
- Refuses to start with less than `RBK_GOLDEN_MIN_FREE` (3G) free unless
  `--force`.

## Why it never shares workspace crates

Cargo hashes path crates relative to the workspace root. Two worktrees of the
same repo therefore get the **same unit hash for different source code**. If
they share one `CARGO_TARGET_DIR` or `build-dir`, a worktree whose files are
older than the other worktree's last build is judged fresh, and **it silently
links the other worktree's code**. We reproduced this: worktree B printed
worktree A's value. Registry and git crates don't have this problem, because
their freshness comes from their version and their hash doesn't depend on the
path.

## Why hardlinks are safe here

`rustc` and the linker write `.rlib`, `.rmeta`, `.so` and `.a` outputs to a
temporary file and rename it into place. A rebuild therefore creates a new
inode and never writes into a shared one (verified: the inode changes on
rebuild). Build-script `out/` directories and fingerprints are *copied*,
because a build script may rewrite its files in place.

## Limits

- Linux and macOS (APFS). Windows NTFS supports hardlinks, but rbk is
  untested there.
- A custom `CARGO_TARGET_DIR` is seen once rbk has built into it (the
  registry); one shared by several worktrees is the unsafe setup described
  above: rbk warns on every build and never seeds it.
- Build-mode dependencies of the whole app (`build -p my-app`) are not in
  the default golden layer (disk); they seed from the richest sibling.
- If you pass `--message-format` yourself, `rbk` doesn't track that build.
  Tracked `cargo test` output loses libtest's colours (stdout is a pipe).
- A `<workspace>/target` that is a symlink to `CARGO_TARGET_DIR` is fine: the
  sweep and `du` resolve target dirs to their real paths and handle each dir
  once. Until 2026-09-30 the sweep locked the same `.cargo-lock` through
  two paths and waited on itself forever (`RBK_BUDGET` set).
- The sweep holds cargo's `.cargo-lock` of every profile dir it trims, but
  never waits for one while holding another, so it cannot deadlock with a
  `cargo --target` build. It skips a target whose `rbk cargo` is still
  running, which includes the test phase: cargo has released its lock by then.
  A plain `cargo test` run outside `rbk` gets no such protection.

## Tests

```bash
python3 tests/test_rbk.py   # ~35 s: regression + seed/lock/queue tests, fixtures in $TMPDIR

# load test / fuzzer: layouts x concurrency x kills against throw-away git
# fixtures under /dev/shm/rbkload (own RBK_LOCK, offline, vendored deps);
# a timeout counts as a hang. Exit 1 on any finding. Deletes its root.
python3 tests/loadtest_rbk.py --quick        # ~2 min
python3 tests/loadtest_rbk.py --seed 11      # full, ~5-8 min
python3 tests/loadtest_rbk.py --only abba,kill --keep --report r.json
```

It never touches the real repo, its worktrees, `~/.cache/rbk`, the golden
layer or `/home/user/.heavy-build.lock` (both test files point `RBK_LOCK`,
`RBK_STATE_DIR` and `RBK_GOLDEN_DIR` at throw-away paths).
Findings and the matrix from its first run: [docs/LOADTEST.md](docs/LOADTEST.md).

## Origins and defaults

rbk was written for one real project and extracted from it. A few defaults
still show that: the lock and golden layer default to `/home/user/...` when
that directory exists (the layout of the cloud containers it ran in), and the
"cold dependency build" heuristic in `rbk timings --job` names two slow build
scripts from that project. All of them can be overridden with the `RBK_*`
environment variables listed in `rbk --help`.

## License

MIT. See [LICENSE](LICENSE).
