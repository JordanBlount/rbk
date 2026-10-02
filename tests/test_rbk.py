"""rbk regression tests. Run: python3 tools/rust-build-kit/tests/test_rbk.py"""
import fcntl
import json
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock

RBK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rbk")

# never write the real ~/.cache/rbk (timing log, target registry, name cache)
# or look at the real golden layer / heavy-build lock
_STATE = tempfile.mkdtemp(prefix="rbk-test-state-")
os.environ.update(RBK_STATE_DIR=_STATE, RBK_TIMINGS_LOG=os.path.join(_STATE, "timings.jsonl"),
                  RBK_GOLDEN_DIR=os.path.join(_STATE, "golden"), RBK_LOCK=os.path.join(_STATE, "heavy.lock"))
for _k in ("RBK_PRIORITY", "RBK_PROBE", "RBK_SPLIT_TESTS", "RBK_BUDGET", "RBK_SEED", "RBK_GOLDEN",
           "RBK_SIBLINGS", "CARGO_TARGET_DIR", "RBK_LEAD_MAX_DEFER"):
    os.environ.pop(_k, None)


def rbk_module():
    """rbk imported as a module (for its helpers, e.g. the sweep's lock order)."""
    loader = importlib.machinery.SourceFileLoader("rbk_mod", RBK)
    spec = importlib.util.spec_from_loader("rbk_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def try_flock(path):
    """An fd holding an exclusive flock on path, or None if someone else holds it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def make_workspace(root):
    """A one-crate cargo workspace in a git repo (cargo metadata runs offline)."""
    os.makedirs(os.path.join(root, "src"))
    with open(os.path.join(root, "Cargo.toml"), "w") as f:
        f.write(textwrap.dedent("""\
            [package]
            name = "rbk-fixture"
            version = "0.1.0"
            edition = "2021"
            """))
    with open(os.path.join(root, "src", "lib.rs"), "w") as f:
        f.write("")
    subprocess.run(["git", "init", "-q", root], check=True)


def fake_profile(target, units=20, size=64 * 1024, prof="debug"):
    """A profile dir with cargo's lock file and some stale units."""
    pdir = os.path.join(target, prof)
    for sub in ("deps", ".fingerprint"):
        os.makedirs(os.path.join(pdir, sub), exist_ok=True)
    open(os.path.join(pdir, ".cargo-lock"), "w").close()
    for i in range(units):
        name = f"libdep{i}-{i:016x}.rlib"
        p = os.path.join(pdir, "deps", name)
        with open(p, "wb") as f:
            f.write(b"\0" * size)
        os.utime(p, (1, 1))  # old, so the budget sweep may evict it
    return pdir


class SymlinkedTargetTest(unittest.TestCase):
    """2026-09-30: with <root>/target a symlink to CARGO_TARGET_DIR, the sweep
    saw the same target dir under two paths, flock'ed its .cargo-lock through
    two open files and waited on itself forever."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rbk-test-")
        self.root = os.path.join(self.tmp, "ws")
        self.real = os.path.join(self.tmp, "real-target")
        make_workspace(self.root)
        fake_profile(self.real)
        os.symlink(self.real, os.path.join(self.root, "target"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_rbk(self, *args):
        env = dict(os.environ, CARGO_TARGET_DIR=self.real, RBK_LOCK="off", RBK_PROTECT_S="0")
        env.pop("RBK_SIBLINGS", None)
        return subprocess.run([sys.executable, RBK, *args], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=30)

    def test_sweep_returns_and_trims(self):
        r = self.run_rbk("sweep", "--budget", "256K")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("across 1 target dir(s)", r.stderr)
        left = os.listdir(os.path.join(self.real, "debug", "deps"))
        self.assertLess(len(left), 20)

    def test_cargo_with_budget_returns(self):
        """The case the followfit job hit: a heavy cargo command, then the
        RBK_BUDGET sweep after it."""
        env = dict(os.environ, CARGO_TARGET_DIR=self.real, RBK_LOCK="off", RBK_PROTECT_S="0",
                   RBK_BUDGET="256K", RBK_SEED="0", CARGO_INCREMENTAL="0")
        r = subprocess.run([sys.executable, RBK, "cargo", "check", "--offline", "-q"], cwd=self.root,
                           env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_du_counts_the_dir_once(self):
        r = self.run_rbk("du")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len([l for l in r.stdout.splitlines() if "total" not in l]), 1, r.stdout)


class LockOrderTest(unittest.TestCase):
    """2026-09-30 (load test): `cargo build --target <triple>` locks
    debug/.cargo-lock, then <triple>/debug/.cargo-lock, holding the first while
    it waits for the second. The sweep locked profile dirs in readdir order
    (on tmpfs: <triple>/debug first) and waited for each while holding the
    ones before it: sweep and cargo each held what the other wanted, forever."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rbk-test-")
        self.root = os.path.join(self.tmp, "ws")
        make_workspace(self.root)
        target = os.path.join(self.root, "target")
        fake_profile(target)
        fake_profile(target, prof=os.path.join("x86_64-unknown-linux-gnu", "debug"))
        # the sweep's order, whatever this filesystem's readdir gives
        self.order = [os.path.join(p, ".cargo-lock") for p in rbk_module().profile_dirs(target)]
        self.fds = []

    def tearDown(self):
        for fd in self.fds:
            os.close(fd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sweep_never_waits_while_holding_a_lock(self):
        first, second = self.order
        # cargo is building: it holds the lock the sweep takes second
        held = try_flock(second)
        self.assertIsNotNone(held)
        self.fds.append(held)
        env = dict(os.environ, RBK_LOCK="off", RBK_PROTECT_S="0")
        env.pop("RBK_SIBLINGS", None)
        env.pop("CARGO_TARGET_DIR", None)
        sweep = subprocess.Popen([sys.executable, RBK, "sweep", "--budget", "1K"], cwd=self.root, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # give the sweep time to take `first` and block on `second`
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                fd = try_flock(first)
                if fd is None:
                    break
                os.close(fd)
                time.sleep(0.02)
            # now cargo wants `first` while still holding `second`
            got = None
            deadline = time.monotonic() + 10
            while got is None and time.monotonic() < deadline:
                got = try_flock(first)
                time.sleep(0.05)
            if got is not None:
                self.fds.append(got)
            self.assertIsNotNone(got, "the sweep held one cargo lock while waiting for another: "
                                      "deadlock with cargo --target")
        finally:
            for fd in self.fds:
                os.close(fd)
            self.fds = []
            _, err = sweep.communicate(timeout=30)
        self.assertEqual(sweep.returncode, 0, err)


class MissingCargoLockTest(unittest.TestCase):
    """2026-09-30 (load test / code read): a seeded profile dir has fingerprints
    and units but no .cargo-lock until cargo first runs in it. The sweep only
    locked lock files that already existed, so a cargo starting in that dir
    during a sweep got its lock at once and built while the sweep evicted the
    units it had just judged fresh."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rbk-test-")
        self.root = os.path.join(self.tmp, "ws")
        make_workspace(self.root)
        self.target = os.path.join(self.root, "target")
        pdir = fake_profile(self.target)
        os.unlink(os.path.join(pdir, ".cargo-lock"))  # as the seed leaves it
        self.lock = os.path.join(pdir, ".cargo-lock")
        # a FIFO as the used-units file parks the sweep mid-eviction, locks held
        self.fifo = os.path.join(self.target, ".rbk-used.json")
        os.mkfifo(self.fifo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sweep_locks_a_profile_dir_without_a_lock_file(self):
        env = dict(os.environ, RBK_LOCK="off", RBK_PROTECT_S="0")
        env.pop("RBK_SIBLINGS", None)
        env.pop("CARGO_TARGET_DIR", None)
        sweep = subprocess.Popen([sys.executable, RBK, "sweep", "--budget", "1K"], cwd=self.root, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wfd = None
        try:
            deadline = time.monotonic() + 20
            while wfd is None and time.monotonic() < deadline:
                try:  # succeeds once the sweep opened the FIFO to read it
                    wfd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
                except OSError:
                    time.sleep(0.02)
            self.assertIsNotNone(wfd, "the sweep never reached the used-units file")
            # a cargo build starting in this profile dir now
            fd = try_flock(self.lock)
            if fd is not None:
                os.close(fd)
            self.assertIsNone(fd, "cargo could lock the profile dir while the sweep was evicting from it")
        finally:
            if wfd is not None:
                os.write(wfd, b"{}")
                os.close(wfd)
            _, err = sweep.communicate(timeout=30)
        self.assertEqual(sweep.returncode, 0, err)


class SweepDuringTestRunTest(unittest.TestCase):
    """2026-09-30 (load test): cargo releases .cargo-lock once it has compiled,
    then runs the test binaries (and doctests, which need the dependency
    rlibs). rbk records what a build used only after cargo exits, so a sweep
    from another worktree while tests ran saw freshly linked but unrecorded
    units, evicted the test binaries not yet run, and `cargo test` failed with
    "could not execute process ... (never executed)" -- with the default
    RBK_PROTECT_S=60 as soon as the tests ran longer than a minute."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rbk-test-")
        self.root = os.path.join(self.tmp, "ws")
        make_workspace(self.root)
        self.gate = os.path.join(self.tmp, "gate")
        self.started = os.path.join(self.tmp, "started")
        os.makedirs(os.path.join(self.root, "tests"))
        with open(os.path.join(self.root, "tests", "a.rs"), "w") as f:
            f.write(textwrap.dedent("""\
                #[test] fn a_waits_for_the_gate() {
                    std::fs::write(env!("STARTED"), "").unwrap();
                    let gate = std::path::Path::new(env!("GATE"));
                    for _ in 0..600 { if gate.exists() { return; } std::thread::sleep(std::time::Duration::from_millis(50)); }
                    panic!("gate never opened");
                }
                """))
        with open(os.path.join(self.root, "tests", "b.rs"), "w") as f:
            f.write("#[test] fn b_runs_after_a() {}\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sweep_leaves_a_running_test_suite_alone(self):
        env = dict(os.environ, RBK_LOCK="off", RBK_SEED="0", CARGO_INCREMENTAL="0",
                   GATE=self.gate, STARTED=self.started)
        for k in ("RBK_SIBLINGS", "CARGO_TARGET_DIR", "RBK_BUDGET"):
            env.pop(k, None)
        t = subprocess.Popen([sys.executable, RBK, "cargo", "test", "--offline", "-q"], cwd=self.root, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 120
            while not os.path.exists(self.started) and t.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(self.started), "test a never started")
            # another worktree's post-build sweep, while cargo runs the tests
            s = subprocess.run([sys.executable, RBK, "sweep", "--budget", "1K"], cwd=self.root,
                               env=dict(env, RBK_PROTECT_S="0"), capture_output=True, text=True, timeout=30)
            self.assertEqual(s.returncode, 0, s.stderr)
        finally:
            open(self.gate, "w").close()
            out, err = t.communicate(timeout=60)
        self.assertEqual(t.returncode, 0, err[-2000:])


def fake_dep_target(target):
    """A target dir holding one registry-style unit the way cargo lays it out:
    fingerprint dir, build-script dir, rlib/rmeta (linked by seed) and a .d
    file (copied)."""
    pdir = os.path.join(target, "debug")
    h = "0123456789abcdef"
    for sub in ("deps", os.path.join(".fingerprint", f"dep-{h}"), os.path.join("build", f"dep-{h}")):
        os.makedirs(os.path.join(pdir, sub))
    for rel in (os.path.join(".fingerprint", f"dep-{h}", "lib-dep"),
                os.path.join("build", f"dep-{h}", f"build_script_build-{h}"),
                os.path.join("deps", f"libdep-{h}.rlib"), os.path.join("deps", f"libdep-{h}.rmeta"),
                os.path.join("deps", f"dep-{h}.d")):
        with open(os.path.join(pdir, rel), "w") as f:
            f.write(rel)
    return pdir


class SeedRaceTest(unittest.TestCase):
    """2026-09-30 (load test): seeding runs before RBK_LOCK, so two `rbk cargo`
    commands started together in a fresh worktree seed it at the same time.
    Whichever lost a race between "is it there?" and the copy crashed
    (FileExistsError from copytree; os.link's EEXIST fell through to copy2,
    which raises SameFileError on the other seeder's hardlink) -- the build
    never ran."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rbk-test-")
        self.src = os.path.join(self.tmp, "a", "target")
        self.dst = os.path.join(self.tmp, "b", "target")
        fake_dep_target(self.src)
        self.rbk = rbk_module()
        self.ws = types.SimpleNamespace(target=self.dst, local_names=set())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_losing_a_seed_race_is_not_fatal(self):
        self.assertEqual(self.rbk.cmd_seed(["--from", self.src], self.ws, quiet=True), 0)
        # the other seeder got there first: every "already present?" check was
        # made just before it created the entry
        real_exists, real_lexists = os.path.exists, os.path.lexists
        raced = lambda real: lambda p: False if str(p).startswith(self.dst + os.sep + "debug") else real(p)
        with mock.patch("os.path.exists", raced(real_exists)), mock.patch("os.path.lexists", raced(real_lexists)):
            self.assertEqual(self.rbk.cmd_seed(["--from", self.src], self.ws, quiet=True), 0)
        rlib = os.path.join(self.dst, "debug", "deps", "libdep-0123456789abcdef.rlib")
        self.assertEqual(open(rlib).read(), os.path.join("deps", "libdep-0123456789abcdef.rlib"))


    def test_units_evicted_from_the_source_mid_seed_are_skipped(self):
        """2026-09-30 (load test): a budget sweep from another worktree evicts
        units from the seed source while it is being copied; the seed crashed
        with FileNotFoundError (copytree / link / copy2 of a listed entry that
        is gone, listdir of a dir that is gone)."""
        real_listdir = os.listdir
        src_pdir = os.path.join(self.src, "debug")
        phantoms = {os.path.join(src_pdir, ".fingerprint"): ["gone-00000000000000aa"],
                    os.path.join(src_pdir, "deps"): ["libgone-00000000000000aa.rlib", "gone-00000000000000aa.d"]}

        def listdir(p="."):
            p = str(p)
            if p == os.path.join(src_pdir, "build"):
                raise FileNotFoundError(2, "No such file or directory", p)  # removed after isdir()
            return real_listdir(p) + phantoms.get(p, [])

        with mock.patch("os.listdir", listdir):
            self.assertEqual(self.rbk.cmd_seed(["--from", self.src], self.ws, quiet=True), 0)
        deps = real_listdir(os.path.join(self.dst, "debug", "deps"))
        self.assertIn("libdep-0123456789abcdef.rlib", deps)
        self.assertFalse([e for e in deps if "gone" in e], deps)
        self.assertEqual(real_listdir(os.path.join(self.dst, "debug", ".fingerprint")), ["dep-0123456789abcdef"])


    def test_directory_in_deps_is_copied(self):
        """2026-09-30 (load test): with split-debuginfo = "packed" (our release
        CI) cargo on macOS writes deps/lib<proc-macro>-<hash>.dylib.dSYM/
        directories; the seed ran copy2 on it and crashed (IsADirectoryError)."""
        dsym = os.path.join(self.src, "debug", "deps", "libdep-0123456789abcdef.dylib.dSYM")
        os.makedirs(os.path.join(dsym, "Contents"))
        with open(os.path.join(dsym, "Contents", "Info.plist"), "w") as f:
            f.write("plist")
        self.assertEqual(self.rbk.cmd_seed(["--from", self.src], self.ws, quiet=True), 0)
        got = os.path.join(self.dst, "debug", "deps", "libdep-0123456789abcdef.dylib.dSYM", "Contents", "Info.plist")
        self.assertEqual(open(got).read(), "plist")



# ------------------------------------------------------------ 2026-09-30 job-speed tests
def run_rbk(args, cwd, timeout=120, **env):
    e = dict(os.environ)
    for k, v in env.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = str(v)
    return subprocess.run([sys.executable, RBK, *args], cwd=cwd, env=e, capture_output=True, text=True,
                          timeout=timeout)


def popen_rbk(args, cwd, **env):
    e = dict(os.environ, **{k: str(v) for k, v in env.items()})
    return subprocess.Popen([sys.executable, RBK, *args], cwd=cwd, env=e, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def add_units(target, names, prof="debug", first=1):
    """Registry-style units: .fingerprint/<name>-<hash>/ and deps/lib<name>-<hash>.rlib."""
    pdir = os.path.join(target, prof)
    keys = []
    for i, n in enumerate(names):
        h = f"{i + first:016x}"
        fd = os.path.join(pdir, ".fingerprint", f"{n}-{h}")
        os.makedirs(fd, exist_ok=True)
        with open(os.path.join(fd, "lib-" + n), "w") as f:
            f.write(n)
        os.makedirs(os.path.join(pdir, "deps"), exist_ok=True)
        with open(os.path.join(pdir, "deps", f"lib{n.replace('-', '_')}-{h}.rlib"), "w") as f:
            f.write(n)
        keys.append(f"{n.replace('-', '_')}-{h}")
    return keys


def commit_all(root):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"], cwd=root,
                   check=True)


def slow_build_rs(root, ms=1000):
    """A build script that sleeps and reruns when src/lib.rs changes."""
    with open(os.path.join(root, "build.rs"), "w") as f:
        f.write('fn main() { println!("cargo:rerun-if-changed=src/lib.rs"); '
                f'std::thread::sleep(std::time::Duration::from_millis({ms})); }}\n')


def edit(root, n):
    with open(os.path.join(root, "src", "lib.rs"), "w") as f:
        f.write(f"pub fn v() -> u32 {{ {n} }}\n")


class SeedSourceTest(unittest.TestCase):
    """2026-09-30: the seed picked the sibling with the newest
    .rustc_info.json, but seeding copies that file with its mtime, so every
    target had the same one and max() fell back to the path: the
    alphabetically last sibling, even one with a small test build
    (wt-crashfix seeded from wt-turnfollow and still compiled 753 units)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rbk-test-")
        self.ws_root = os.path.join(self.tmp, "ws")
        make_workspace(self.ws_root)
        self.rich = os.path.join(self.tmp, "aa")
        self.sparse = os.path.join(self.tmp, "zz")
        for r, n in ((self.rich, 30), (self.sparse, 1)):
            make_workspace(r)
            add_units(os.path.join(r, "target"), [f"dep{i}" for i in range(n)])
            info = os.path.join(r, "target", ".rustc_info.json")
            with open(info, "w") as f:
                f.write("{}")
            os.utime(info, (1000, 1000))  # the same copied mtime everywhere

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_richest_sibling_wins(self):
        m = rbk_module()
        with mock.patch.dict(os.environ, RBK_SIBLINGS=f"{self.rich}:{self.sparse}"):
            ws = m.Workspace(self.ws_root)
            srcs = m.seed_sources(ws, ["debug"])
        self.assertEqual(srcs, [os.path.realpath(os.path.join(self.rich, "target")),
                                os.path.realpath(os.path.join(self.sparse, "target"))])


class TopUpTest(unittest.TestCase):
    """A used target missing dependency units (a budget sweep evicted them)
    is topped up from a sibling; the old seed only ever filled an empty one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rbk-test-")
        self.root = os.path.join(self.tmp, "ws")
        make_workspace(self.root)
        self.sib = os.path.join(self.tmp, "sib")
        make_workspace(self.sib)
        add_units(os.path.join(self.sib, "target"), ["depx", "depy"])
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, RBK_SEED="0", CARGO_INCREMENTAL="0")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_top_up_then_nothing(self):
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, RBK_SIBLINGS=self.sib, CARGO_INCREMENTAL="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("topping up", r.stderr)
        fp = os.listdir(os.path.join(self.root, "target", "debug", ".fingerprint"))
        self.assertIn("depx-0000000000000001", fp)
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, RBK_SIBLINGS=self.sib, CARGO_INCREMENTAL="0")
        self.assertNotIn("seed:", r.stderr)

    def test_no_top_up_while_cargo_builds_here(self):
        m = rbk_module()
        held = try_flock(os.path.join(self.root, "target", "debug", ".cargo-lock"))
        self.assertIsNotNone(held)
        try:
            with mock.patch.dict(os.environ, RBK_SIBLINGS=self.sib):
                m.maybe_seed(m.Workspace(self.root), ["check"], {})
        finally:
            os.close(held)
        fp = os.listdir(os.path.join(self.root, "target", "debug", ".fingerprint"))
        self.assertNotIn("depx-0000000000000001", fp)


class TaintedTest(unittest.TestCase):
    """Registry crates that depend on our [patch]ed path crates (iced, wgpu
    over vendor-iced-winit / vendor-wgpu-hal) are compiled against one
    worktree's sources: never seeded."""

    def test_tainted_and_local_units_stay_behind(self):
        tmp = tempfile.mkdtemp(prefix="rbk-test-")
        try:
            src, dst = os.path.join(tmp, "a"), os.path.join(tmp, "b")
            add_units(src, ["iced", "dep", "app"])
            ws = types.SimpleNamespace(target=dst, local_names={"app"}, tainted={"iced"})
            rbk_module().cmd_seed(["--from", src], ws, quiet=True)
            self.assertEqual(os.listdir(os.path.join(dst, "debug", ".fingerprint")), ["dep-0000000000000002"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CustomTargetTest(unittest.TestCase):
    """A per-worktree CARGO_TARGET_DIR (/dev/shm/<job>) is registered, seeds
    siblings and is seeded; one shared by two worktrees is refused."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rbk-test-")
        self.main = os.path.join(self.tmp, "main")
        make_workspace(self.main)
        with open(os.path.join(self.main, ".gitignore"), "w") as f:
            f.write("target\n")
        commit_all(self.main)
        self.wt = os.path.join(self.tmp, "wt2")
        subprocess.run(["git", "worktree", "add", "-q", self.wt], cwd=self.main, check=True, capture_output=True)
        self.ta, self.tb = os.path.join(self.tmp, "ta"), os.path.join(self.tmp, "tb")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_custom_dirs_seed_and_share_check(self):
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.main, CARGO_TARGET_DIR=self.ta, RBK_SEED="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        add_units(self.ta, ["depz"])
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.wt, CARGO_TARGET_DIR=self.tb)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"seed: from {os.path.realpath(self.ta)}", r.stderr)
        self.assertIn("depz-0000000000000001", os.listdir(os.path.join(self.tb, "debug", ".fingerprint")))
        du = run_rbk(["du"], self.wt, CARGO_TARGET_DIR=self.tb)
        self.assertIn(self.ta, du.stdout)
        self.assertIn(self.tb, du.stdout)
        # the same dir for both worktrees: warned, never seeded
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.wt, CARGO_TARGET_DIR=self.ta)
        self.assertIn("also the target dir of", r.stderr)
        self.assertNotIn("seed: from", r.stderr)


class MissedSeedTest(unittest.TestCase):
    """A build that compiles dependency units another target already had
    says so (and the timing log marks it)."""

    def test_warning(self):
        import contextlib, io
        tmp = tempfile.mkdtemp(prefix="rbk-test-")
        try:
            src = os.path.join(tmp, "sib", "target")
            keys = add_units(src, [f"d{i}" for i in range(6)])
            ws = types.SimpleNamespace(target=os.path.join(tmp, "me", "target"), local_names=set(), tainted=set(),
                                       golden_target=os.path.join(tmp, "nogolden", "target"),
                                       sibling_targets=lambda: [(os.path.join(tmp, "sib"), src)])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                res = rbk_module().check_missed_seed(ws, ["check"], set(keys))
            self.assertEqual(res["missed"], 6)
            self.assertIn("already had", err.getvalue())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LockTest(unittest.TestCase):
    """The heavy lock: fresh builds and test runs don't queue; compiles do;
    a lead goes first; a job's deference is bounded; cheap commands never
    touch it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rbk-test-")
        self.lock = os.path.join(self.tmp, "heavy.lock")
        self.root = os.path.join(self.tmp, "ws")
        make_workspace(self.root)
        self.env = dict(RBK_LOCK=self.lock, CARGO_INCREMENTAL="0", RBK_SEED="0")
        self.fds = []

    def tearDown(self):
        for fd in self.fds:
            os.close(fd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hold(self, path=None):
        fd = try_flock(path or self.lock)
        self.assertIsNotNone(fd)
        self.fds.append(fd)
        return fd

    def release(self, fd):
        self.fds.remove(fd)
        os.close(fd)

    def test_fresh_build_does_not_queue_and_a_dirty_one_does(self):
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, **self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        fd = self.hold()
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, timeout=30, **self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("waiting for", r.stderr)
        edit(self.root, 7)
        p = popen_rbk(["cargo", "check", "--offline", "-q"], self.root, **self.env)
        time.sleep(3)
        self.assertIsNone(p.poll(), "a build with work to do ran while the lock was held")
        self.release(fd)
        _, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("waiting for", err)

    def test_tests_run_outside_the_lock(self):
        os.makedirs(os.path.join(self.root, "tests"))
        gate, started = os.path.join(self.tmp, "gate"), os.path.join(self.tmp, "started")
        with open(os.path.join(self.root, "tests", "a.rs"), "w") as f:
            f.write(textwrap.dedent("""\
                #[test] fn waits_for_the_gate() {
                    std::fs::write(env!("STARTED"), "").unwrap();
                    for _ in 0..600 { if std::path::Path::new(env!("GATE")).exists() { return; }
                        std::thread::sleep(std::time::Duration::from_millis(50)); }
                    panic!("gate never opened");
                }
                """))
        env = dict(self.env, GATE=gate, STARTED=started)
        p = popen_rbk(["cargo", "test", "--offline", "-q"], self.root, **env)
        try:
            deadline = time.monotonic() + 120
            while not os.path.exists(started) and p.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(started), "the test never started")
            fd = try_flock(self.lock)
            self.assertIsNotNone(fd, "the heavy lock was held while the tests ran")
            os.close(fd)
        finally:
            open(gate, "w").close()
            _, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("tests ", err)
        # and with everything built, `test -- --list` doesn't queue behind a held lock
        fd = self.hold()
        r = run_rbk(["cargo", "test", "--offline", "-q", "--", "--list"], self.root, timeout=60, **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("waits_for_the_gate", r.stdout)
        self.assertNotIn("waiting for", r.stderr)

    def _two(self):
        roots = []
        for n in ("job", "lead"):
            r = os.path.join(self.tmp, n)
            make_workspace(r)
            slow_build_rs(r, 1500)
            self.assertEqual(run_rbk(["cargo", "check", "--offline", "-q"], r, **self.env).returncode, 0)
            edit(r, 1)
            roots.append(r)
        return roots

    def test_lead_goes_first(self):
        job, lead = self._two()
        fd = self.hold()
        pj = popen_rbk(["cargo", "check", "--offline", "-q"], job, **self.env)
        time.sleep(2)
        pl = popen_rbk(["cargo", "check", "--offline", "-q"], lead, RBK_PRIORITY="lead", **self.env)
        time.sleep(2)
        self.release(fd)
        done = {}
        deadline = time.monotonic() + 90
        while len(done) < 2 and time.monotonic() < deadline:
            for name, p in (("job", pj), ("lead", pl)):
                if name not in done and p.poll() is not None:
                    done[name] = time.monotonic()
            time.sleep(0.05)
        for p in (pj, pl):
            _, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err)
        self.assertLess(done["lead"], done["job"], "the waiting job took the lock before the lead")

    def test_deference_to_leads_is_bounded(self):
        job, _ = self._two()
        fd = self.hold()
        self.hold(self.lock + ".lead")  # a "lead" that waits forever and never takes it
        p = popen_rbk(["cargo", "check", "--offline", "-q"], job, RBK_LEAD_MAX_DEFER="1", **self.env)
        time.sleep(2)
        self.release(fd)
        _, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("letting it go first", err)

    def test_cheap_commands_never_wait(self):
        self.hold()
        for args in (["cargo", "metadata", "--offline", "--format-version", "1", "--no-deps"],
                     ["cargo", "tree", "--offline"], ["timings"], ["doctor"]):
            r = run_rbk(args, self.root, timeout=20, **self.env)
            self.assertEqual(r.returncode, 0, f"{args}: {r.stderr}")
            self.assertNotIn("waiting for", r.stderr)


class JobTimingsTest(unittest.TestCase):
    def test_job_summary(self):
        tmp = tempfile.mkdtemp(prefix="rbk-test-")
        try:
            log = os.path.join(tmp, "t.jsonl")
            rows = [dict(ts=f"2026-09-30T20:0{i}:00", root="/w/wt-x/app", branch="claude/x", cmd="check",
                         rc=0, total_s=100, seed_s=1, lock_s=40, load_s=0, probe_s=0, cargo_s=50, tests_s=9,
                         sweep_s=0, deps_built=900 if i == 0 else 3, missed=0,
                         top=[["webrtc-sys [build.rs]", 185.0]]) for i in range(3)]
            rows.append(dict(rows[0], branch="claude/other"))
            with open(log, "w") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
            r = run_rbk(["timings", "--job", "claude/x"], tmp, RBK_TIMINGS_LOG=log)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("3 rbk cargo runs, 5.0 min", r.stdout)
            self.assertIn("compile 2.5 min | lock wait 2.0 min | tests 0.5 min", r.stdout)
            self.assertIn("cold dependency builds: 3", r.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------- 2026-10-01 mixed-build seeds
def _crate_ws(root, offline_ok):
    """A workspace `p` on serde + serde_json (in the local registry cache)."""
    os.makedirs(os.path.join(root, "src"))
    with open(os.path.join(root, "Cargo.toml"), "w") as f:
        f.write(textwrap.dedent("""\
            [package]
            name = "p"
            version = "0.1.0"
            edition = "2021"
            [dependencies]
            serde = { version = "1", features = ["derive"] }
            serde_json = "1"
            """))
    with open(os.path.join(root, "src", "lib.rs"), "w") as f:
        f.write('#[derive(serde::Serialize)] pub struct S { pub a: u32 }\n'
                'pub fn j() -> String { serde_json::to_string(&S { a: 1 }).unwrap() }\n'
                '#[cfg(test)] mod t { #[test] fn x() { assert_eq!(super::j(), "{\\"a\\":1}"); } }\n')
    if offline_ok is not None:
        shutil.copy(offline_ok, os.path.join(root, "Cargo.lock"))
        return True
    r = subprocess.run(["cargo", "generate-lockfile", "--offline", "-q"], cwd=root, capture_output=True, text=True)
    return r.returncode == 0


def _cargo(root, *args):
    env = dict(os.environ, CARGO_INCREMENTAL="0")
    env.pop("CARGO_TARGET_DIR", None)
    return subprocess.run(["cargo", *args, "--offline"], cwd=root, env=env, capture_output=True, text=True,
                          timeout=600)


def _lib_unit(target, name):
    """The build-mode (rlib) unit entry of a registry lib in target/debug."""
    pdir = os.path.join(target, "debug")
    for e in os.listdir(os.path.join(pdir, ".fingerprint")):
        if e.startswith(name + "-") and os.path.exists(os.path.join(pdir, "deps", f"lib{name}-{e[-16:]}.rlib")):
            return e
    raise AssertionError(f"no {name} rlib unit in {target}")


def _evict(target, entry):
    """What a budget sweep does to one unit: every file with its hash."""
    h = entry[-16:]
    pdir = os.path.join(target, "debug")
    for sub in ("deps", ".fingerprint", "build"):
        for e in os.listdir(os.path.join(pdir, sub)):
            if h in e:
                p = os.path.join(pdir, sub, e)
                shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)


class MixedBuildSeedTest(unittest.TestCase):
    """2026-10-01 (the lead, main checkout, after golden + sibling top-ups):
    `cargo test -p media -p protocol` failed with "can't find
    crate for `serde`" while deps/ held a libserde-<h>.rmeta.

    Cause: a crate that include!()s a build-script output (serde,
    serde_core, serde_json, ...) gets a different crate hash (SVH) in every
    target dir -- the OUT_DIR path is part of it -- although its unit hash,
    and so its file names, are the same everywhere. The seed took each unit
    from the first source that had it, so a target could get serde_core
    from the golden layer and serde_json (compiled against a sibling's own
    serde_core) from the sibling; or a top-up re-filled a unit a sweep had
    evicted from under a unit that stays, with another worktree's build.
    The seeded files keep the source's (older) mtimes, so cargo calls
    everything fresh and rustc then finds no matching crate (E0460/E0463).
    The .rmeta the lead saw was a legitimate check-mode unit, the only other
    candidate rustc could list.

    Now a unit is only seeded with the dependencies it was compiled against
    (or byte-identical ones), never under a unit compiled against another
    build of it, and its fingerprint goes in last, after all the outputs
    its dep-info names."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rbk-mixed-")
        # three independent builds of the same lock: a sibling built before
        # the golden layer, the golden layer, a sibling built after it
        cls.old, cls.g, cls.s = (os.path.join(cls.tmp, n) for n in ("sib-old", "golden", "sib"))
        if not _crate_ws(cls.g, None):
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("serde/serde_json not in the local cargo registry cache (offline)")
        _crate_ws(cls.s, os.path.join(cls.g, "Cargo.lock"))
        _crate_ws(cls.old, os.path.join(cls.g, "Cargo.lock"))
        for r in (cls.old, cls.g, cls.s):
            res = _cargo(r, "test", "--no-run", "-q")
            if res.returncode != 0:
                raise AssertionError(res.stderr[-2000:])
            time.sleep(1.1)
        cls.rbk = rbk_module()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.m = tempfile.mkdtemp(prefix="m-", dir=self.tmp)
        _crate_ws(os.path.join(self.m, "ws"), os.path.join(self.g, "Cargo.lock"))
        self.root = os.path.join(self.m, "ws")
        self.target = os.path.join(self.root, "target")
        self.ws = types.SimpleNamespace(target=self.target, local_names={"p"}, tainted=set())

    def test_builds_differ_per_target_dir(self):
        """The premise: same unit hash, different bytes for serde_core."""
        e = _lib_unit(os.path.join(self.g, "target"), "serde_core")
        f = f"libserde_core-{e[-16:]}.rmeta"
        a = open(os.path.join(self.g, "target", "debug", "deps", f), "rb").read()
        b = open(os.path.join(self.s, "target", "debug", "deps", f), "rb").read()
        self.assertNotEqual(a, b)

    def test_unit_is_not_seeded_over_another_build_of_its_dependency(self):
        """Golden lacks serde_json; the sibling's serde_json was compiled
        against the sibling's serde_core, this target has golden's."""
        gt = os.path.join(self.g, "target")
        json_unit = _lib_unit(gt, "serde_json")
        thin = os.path.join(self.m, "golden-thin")
        shutil.copytree(gt, thin, copy_function=os.link)
        _evict(thin, json_unit)
        self.assertEqual(self.rbk.cmd_seed(["--from", thin], self.ws, quiet=True), 0)
        self.assertEqual(self.rbk.cmd_seed(["--from", os.path.join(self.s, "target")], self.ws, quiet=True), 0)
        self.assertFalse(os.path.exists(os.path.join(self.target, "debug", ".fingerprint", json_unit)),
                         "seeded the sibling's serde_json over golden's serde_core")
        r = _cargo(self.root, "test", "-q")
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])

    def test_top_up_never_refills_an_evicted_dependency_with_another_build(self):
        """This target compiled p against golden's serde_core; a sweep
        evicted serde_core (not p); the top-up must not bring a sibling's
        (an older one: newer files would make cargo rebuild the dependents)."""
        self.assertEqual(self.rbk.cmd_seed(["--from", os.path.join(self.g, "target")], self.ws, quiet=True), 0)
        r = _cargo(self.root, "test", "--no-run", "-q")
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        core = _lib_unit(self.target, "serde_core")
        _evict(self.target, core)
        self.assertEqual(self.rbk.cmd_seed(["--from", os.path.join(self.old, "target")], self.ws, quiet=True), 0)
        self.assertFalse(os.path.exists(os.path.join(self.target, "debug", ".fingerprint", core)),
                         "re-filled serde_core from another build under units compiled against golden's")
        # the developer edits p: it compiles against serde_json (built
        # against golden's serde_core) and whatever serde_core is here now
        with open(os.path.join(self.root, "src", "lib.rs"), "a") as f:
            f.write("pub fn more() {}\n")
        r = _cargo(self.root, "test", "-q")
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])

    def test_rmeta_without_rlib_is_never_installed_as_fresh(self):
        """A source unit whose .rlib is gone (evicted mid-seed, or a cargo
        killed after the pipelined .rmeta) is left to cargo."""
        gt = os.path.join(self.g, "target")
        core = _lib_unit(gt, "serde_core")
        part = os.path.join(self.m, "golden-part")
        shutil.copytree(gt, part, copy_function=os.link)
        os.unlink(os.path.join(part, "debug", "deps", f"libserde_core-{core[-16:]}.rlib"))
        self.assertEqual(self.rbk.cmd_seed(["--from", part], self.ws, quiet=True), 0)
        self.assertFalse(os.path.exists(os.path.join(self.target, "debug", ".fingerprint", core)))
        # and nothing compiled against it came along without it
        self.assertFalse(os.path.exists(os.path.join(self.target, "debug", ".fingerprint",
                                                     _lib_unit(gt, "serde_json"))))
        r = _cargo(self.root, "test", "-q")
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])

    def test_plain_seed_still_seeds_everything(self):
        """One consistent source: every dependency unit comes along."""
        self.assertEqual(self.rbk.cmd_seed(["--from", os.path.join(self.g, "target")], self.ws, quiet=True), 0)
        r = _cargo(self.root, "test", "--no-run", "-v")
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertNotIn("Compiling serde", r.stderr)
        self.assertIn("Compiling p ", r.stderr)


# ------------------------------------------------- 2026-10-01 golden auto-refresh
def _wait_for(pred, timeout=120, step=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


class GoldenAutoRefreshTest(unittest.TestCase):
    """A Cargo.lock change (a new dependency) made every job compile the new
    units itself until someone ran `rbk golden build`. Now the first
    successful build that sees an uncovered Cargo.lock refreshes the golden
    layer in the background, incrementally, as a job that yields to leads,
    never under a seed. Everything here uses a test lock and golden dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rbk-gold-")
        self.root = os.path.join(self.tmp, "ws")
        make_workspace(self.root)
        with open(os.path.join(self.root, ".rbk-golden.txt"), "w") as f:
            f.write("check\n")
        r = subprocess.run(["cargo", "generate-lockfile", "--offline", "-q"], cwd=self.root,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.golden = os.path.join(self.tmp, "golden")
        self.state = os.path.join(self.tmp, "state")
        self.lock = os.path.join(self.tmp, "heavy.lock")
        self.env = dict(RBK_GOLDEN_DIR=self.golden, RBK_STATE_DIR=self.state, RBK_LOCK=self.lock,
                        RBK_TIMINGS_LOG=os.path.join(self.state, "t.jsonl"), CARGO_INCREMENTAL="0",
                        RBK_GOLDEN_RETRY_S="0", RBK_GOLDEN_MIN_FREE="1M")  # tiny fixture layer
        r = run_rbk(["golden", "build"], self.root, **self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.gdir = os.path.dirname(rbk_module_ws_golden(self.root, self.env))
        self.fds = []

    def tearDown(self):
        for fd in self.fds:
            os.close(fd)
        # never leave a background refresh behind
        st = os.path.join(self.gdir, "refresh.json")
        try:
            pid = json.load(open(st)).get("pid")
            if pid and _wait_for(lambda: not _alive(pid), 60) is False:
                os.kill(pid, 9)
        except (OSError, ValueError):
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def meta(self):
        with open(os.path.join(self.gdir, "rbk-golden.json")) as f:
            return json.load(f)

    def add_dep(self):
        with open(os.path.join(self.root, "Cargo.toml"), "a") as f:
            f.write('\n[dependencies]\nitoa = "1"\n')
        r = subprocess.run(["cargo", "generate-lockfile", "--offline", "-q"], cwd=self.root,
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("itoa not in the local cargo registry cache (offline)")

    def refresh_done(self):
        try:
            st = json.load(open(os.path.join(self.gdir, "refresh.json")))
        except (OSError, ValueError):
            return None
        return st if st.get("rc") is not None else None

    def golden_units(self):
        fp = os.path.join(self.gdir, "target", "debug", ".fingerprint")
        return os.listdir(fp) if os.path.isdir(fp) else []

    def test_lock_change_refreshes_in_the_background(self):
        sha0 = self.meta()["lock_sha"]
        self.add_dep()
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, **self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("refreshing it in the background", r.stderr)
        self.assertTrue(_wait_for(self.refresh_done, 180), "refresh never finished")
        self.assertEqual(self.refresh_done()["rc"], 0, open(os.path.join(self.state, "golden-refresh.log")).read())
        m = self.meta()
        self.assertIn(sha0, m["lock_shas"])          # the old lock's units still serve older branches
        self.assertNotEqual(m["lock_sha"], sha0)
        self.assertIn(m["lock_sha"], m["lock_shas"])
        self.assertTrue([u for u in self.golden_units() if u.startswith("itoa-")], self.golden_units())
        self.assertFalse([u for u in self.golden_units() if u.startswith("rbk-fixture") or u.startswith("rbk_fixture")])
        # covered now: the next build starts nothing
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, **self.env)
        self.assertNotIn("refreshing", r.stderr)
        st = run_rbk(["golden", "status"], self.root, **self.env)
        self.assertIn("refreshed", st.stdout)

    def test_golden_list_change_refreshes_in_the_background(self):
        """2026-10-01: a new line in .rbk-golden.txt (same Cargo.lock)
        refreshes the layer too, and the old commands stay recorded."""
        sha0 = self.meta()["lock_sha"]
        with open(os.path.join(self.root, ".rbk-golden.txt"), "a") as f:
            f.write("build\n")
        st = run_rbk(["golden", "status"], self.root, **self.env)
        self.assertIn("golden list differs", st.stdout)
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, **self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("golden list (.rbk-golden.txt) has commands", r.stderr)
        self.assertTrue(_wait_for(self.refresh_done, 180), "refresh never finished")
        self.assertEqual(self.refresh_done()["rc"], 0, open(os.path.join(self.state, "golden-refresh.log")).read())
        m = self.meta()
        self.assertEqual(m["cmds"], ["check", "build"])
        self.assertEqual(m["lock_sha"], sha0)
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, **self.env)
        self.assertNotIn("refreshing", r.stderr)
        # a removed line is not a reason to refresh (its units just stay)
        with open(os.path.join(self.root, ".rbk-golden.txt"), "w") as f:
            f.write("build\n")
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, **self.env)
        self.assertNotIn("refreshing", r.stderr)

    def test_off_switch_and_covered_lock_start_nothing(self):
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, **self.env)
        self.assertNotIn("refreshing", r.stderr)
        self.add_dep()
        r = run_rbk(["cargo", "check", "--offline", "-q"], self.root, RBK_GOLDEN_AUTO="0", **self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("refreshing", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.gdir, "refresh.json")))

    def test_refresh_yields_to_a_waiting_lead(self):
        self.add_dep()
        slow_build_rs(self.root, ms=8000)  # the refresh's cargo takes a while
        p = popen_rbk(["golden", "refresh"], self.root, **self.env)
        try:
            # it holds the heavy lock and .building while its cargo runs
            self.assertTrue(_wait_for(lambda: _flocked(os.path.join(self.gdir, ".building")), 60))
            # a lead build starts waiting (rbk's lead marker: a shared flock on <lock>.lead)
            lfd = os.open(self.lock + ".lead", os.O_RDWR | os.O_CREAT)
            fcntl.flock(lfd, fcntl.LOCK_SH)
            self.fds.append(lfd)
            t0 = time.monotonic()
            p.wait(timeout=30)
            waited = time.monotonic() - t0
        finally:
            if p.poll() is None:
                p.kill()
            out, err = p.communicate()
        self.assertLess(waited, 10, err)
        self.assertIn("lead build is waiting", err)
        self.assertEqual(self.refresh_done()["rc"], 75)
        # and the lead gets the heavy lock and .building is free again
        self.assertIsNotNone(try_flock(self.lock))
        self.assertFalse(_flocked(os.path.join(self.gdir, ".building")))
        # the layer was neither deleted nor marked as covering the new lock
        self.assertTrue(os.path.isfile(os.path.join(self.gdir, "rbk-golden.json")))
        self.assertEqual(len(self.meta()["lock_shas"]), 1)

    def test_golden_is_never_rewritten_or_removed_under_a_seed(self):
        m = rbk_module()
        # a seed reading the layer holds .building shared ...
        seed_fd = m.golden_read_lock(os.path.join(self.gdir, "target"))
        self.assertTrue(seed_fd)
        try:
            r = run_rbk(["golden", "rm"], self.root, RBK_GOLDEN_WAIT_S="1", **self.env)
            self.assertNotEqual(r.returncode, 0)
            self.assertTrue(os.path.isdir(self.gdir))
            r = run_rbk(["golden", "build"], self.root, RBK_GOLDEN_WAIT_S="1", **self.env)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("seeds keep reading it", r.stderr)
        finally:
            os.close(seed_fd)
        # ... and while a build/refresh holds it, a seed skips the layer
        fd = m.golden_write_lock(self.gdir, wait=1)
        self.assertIsNotNone(fd)
        try:
            self.assertIs(m.golden_read_lock(os.path.join(self.gdir, "target")), False)
        finally:
            os.close(fd)
        r = run_rbk(["golden", "rm"], self.root, **self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.isdir(self.gdir))


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _flocked(path):
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def rbk_module_ws_golden(root, env):
    with mock.patch.dict(os.environ, env):
        return rbk_module().Workspace(root).golden_target
