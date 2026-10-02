# rbk load test and fuzz, 2026-09-30

*Written by the agent session that ran the load test, lightly edited for this
repo. Commit hashes refer to the private repository rbk was extracted from.*

Goal: find anything in `rbk` that could stall or corrupt
builds. Priorities, in order: hangs, crashes, wrong evictions, lock problems.
It was prompted by the self-deadlock fixed in cfc83f9a: a sweep locked the same
`.cargo-lock` through a symlink and the real path.

**Result: 6 bugs found, all fixed with a regression test each.** One is a
permanent deadlock, one is an eviction that breaks `cargo test`, three are
seeding crashes, and one is a gap in the sweep's locking. On the same seed,
the load test gives 22 findings on the old rbk (6 hang reports from 3
deadlocks, 16 crashes) and 0 on the fixed one.

## What was run

Harness: `tests/loadtest_rbk.py` (seedable; `--quick`,
full, `--only`, `--rbk` to test another copy, `--report`).

- **Fixtures.** Every fixture is its own `git init` repo under
  `/dev/shm/rbkload`, with real `git worktree add` worktrees. Each is a cargo
  workspace with 1–3 local crates: `app` (bin + unit test + integration test
  that runs the bin; its `build.rs` sleeps for a configurable time, used to
  make builds slow on purpose), `applib`, `extra`.
- **Vendored "registry" crates.** Two crates, `depa` and `depb` (`depb` has a
  build script), come from a vendored directory source that replaces
  crates-io. It sits at one absolute path shared by every worktree, as
  `~/.cargo/registry` does, so cargo and rbk treat them exactly like
  registry deps: they are seeded, hardlinked and evicted. A per-worktree copy
  does not work: cargo says "the path to the source changed" and rebuilds them.
- **Markers.** Each worktree's binary prints its own marker, so reading
  another worktree's code or getting corrupted hardlinks would show up.
- **Isolation.** Offline, `-j2`, own `RBK_LOCK` and timing log inside the
  root, `RBK_SIBLINGS` unset.
- **Timeouts.** Every process has one (30–120 s). A timeout is counted as a
  HANG and its process group is killed.

**Layouts:**

- default `target/`
- `target` symlinked to `CARGO_TARGET_DIR`
- relative `CARGO_TARGET_DIR`
- symlink chain (`target` → l1 → l2 → real)
- 2 and 3 worktrees, each with its own target (the seed/hardlink path)
- 2 worktrees sharing one target through symlinks
- dangling `target` symlink
- missing target
- profile dir with no `.cargo-lock`
- weird names in `deps/`: spaces, a newline, non-UTF-8 bytes, uppercase hash,
  a dangling symlink, a `.dSYM` directory
- a worktree removed (`git worktree remove --force`) while another worktree sweeps

**Concurrency and faults:**

- 30 random rounds (full mode) of 2–6 parallel `rbk` commands across the
  worktrees: `cargo build/check/test/run`, `sweep --budget`, `sweep --dry-run`,
  `du`, `timings`, `doctor`, `seed`. Each round randomises
  `RBK_BUDGET` (none / 1K / 3M / 40M), `RBK_PROTECT_S` (60 / 0),
  `RBK_LOCK` (file / off), `RBK_SEED` (on / off), slow builds (0.3–2 s), and
  targets aged 3 days.
- A budget sweep hammering in a loop while a seeded worktree builds.
- A budget sweep against `cargo build --target <host>` in the same target,
  in both start orders.
- `SIGKILL` / `SIGINT` (leader or whole group) mid-seed, mid-build and
  mid-sweep. 3000 fake units make the seed and sweep slow enough to interrupt.
- `RBK_LOCK` contention: a holder that sleeps, a holder that gets `SIGKILL`ed,
  and non-heavy commands while the lock is held.
- Parallel tracked builds in one target (`RBK_LOCK=off`), checking
  `.rbk-used.json` and the timing log.
- Two seeds of one fresh target at once, while the seed source is being swept.

**Checked after every round:**

- no hang, and no Python traceback from rbk
- exit codes as expected
- plain `cargo build` and `cargo test` pass in every worktree
- each binary prints its own marker (so a rebuild in A leaves B's hardlinked
  rlibs and binary alone)
- `.rbk-used.json` and every timing-log line parse
- `RBK_LOCK` and every `.cargo-lock` are free
- no process left running inside the root

**Runs** (a machine at load ~11–13 on 4 cores; a release build was running):

| run | rbk | duration | processes | findings |
|---|---|---|---|---|
| full, seed 11 | old (cfc83f9a) | 387 s | 853 | **22**: 6 HANG reports (abba, from 3 deadlocks), 16 CRASH (seed-race 10, sweep-during-build 4, concurrent 1, layout 1) |
| full, seed 11 | after the first 5 fixes | 497 s | 858 | 0 |
| quick, seed 2 | after the first 5 fixes | 78 s | 239 | 1: `cargo test` failed, a test binary evicted mid-run (bug 6) |
| quick, seed 2 | final | 75 s | 239 | 0 |
| full, seed 11 | final | 285 s | 860 | 0 |
| full, seed 23 | final | 340 s | 878 | 0 |

- **Disk.** The root peaked at about 125 MB. Minimum free space seen was
  4.1 GB on `/home/user` and more than 7 GB on `/dev/shm`. The root is deleted
  after every run.
- **Regression tests.** `tests/test_rbk.py` went from 3 tests to 9 (~4 s).
  Each new test was shown to fail on the code before its fix and to pass after
  it.

## Findings

| # | severity | kind | fix | test |
|---|---|---|---|---|
| 1 | **high** | hang (deadlock) | ee38d32d | `LockOrderTest` |
| 2 | **high** | wrong eviction, `cargo test` fails | db8f1fbd | `SweepDuringTestRunTest` |
| 3 | medium | crash (seed) | 45ab0bbd | `SeedRaceTest.test_units_evicted_from_the_source_mid_seed_are_skipped` |
| 4 | medium | crash (seed) | 0394956b | `SeedRaceTest.test_losing_a_seed_race_is_not_fatal` |
| 5 | medium | lock gap, eviction during a build | 0a11048e | `MissingCargoLockTest` |
| 6 | low–medium | crash (seed, macOS release) | 5a59750a | `SeedRaceTest.test_directory_in_deps_is_copied` |

### 1. Sweep deadlocks with `cargo build --target T`

- **Symptom.** A budget sweep and a `--target` cargo build both wait forever.
  Under `rbk cargo`, the stuck build may be holding `RBK_LOCK`, which stalls
  every build on the machine.
- **How often.** 3 of 3 rounds in which cargo queued first; 0 of 3 when the
  sweep queued first.
- **Cause.**
  - Cargo (1.94; checked with strace) locks `debug/.cargo-lock` for host
    artifacts, then `T/debug/.cargo-lock`, and keeps the first while it waits
    for the second.
  - The sweep locked profile dirs in readdir order and blocked on each one
    while holding the ones before it. On tmpfs, readdir returns `T/debug`
    before `debug`.
  - Our targets often live on `/dev/shm`, and the Linux gate runs
    `--target x86_64-pc-windows-gnu`, `aarch64-apple-darwin` and `wasm32`
    checks. A sweep that queued behind a host build therefore took `T/debug`
    and then waited on `debug`. The `--target` build had taken `debug` and was
    waiting on `T/debug`.
- **Fix.** The sweep takes every lock with `LOCK_NB`. If one is busy, it
  releases all of them, waits for that one alone, and starts over.

### 2. A sweep during `cargo test` evicts test binaries before they run

- **Symptom.**
  `could not execute process .../deps/applib-<hash> (never executed)`.
- **Cause.**
  - Cargo releases `.cargo-lock` after compiling and only then runs the test
    binaries and doctests; doctests read the dependency rlibs. Verified:
    during `cargo run` and during test execution the lock is free.
  - rbk writes `.rbk-used.json` only after cargo exits.
  - Any other worktree's budget sweep in that window sees the new units as
    unprotected. With the default `RBK_PROTECT_S=60`, that takes a test phase
    longer than a minute, which is normal for workspace test runs.
- **Fix.** `rbk cargo` holds a shared flock on `<target>/.rbk-in-use` while
  cargo runs. The sweep, with every cargo lock held, probes that file without
  blocking and skips targets still in use for that pass. The probe never
  blocks, so no new wait appears.
- **Not covered.** Plain `cargo test` run outside rbk.

### 3. Seed crashes when a sweep evicts from its source

- **Symptom.** `FileNotFoundError` from copytree, os.link or copy2 (or
  shutil.Error for a unit emptied mid-copy). `rbk cargo` died before
  building. Seen in 16 old-code runs, spread across seed-race,
  sweep-during-build and concurrent.
- **Cause.** Seeding takes no lock. Any worktree's `RBK_BUDGET` sweep can
  delete a unit between the seed listing the source and copying it. A
  `git worktree remove` between `isdir` and `listdir` did the same.
- **Fix.** A vanished entry is skipped and its partial copy removed; cargo
  builds that unit itself (verified: cargo rebuilds a unit whose outputs are
  missing).

### 4. Two seeds of one fresh target crash each other

- **Symptom.** `FileExistsError` from copytree. `os.link`'s EEXIST fell
  through to `shutil.copy2`, which raises `SameFileError` on the other seed's
  hardlink or rewrites, in place, a file hardlinked from a sibling.
- **Cause.** Seeding runs before `RBK_LOCK`, so `rbk cargo check` and
  `rbk cargo test` started together in a new worktree seed it at the same
  time.
- **Fix.** An entry that appears underneath the seed counts as already
  present, and a linkable file is never copied over an existing one.

### 5. Sweep did not lock a profile dir without `.cargo-lock`

- **Cause.**
  - The seed copies fingerprints and units but not `.cargo-lock`, so a
    seeded profile dir has no lock file until cargo first runs there.
  - The sweep only locked lock files that already existed.
  - A cargo starting there during a sweep got its lock at once and built
    while the sweep evicted the units it had just judged fresh.
  - Found by code read. The test proves the lock gap deterministically: a
    FIFO as `.rbk-used.json` parks the sweep mid-eviction.
- **Fix.** The sweep creates the lock file (`O_CREAT`, as cargo does; same
  file, same flock). It also skips a profile dir whose worktree vanished,
  where it used to crash.

### 6. Seed crashed on a directory in `deps/`

- **Cause.** With `split-debuginfo = "packed"` (our release CI), cargo on
  macOS writes `deps/lib<proc-macro>-<hash>.dylib.dSYM/` bundles. The seed
  called copy2 on a directory and got `IsADirectoryError`.
- **Fix.** Directories are copied as trees.

### Checked and fine

- Symlinked target, symlink chain, shared-by-symlink target, relative
  `CARGO_TARGET_DIR`, and dangling or missing targets: no hangs, and rbk exits
  with cargo's own code.
- Hardlinked rlibs are byte-identical after the other worktree does
  `cargo clean -p` plus a rebuild, and every binary prints its own marker.
- A half-evicted unit (rlib gone, fingerprint kept) is rebuilt by cargo.
- `SIGKILL`/`SIGINT` in seed, build or sweep leaves no lock held (flocks die
  with the process; lock fds are `O_CLOEXEC`), and the next build succeeds.
- `RBK_LOCK` waits for a holder, and proceeds as soon as a `SIGKILL`ed holder
  dies.
- No corrupted `.rbk-used.json` or timing-log line in any run.

## Not fixed (needs a decision) / not covered

- **`RBK_LOAD_MAX` waits while holding `RBK_LOCK`** (up to 30 min), but the
  docstring says "wait (holding nothing)". Code or doc should change; moving
  the wait before the lock changes behaviour, so I left it.
- **Subcommand detection** takes the first non-flag argument.
  `rbk cargo -j 2 build` or `--manifest-path X build` is therefore not
  "heavy": no lock, no tracking, no seed. No current doc or skill uses that
  form.
- **`.rbk-used.json` is updated read-modify-write through one shared
  `.tmp` name.** Two tracked builds finishing at the same instant in one
  target (only possible with `RBK_LOCK=off`, or debug and release together)
  can lose an update, or crash on `os.replace`. Never hit in any run: the
  write window is microseconds and cargo's own lock serialises the builds.
- **`rbk cargo … | head`** (stdout closed early): rbk dies with
  BrokenPipeError, and cargo carries on without `RBK_LOCK`. Not a hang.
- **Not covered:**
  - unreadable files (the container runs as root, so permissions are not
    enforced)
  - macOS/APFS and Windows
  - real registry crates with proc-macros
  - plain `cargo` (not rbk) racing a sweep in the test phase
  - `CARGO_BUILD_BUILD_DIR` layouts (rbk ignores the build dir)

## Re-run

```bash
python3 tests/test_rbk.py                      # ~4 s
python3 tests/loadtest_rbk.py --quick          # ~75 s
python3 tests/loadtest_rbk.py --seed 11        # full, ~5-8 min
python3 tests/loadtest_rbk.py --seed 11 --rbk /path/to/old/rbk   # compare a version
```
