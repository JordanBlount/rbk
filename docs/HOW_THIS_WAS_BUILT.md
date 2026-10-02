# How this was built

Most of this code was written by AI coding agents (Claude Code). I directed
the work, set the goals, and decided what was good enough to rely on. This
page says plainly who did what.

## The problem I had

I run several coding agents at once on one Rust project. Each agent works in
its own git worktree, inside a small Linux container (4 cores, about 24 GB of
disk). Two things kept breaking my day:

- The disk filled up. Every worktree built its own copy of the same
  dependencies.
- Jobs took one to two hours. They waited on each other to compile, and each
  one rebuilt third-party code that a sibling had already built.

My idea was that the jobs should share one cache of compiled dependencies and
coordinate, so they stop stepping on each other and stop repeating work.

## What I asked for

On September 29, 2026 I gave an agent this brief: make Rust builds much faster
and much smaller on limited machines, with the same accuracy. Try the tools
people recommend, measure them, pick the best, and build something better if
nothing fits.

## What the agents did

- **Survey and measurement.** An agent tested sccache, kache, mbx, mold, wild
  and others in the container, with repeats and correctness checks (same test
  results, byte-identical output).
- **A key finding.** A shared `CARGO_TARGET_DIR` across worktrees silently
  runs the wrong code. The agent reproduced it: worktree 2 compiled nothing
  and ran worktree 1's code. That ruled out the obvious fix.
- **The tool.** None of the existing tools fit several worktrees on a small
  disk, so the agent wrote `rbk`: share only third-party dependencies, by
  hardlink, and never share workspace crates.
- **The load test.** After a deadlock showed up in real use, an agent built a
  load test and fuzzer. It found six bugs, including a second deadlock. Each
  fix has a regression test that fails on the old code.
- **The second round.** The timing log showed the first version was not
  paying off: 45% of build time was lock wait, and the seed picked a poor
  source. That led to the golden layer, the queue with a priority lane, and
  top-up seeding.

## What I did

- Found the problem and framed it (shared cache plus coordination between
  parallel jobs).
- Set the bar: same accuracy, measured results, no guessing.
- Ran it daily across my own agent jobs, which is how the real failures
  surfaced (the deadlock, the "can't find crate for serde" error after
  top-ups).
- Decided what to ship, and asked for the fixes and tests.

## Timeline

- Sept 29: survey, measurements, first `rbk` (seed, lock, track, sweep).
- Sept 30: deadlock fix, load test, six bugs fixed, golden layer, lock queue.
- Oct 1: unit-by-unit consistent seeding, background golden refresh.

## Honest limits

- It was built for one project in one kind of container. Some defaults still
  reflect that (see the README).
- I did not write most of the lines. I can explain what each part does and
  why it exists, and the tests and load test are how I trust it.
