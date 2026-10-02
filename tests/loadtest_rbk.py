#!/usr/bin/env python3
"""rbk load test / fuzzer: hangs first, then crashes, wrong evictions, lock problems.

    python3 tools/rust-build-kit/tests/loadtest_rbk.py --quick            # ~1-2 min
    python3 tools/rust-build-kit/tests/loadtest_rbk.py --seed 7           # full, ~10 min
    python3 tools/rust-build-kit/tests/loadtest_rbk.py --only abba,kill   # some scenarios

Everything happens under --root (default /dev/shm/rbkload, deleted at the end
unless --keep). Every fixture is its own `git init` repo, so rbk's sibling
discovery (`git worktree list`) never sees a real worktree; RBK_SIBLINGS is
unset, RBK_LOCK points inside the root, CARGO_NET_OFFLINE=true. Fixture crates
depend on two vendored "registry" crates (a directory source replacing
crates-io), so rbk treats them as non-local and seeds / evicts them like real
registry deps. Every subprocess has a timeout; a timeout is recorded as a HANG,
its process group is killed and the run carries on.

Checked after every round: no hang; no Python traceback from rbk; `cargo build`
+ `cargo test` succeed in every fixture worktree; each worktree's binary prints
its own marker (hardlinked deps not corrupted, no cross-worktree code); rbk's
JSON files parse; RBK_LOCK and every .cargo-lock are free; no process is left
running inside the root. Exit status 1 if anything was found.
"""
import argparse, fcntl, hashlib, json, os, random, shutil, signal, subprocess, sys, textwrap, time

HERE = os.path.dirname(os.path.abspath(__file__))
RBK = os.path.normpath(os.path.join(HERE, "..", "rbk"))
TIMEOUT = 60
HOST = None


# ------------------------------------------------------------------ plumbing
class Ctx:
    def __init__(self, a):
        self.root = os.path.abspath(a.root)
        self.rng = random.Random(a.seed)
        self.seed = a.seed
        self.quick = a.quick
        self.lock = os.path.join(self.root, "heavy.lock")
        self.tlog = os.path.join(self.root, "timings.jsonl")
        self.findings, self.counts = [], {"procs": 0, "rounds": 0, "hangs": 0}
        self.peak_bytes = 0
        self.n = 0

    def fixture_dir(self, name):
        self.n += 1
        d = os.path.join(self.root, f"{self.n:03d}-{name}")
        os.makedirs(d)
        return d

    def find(self, scen, kind, msg):
        self.findings.append({"scenario": scen, "kind": kind, "msg": msg[:1500]})
        print(f"  !! {kind} [{scen}] {msg[:400]}", flush=True)


def base_env(ctx, **kw):
    env = dict(os.environ)
    for k in ("RBK_SIBLINGS", "CARGO_TARGET_DIR", "RBK_BUDGET", "CARGO_BUILD_BUILD_DIR", "RUSTC_WRAPPER",
              "RBK_LOAD_MAX", "RBK_SEED", "RBK_PROTECT_S", "RBK_TRACK", "RBK_TIMINGS", "RBK_QUIET",
              "CARGO_BUILD_TARGET", "RBK_PRIORITY", "RBK_PROBE", "RBK_SPLIT_TESTS", "RBK_GOLDEN",
              "RBK_LEAD_MAX_DEFER"):
        env.pop(k, None)
    env.update(RBK_LOCK=ctx.lock, RBK_TIMINGS_LOG=ctx.tlog, CARGO_NET_OFFLINE="true",
               CARGO_BUILD_JOBS="2", CARGO_TERM_COLOR="never",
               RBK_STATE_DIR=os.path.join(ctx.root, "state"), RBK_GOLDEN_DIR=os.path.join(ctx.root, "golden"))
    for k, v in kw.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return env


class Proc:
    def __init__(self, ctx, label, cmd, cwd, env, timeout=TIMEOUT):
        self.ctx, self.label, self.cmd, self.timeout = ctx, label, cmd, timeout
        self.t0 = time.monotonic()
        self.p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, errors="replace", start_new_session=True)
        ctx.counts["procs"] += 1
        self.rc = self.out = self.err = None
        self.hung = False

    def wait(self):
        left = max(0.1, self.timeout - (time.monotonic() - self.t0))
        try:
            self.out, self.err = self.p.communicate(timeout=left)
        except subprocess.TimeoutExpired:
            self.hung = True
            self.kill(signal.SIGKILL)
            self.out, self.err = self.p.communicate()
        self.rc = self.p.returncode
        self.dur = time.monotonic() - self.t0
        return self

    def kill(self, sig):
        try:
            os.killpg(self.p.pid, sig)
        except ProcessLookupError:
            pass


def rbk(ctx, label, args, cwd, env, timeout=TIMEOUT):
    return Proc(ctx, label, [sys.executable, RBK, *args], cwd, env, timeout)


def run(ctx, label, cmd, cwd, env, timeout=TIMEOUT):
    return Proc(ctx, label, cmd, cwd, env, timeout).wait()


def judge(ctx, scen, p, want_rc=(0,)):
    """Record hangs, tracebacks and unexpected exit codes of a finished Proc."""
    if p.hung:
        ctx.counts["hangs"] += 1
        ctx.find(scen, "HANG", f"{p.label}: {' '.join(p.cmd[-6:])} still running after {p.timeout}s\n"
                 f"stderr tail: {(p.err or '')[-800:]}")
        return False
    if "Traceback (most recent call last)" in (p.err or ""):
        ctx.find(scen, "CRASH", f"{p.label}: {' '.join(p.cmd[-6:])} rc={p.rc}\n{p.err[-1200:]}")
        return False
    if want_rc is not None and p.rc not in want_rc:
        ctx.find(scen, "RC", f"{p.label}: {' '.join(p.cmd[-6:])} rc={p.rc} (want {want_rc})\n{(p.err or '')[-800:]}")
        return False
    return True


# ------------------------------------------------------------------ fixture
def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True, timeout=30)


def make_repo(path, nlocal=2):
    """A git repo holding a cargo workspace: `app` (bin, build.rs that sleeps
    app/slow.txt ms, integration test, marker from app/mark.txt), optional
    `applib`/`extra` path crates, and vendored registry crates depa + depb
    (depb has a build script)."""
    # one vendor dir per fixture, outside the repo, at one absolute path for
    # every worktree -- like ~/.cargo/registry/src (a per-worktree copy would
    # change the source path and make cargo rebuild the "registry" crates)
    vendor = os.path.join(os.path.dirname(path), "vendor")
    w(f"{path}/.cargo/config.toml", '[source.crates-io]\nreplace-with = "vendored"\n'
      f'[source.vendored]\ndirectory = "{vendor}"\n')
    for name, extra in (("depa", ""), ("depb", 'build = "build.rs"\n')):
        w(f"{vendor}/{name}/Cargo.toml",
          f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n{extra}')
        w(f"{vendor}/{name}/.cargo-checksum.json", json.dumps({"files": {}, "package": "0" * 64}))
    w(f"{vendor}/depa/src/lib.rs", "pub fn value() -> u32 { 40 }\n")
    w(f"{vendor}/depb/build.rs", 'fn main() { let o = std::env::var("OUT_DIR").unwrap();\n'
      '  std::fs::write(format!("{o}/gen.rs"), "pub fn two() -> u32 { 2 }").unwrap(); }\n')
    w(f"{vendor}/depb/src/lib.rs", 'include!(concat!(env!("OUT_DIR"), "/gen.rs"));\n')
    members = ["app"] + (["applib"] if nlocal >= 2 else []) + (["extra"] if nlocal >= 3 else [])
    w(f"{path}/Cargo.toml", "[workspace]\nmembers = [" + ", ".join(f'"{m}"' for m in members)
      + ']\nresolver = "2"\n')
    if nlocal >= 2:
        w(f"{path}/applib/Cargo.toml", '[package]\nname = "applib"\nversion = "0.1.0"\nedition = "2021"\n'
          '[dependencies]\ndepa = "0.1"\ndepb = "0.1"\n')
        w(f"{path}/applib/src/lib.rs", "pub fn total() -> u32 { depa::value() + depb::two() }\n")
        deps, total = 'applib = { path = "../applib" }\n', "applib::total()"
    else:
        deps, total = 'depa = "0.1"\ndepb = "0.1"\n', "depa::value() + depb::two()"
    if nlocal >= 3:
        w(f"{path}/extra/Cargo.toml", '[package]\nname = "extra"\nversion = "0.1.0"\nedition = "2021"\n'
          '[dependencies]\ndepa = "0.1"\n')
        w(f"{path}/extra/src/lib.rs", "pub fn e() -> u32 { depa::value() }\n"
          "#[cfg(test)] mod t { #[test] fn e() { assert_eq!(super::e(), 40); } }\n")
    w(f"{path}/app/Cargo.toml", '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n'
      f'[dependencies]\n{deps}')
    w(f"{path}/app/build.rs", 'fn main() { println!("cargo:rerun-if-changed=slow.txt");\n'
      '  let ms: u64 = std::fs::read_to_string("slow.txt").ok().and_then(|s| s.trim().parse().ok()).unwrap_or(0);\n'
      '  std::thread::sleep(std::time::Duration::from_millis(ms)); }\n')
    w(f"{path}/app/slow.txt", "0\n")
    w(f"{path}/app/mark.txt", "base")
    w(f"{path}/app/src/main.rs", f'fn total() -> u32 {{ {total} }}\n'
      'fn main() { println!("{} {}", include_str!("../mark.txt"), total()); }\n'
      "#[cfg(test)] mod t { #[test] fn total42() { assert_eq!(super::total(), 42); } }\n")
    w(f"{path}/app/tests/it.rs", '#[test] fn runs() { let o = std::process::Command::new(env!("CARGO_BIN_EXE_app"))'
      '.output().unwrap(); assert!(String::from_utf8_lossy(&o.stdout).trim_end().ends_with(" 42")); }\n')
    w(f"{path}/.gitignore", "target\n")
    subprocess.run(["git", "init", "-q", path], check=True, timeout=30)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "fixture")


def add_worktree(repo, path, branch):
    git(repo, "worktree", "add", "-q", "-b", branch, path)


def set_mark(wt, mark):
    w(f"{wt}/app/mark.txt", mark)


def set_slow(wt, ms):
    w(f"{wt}/app/slow.txt", f"{ms}\n")


def target_of(wt, env):
    ctd = env.get("CARGO_TARGET_DIR")
    if ctd:
        return ctd if os.path.isabs(ctd) else os.path.join(wt, ctd)
    return os.path.join(wt, "target")


def add_fake_units(target, n, prof="debug"):
    """n extra non-local units (rlib + fingerprint dir) with old mtimes: makes
    seeding and sweeping take long enough to be interrupted."""
    pd = os.path.join(target, prof)
    os.makedirs(os.path.join(pd, "deps"), exist_ok=True)
    blob = os.path.join(target, "fakeblob")
    with open(blob, "wb") as f:
        f.write(b"x" * 4096)
    for i in range(n):
        h = f"{i:016x}"
        p = os.path.join(pd, "deps", f"libfake{i}-{h}.rlib")
        shutil.copyfile(blob, p)
        fd = os.path.join(pd, ".fingerprint", f"fake{i}-{h}")
        os.makedirs(fd, exist_ok=True)
        w(os.path.join(fd, "lib-fake"), h)
        os.utime(p, (1, 1))
        os.utime(fd, (1, 1))
    os.unlink(blob)


def age_units(target, secs=86400 * 3):
    """Push every unit's mtime (and .rbk-used.json stamps) into the past."""
    old = time.time() - secs
    for dp, dn, fn in os.walk(target):
        for x in dn + fn:
            try:
                os.utime(os.path.join(dp, x), (old, old), follow_symlinks=False)
            except OSError:
                pass
    u = os.path.join(target, ".rbk-used.json")
    try:
        d = json.load(open(u))
        json.dump({k: int(old) for k in d}, open(u, "w"))
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------- invariants
def trylock(path):
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def procs_inside(root):
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if cwd == root or cwd.startswith(root + "/"):
            out.append((int(pid), cwd))
    return out


def check_locks(ctx, scen):
    if os.path.exists(ctx.lock) and not trylock(ctx.lock):
        ctx.find(scen, "LOCK", f"RBK_LOCK {ctx.lock} still held after the round")
    for dp, dn, fn in os.walk(ctx.root):
        if ".cargo-lock" in fn and not trylock(os.path.join(dp, ".cargo-lock")):
            ctx.find(scen, "LOCK", f"{dp}/.cargo-lock still held after the round")
    left = procs_inside(ctx.root)
    if left:
        ctx.find(scen, "LEAK", f"processes still running inside the root: {left[:5]}")
        for pid, _ in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def check_json(ctx, scen, targets):
    for t in targets:
        u = os.path.join(t, ".rbk-used.json")
        if os.path.exists(u):
            try:
                d = json.load(open(u))
                assert isinstance(d, dict)
            except (ValueError, AssertionError) as e:
                ctx.find(scen, "CORRUPT", f"{u} does not parse: {e}")
        for f in os.listdir(t) if os.path.isdir(t) else []:
            if f.startswith(".rbk-used.json.") and f != ".rbk-used.json":
                ctx.find(scen, "LEFTOVER", f"{t}/{f} left behind")
    if os.path.exists(ctx.tlog):
        for i, line in enumerate(open(ctx.tlog)):
            try:
                json.loads(line)
            except ValueError:
                ctx.find(scen, "CORRUPT", f"timings log line {i + 1} does not parse: {line[:200]!r}")


def verify(ctx, scen, wts, check_marks=True):
    """Plain cargo build + test in every (worktree, env, mark); run the binary."""
    ok = True
    for wt, env, mark in wts:
        for args in (["build", "--offline", "-q"], ["test", "--offline", "-q"]):
            p = run(ctx, f"verify {os.path.basename(wt)}", ["cargo", *args], wt, env, timeout=120)
            if p.hung or p.rc != 0:
                ctx.find(scen, "BROKEN", f"plain `cargo {' '.join(args)}` in {wt} failed after rbk: "
                         f"rc={p.rc} hung={p.hung}\n{(p.err or '')[-1000:]}")
                ok = False
                break
        else:
            b = os.path.join(target_of(wt, env), "debug", "app")
            p = run(ctx, "bin", [b], wt, env, timeout=20)
            want = f"{mark} 42"
            if check_marks and (p.out or "").strip() != want:
                ctx.find(scen, "WRONG-OUTPUT", f"{b} printed {(p.out or '').strip()!r}, want {want!r} "
                         f"(rc={p.rc} {(p.err or '')[-300:]})")
                ok = False
    return ok


def unit_bytes(targets):
    seen, tot = set(), 0
    for t in {os.path.realpath(x) for x in targets}:
        for dp, dn, fn in os.walk(t):
            for f in fn:
                try:
                    st = os.lstat(os.path.join(dp, f))
                except OSError:
                    continue
                if (st.st_dev, st.st_ino) not in seen:
                    seen.add((st.st_dev, st.st_ino))
                    tot += st.st_blocks * 512
    return tot


def disk_guard(ctx):
    used = unit_bytes([ctx.root])
    ctx.peak_bytes = max(ctx.peak_bytes, used)
    ctx.min_free = min(getattr(ctx, "min_free", 1 << 62), *(
        os.statvfs(p).f_bavail * os.statvfs(p).f_frsize for p in (ctx.root, "/home/user") if os.path.isdir(p)))
    for p in (ctx.root, "/home/user"):
        if os.path.isdir(p):
            st = os.statvfs(p)
            if st.f_bavail * st.f_frsize < 3 * 1024 ** 3:
                raise SystemExit(f"free disk under 3 GB on {p}: stopping")
    if used > 2 * 1024 ** 3:
        raise SystemExit(f"load-test root uses {used >> 20} MB (> 2 GB): stopping")


CTX = None


def rm_fixture(path):
    if CTX is not None:  # measure the fixture at its biggest, just before it goes
        CTX.peak_bytes = max(CTX.peak_bytes, unit_bytes([CTX.root]))
    shutil.rmtree(path, ignore_errors=True)


# ----------------------------------------------------------------- scenarios
def layouts(ctx):
    """name -> builder(dir) -> list of (worktree, env, mark). Every worktree gets
    its own marker unless its target is shared."""
    def default(d):
        make_repo(f"{d}/r", ctx.rng.randint(1, 3))
        return [(f"{d}/r", base_env(ctx), "base")], True

    def symlink_ctd(d):
        make_repo(f"{d}/r", 2)
        os.makedirs(f"{d}/ctd")
        os.symlink(f"{d}/ctd", f"{d}/r/target")
        return [(f"{d}/r", base_env(ctx, CARGO_TARGET_DIR=f"{d}/ctd"), "base")], True

    def relative_ctd(d):
        make_repo(f"{d}/r", 2)
        return [(f"{d}/r", base_env(ctx, CARGO_TARGET_DIR="reltgt"), "base")], True

    def chain(d):
        make_repo(f"{d}/r", 2)
        os.makedirs(f"{d}/real")
        os.symlink(f"{d}/real", f"{d}/l2")
        os.symlink(f"{d}/l2", f"{d}/l1")
        os.symlink(f"{d}/l1", f"{d}/r/target")
        return [(f"{d}/r", base_env(ctx), "base")], True

    def worktrees(n):
        def b(d):
            make_repo(f"{d}/main", ctx.rng.randint(1, 3))
            out = [(f"{d}/main", base_env(ctx), "main")]
            for i in range(2, n + 1):
                add_worktree(f"{d}/main", f"{d}/wt{i}", f"wt{i}")
                out.append((f"{d}/wt{i}", base_env(ctx), f"wt{i}"))
            for wt, _, m in out:
                set_mark(wt, m)
            return out, True
        return b

    def shared_symlink(d):
        # the unsafe setup the README warns about: same marker, no cross check
        make_repo(f"{d}/main", 2)
        add_worktree(f"{d}/main", f"{d}/wt2", "wt2")
        os.makedirs(f"{d}/shared")
        for wt in ("main", "wt2"):
            os.symlink(f"{d}/shared", f"{d}/{wt}/target")
        return [(f"{d}/main", base_env(ctx), "base"), (f"{d}/wt2", base_env(ctx), "base")], False

    return {"default": default, "symlink_ctd": symlink_ctd, "relative_ctd": relative_ctd, "chain": chain,
            "wt2": worktrees(2), "wt3": worktrees(3), "shared_symlink": shared_symlink}


def build_all(ctx, scen, wts, env_extra=None):
    for wt, env, _ in wts:
        e = dict(env, **(env_extra or {}))
        judge(ctx, scen, rbk(ctx, "build", ["cargo", "build", "--offline"], wt, e).wait())


def sc_layouts(ctx):
    """Every layout: sequential rbk build/test/sweep with a tiny budget, then
    verify; plus the odd target states (dangling link, no .cargo-lock, weird
    names in deps/)."""
    for name, b in layouts(ctx).items():
        scen = f"layout:{name}"
        d = ctx.fixture_dir(name)
        wts, marks = b(d)
        print(f"- {scen}", flush=True)
        build_all(ctx, scen, wts)
        for wt, env, _ in wts:
            e = dict(env, RBK_BUDGET="1K", RBK_PROTECT_S="0")
            judge(ctx, scen, rbk(ctx, "test", ["cargo", "test", "--offline", "-q"], wt, e).wait())
            judge(ctx, scen, rbk(ctx, "sweep", ["sweep", "--budget", "1K"], wt, e).wait())
            judge(ctx, scen, rbk(ctx, "du", ["du"], wt, e).wait())
        verify(ctx, scen, wts, marks)
        check_json(ctx, scen, [target_of(wt, env) for wt, env, _ in wts])
        check_locks(ctx, scen)
        rm_fixture(d)
        disk_guard(ctx)

    # dangling target symlink / missing target
    scen = "layout:dangling"
    d = ctx.fixture_dir("dangling")
    make_repo(f"{d}/r", 2)
    os.symlink(f"{d}/nowhere", f"{d}/r/target")
    env = base_env(ctx, RBK_BUDGET="1K")
    print(f"- {scen}", flush=True)
    plain = run(ctx, "plain", ["cargo", "build", "--offline", "-q"], f"{d}/r", env)
    for args in (["cargo", "build", "--offline"], ["sweep", "--budget", "1K"], ["du"], ["seed"], ["doctor"]):
        want = (plain.rc,) if args[0] == "cargo" else (0,)
        judge(ctx, scen, rbk(ctx, args[0], args, f"{d}/r", env).wait(), want)
    rm_fixture(d)

    # a profile dir without .cargo-lock, and weird names in deps/
    scen = "layout:weird"
    d = ctx.fixture_dir("weird")
    make_repo(f"{d}/main", 2)
    add_worktree(f"{d}/main", f"{d}/wt2", "wt2")
    set_mark(f"{d}/wt2", "wt2")
    env = base_env(ctx)
    print(f"- {scen}", flush=True)
    judge(ctx, scen, rbk(ctx, "build", ["cargo", "build", "--offline"], f"{d}/main", env).wait())
    deps = f"{d}/main/target/debug/deps"
    os.unlink(f"{d}/main/target/debug/.cargo-lock")
    for n in ("lib-0123456789abcdef.rlib", "libspace y-0123456789abcdef.rlib", "libnl\nx-0123456789abcdef.rlib",
              "libUPPER-0123456789ABCDEF.rlib", "libodd-0123456789abcdef", "-0123456789abcdef",
              "libcafé-0123456789abcdef.rlib"):
        try:
            w(os.path.join(deps, n), "x")
        except OSError:
            pass
    with open(os.fsencode(deps) + b"/libbad\xff-0123456789abcdef.rlib", "wb") as f:
        f.write(b"x")
    os.symlink(f"{d}/nowhere", os.path.join(deps, "libdangle-0123456789abcdef.rlib"))
    os.makedirs(os.path.join(deps, "libdir-0123456789abcdef.dSYM", "Contents"))
    w(os.path.join(deps, "libdir-0123456789abcdef.dSYM", "Contents", "Info.plist"), "x")
    judge(ctx, scen, rbk(ctx, "build2", ["cargo", "build", "--offline"], f"{d}/wt2", env).wait())
    judge(ctx, scen, rbk(ctx, "sweep", ["sweep", "--budget", "1K"], f"{d}/main",
                         dict(env, RBK_PROTECT_S="0")).wait())
    judge(ctx, scen, rbk(ctx, "du", ["du"], f"{d}/main", env).wait())
    verify(ctx, scen, [(f"{d}/main", env, "base"), (f"{d}/wt2", env, "wt2")])
    check_locks(ctx, scen)
    rm_fixture(d)


def sc_hardlinks(ctx):
    """Seed wt2 from main, then rebuild a dependency and the app in main: wt2's
    hardlinked rlibs keep their bytes and wt2's binary its own marker."""
    scen = "hardlinks"
    print(f"- {scen}", flush=True)
    d = ctx.fixture_dir("hardlinks")
    wts, _ = layouts(ctx)["wt2"](d)
    (m, env, _), (w2, _, _) = wts
    judge(ctx, scen, rbk(ctx, "build", ["cargo", "build", "--offline"], m, env).wait())
    p = rbk(ctx, "build-seeded", ["cargo", "build", "--offline"], w2, env).wait()
    judge(ctx, scen, p)
    if "seed: from" not in (p.err or ""):
        ctx.find(scen, "NO-SEED", f"wt2 was not seeded: {(p.err or '')[-400:]}")
    if "Compiling depa" in (p.err or "") or "Compiling depb" in (p.err or ""):
        ctx.find(scen, "NO-SEED", "wt2 recompiled a seeded registry crate")
    linked = [f for f in os.listdir(f"{w2}/target/debug/deps") if f.endswith(".rlib")
              and os.stat(f"{w2}/target/debug/deps/{f}").st_nlink > 1]
    if not linked:
        ctx.find(scen, "NO-SEED", "no hardlinked rlib in wt2 after seeding")
    before = {f: hashlib.sha256(open(f"{w2}/target/debug/deps/{f}", "rb").read()).hexdigest() for f in linked}
    run(ctx, "clean-depa", ["cargo", "clean", "-p", "depa", "-p", "depb", "--offline", "-q"], m, env)
    set_mark(m, "main-v2")
    judge(ctx, scen, rbk(ctx, "rebuild", ["cargo", "build", "--offline"], m, env).wait())
    for f, h in before.items():
        if hashlib.sha256(open(f"{w2}/target/debug/deps/{f}", "rb").read()).hexdigest() != h:
            ctx.find(scen, "CORRUPT", f"wt2's hardlinked {f} changed when main rebuilt")
    verify(ctx, scen, [(m, env, "main-v2"), (w2, env, "wt2")])
    check_locks(ctx, scen)
    rm_fixture(d)


def sc_concurrent(ctx, rounds):
    """Random mixes of parallel rbk commands across the worktrees of one repo."""
    actions = ["build", "check", "test", "run", "sweep", "sweep", "sweep-dry", "du", "timings", "doctor", "seed",
               "test-list", "check-q"]
    for rnd in range(rounds):
        ctx.counts["rounds"] += 1
        lay = ctx.rng.choice(["wt2", "wt3", "wt3", "shared_symlink", "symlink_ctd"])
        scen = f"concurrent#{rnd}:{lay}"
        d = ctx.fixture_dir(f"conc{rnd}")
        wts, marks = layouts(ctx)[lay](d)
        common = {"RBK_BUDGET": ctx.rng.choice([None, "1K", "3M", "40M"]),
                  "RBK_PROTECT_S": ctx.rng.choice([None, "0"]),
                  "RBK_LOCK": ctx.rng.choice([ctx.lock, ctx.lock, "off"]),
                  "RBK_SEED": ctx.rng.choice([None, None, "0"])}
        # prime: the first worktree built; maybe old; maybe slow next builds
        judge(ctx, scen, rbk(ctx, "prime", ["cargo", "build", "--offline"], wts[0][0], wts[0][1]).wait())
        if ctx.rng.random() < 0.5:
            age_units(target_of(wts[0][0], wts[0][1]))
        n = ctx.rng.randint(2, 6)
        plan = []
        for i in range(n):
            wt, env, _ = ctx.rng.choice(wts)
            a = ctx.rng.choice(actions)
            if ctx.rng.random() < 0.3:
                set_slow(wt, ctx.rng.choice([300, 1000, 2000]))
            plan.append((wt, env, a))
        desc = ", ".join(f"{a}@{os.path.basename(wt)}" for wt, _, a in plan)
        print(f"- {scen} env={ {k: v for k, v in common.items() if v} } [{desc}]", flush=True)
        procs = []
        for wt, env, a in plan:
            e = dict(env)
            for k, v in common.items():
                if v is None:
                    e.pop(k, None)
                else:
                    e[k] = v
            # per process: lead or job, probing or not
            for k, choices in (("RBK_PRIORITY", [None, None, "lead"]), ("RBK_PROBE", [None, None, "0"])):
                v = ctx.rng.choice(choices)
                if v is not None:
                    e[k] = v
            args = {"build": ["cargo", "build", "--offline"], "check": ["cargo", "check", "--offline"],
                    "check-q": ["cargo", "check", "--offline", "-q"],
                    "test-list": ["cargo", "test", "--offline", "-q", "--", "--list"],
                    "test": ["cargo", "test", "--offline", "-q"], "run": ["cargo", "run", "--offline", "-q"],
                    "sweep": ["sweep", "--budget", ctx.rng.choice(["1K", "2M", "20M"])],
                    "sweep-dry": ["sweep", "--max-age-days", "0", "--dry-run"], "du": ["du"],
                    "timings": ["timings", "-n", "5"], "doctor": ["doctor"], "seed": ["seed"]}[a]
            procs.append((a, rbk(ctx, a, args, wt, e)))
            time.sleep(ctx.rng.uniform(0, 0.3))
        for a, p in procs:
            p.wait()
            # a check/build may fail only if it could not be tracked -- it never should
            judge(ctx, scen, p)
            if a == "run" and p.rc == 0 and not p.hung and not (p.out or "").strip().endswith(" 42"):
                ctx.find(scen, "WRONG-OUTPUT", f"rbk cargo run printed {(p.out or '')!r}")
        for wt, _, _ in wts:
            set_slow(wt, 0)
        check_locks(ctx, scen)
        verify(ctx, scen, wts, marks)
        check_json(ctx, scen, [target_of(wt, env) for wt, env, _ in wts])
        check_locks(ctx, scen)
        rm_fixture(d)
        disk_guard(ctx)


def sc_sweep_during_build(ctx, rounds):
    """A budget sweep (protect 0) hammering while a seeded worktree builds; the
    build must succeed and the tree stay buildable."""
    for rnd in range(rounds):
        scen = f"sweep-during-build#{rnd}"
        print(f"- {scen}", flush=True)
        d = ctx.fixture_dir(f"swb{rnd}")
        wts, _ = layouts(ctx)["wt2"](d)
        (m, env, _), (w2, _, _) = wts
        judge(ctx, scen, rbk(ctx, "prime", ["cargo", "build", "--offline"], m, env).wait())
        age_units(f"{m}/target")
        set_slow(w2, ctx.rng.choice([500, 1500]))
        b = rbk(ctx, "build", ["cargo", "build", "--offline"], w2, env)
        sweeps = []
        t_end = time.monotonic() + 3
        while time.monotonic() < t_end and b.p.poll() is None:
            sweeps.append(rbk(ctx, "sweep", ["sweep", "--budget", "1K"], m,
                              dict(env, RBK_PROTECT_S="0")).wait())
        for s in sweeps:
            judge(ctx, scen, s)
        b.wait()
        judge(ctx, scen, b)
        set_slow(w2, 0)
        verify(ctx, scen, [(m, env, "main"), (w2, env, "wt2")])
        check_locks(ctx, scen)
        rm_fixture(d)


def sc_abba(ctx, rounds):
    """cargo --target <host> takes debug/.cargo-lock then <triple>/debug/.cargo-lock;
    a budget sweep that waits for one of them while holding the other must not
    deadlock with it."""
    for rnd in range(rounds):
        scen = f"abba#{rnd}"
        print(f"- {scen}", flush=True)
        d = ctx.fixture_dir(f"abba{rnd}")
        make_repo(f"{d}/r", 2)
        r = f"{d}/r"
        env = base_env(ctx, RBK_LOCK="off")
        run(ctx, "prime", ["cargo", "build", "--offline", "-q", "--target", HOST], r, env)
        run(ctx, "prime", ["cargo", "build", "--offline", "-q"], r, env)
        age_units(f"{r}/target")
        set_slow(r, 1500)
        a = Proc(ctx, "cargo-host", ["cargo", "build", "--offline", "-q"], r, env)
        time.sleep(0.4)
        # both queue on debug/.cargo-lock behind cargo-host; flock wakes waiters
        # roughly first-come, so the start order decides who gets it first
        sweep_first = rnd % 2 == 0
        mk_s = lambda: rbk(ctx, "sweep", ["sweep", "--budget", "1K"], r, dict(env, RBK_PROTECT_S="0"), timeout=30)
        mk_c = lambda: Proc(ctx, "cargo-triple", ["cargo", "build", "--offline", "-q", "--target", HOST], r, env,
                            timeout=30)
        first = mk_s() if sweep_first else mk_c()
        time.sleep(0.3)
        second = mk_c() if sweep_first else mk_s()
        s, c = (first, second) if sweep_first else (second, first)
        for p in (a, s, c):
            p.wait()
            judge(ctx, scen, p)
        set_slow(r, 0)
        verify(ctx, scen, [(r, base_env(ctx), "base")])
        check_locks(ctx, scen)
        rm_fixture(d)


def sc_kill(ctx, rounds):
    """SIGKILL / SIGINT rbk mid-seed, mid-build and mid-sweep; the next build
    must succeed and nothing may stay locked."""
    for rnd in range(rounds):
        sig = ctx.rng.choice([signal.SIGKILL, signal.SIGINT])
        what = ctx.rng.choice(["seed", "sweep", "build"])
        delay = ctx.rng.uniform(0.05, 0.8)
        scen = f"kill#{rnd}:{what}:{sig.name}@{delay:.2f}s"
        print(f"- {scen}", flush=True)
        d = ctx.fixture_dir(f"kill{rnd}")
        wts, _ = layouts(ctx)["wt2"](d)
        (m, env, _), (w2, _, _) = wts
        judge(ctx, scen, rbk(ctx, "prime", ["cargo", "build", "--offline"], m, env).wait())
        add_fake_units(f"{m}/target", 1500 if ctx.quick else 3000)
        if what == "sweep":
            judge(ctx, scen, rbk(ctx, "seed", ["seed"], w2, env).wait())
            p = rbk(ctx, "sweep", ["sweep", "--budget", "1K"], m, dict(env, RBK_PROTECT_S="0"))
        elif what == "seed":
            p = rbk(ctx, "build", ["cargo", "build", "--offline"], w2, env)
        else:
            set_slow(w2, 1500)
            p = rbk(ctx, "build", ["cargo", "build", "--offline"], w2, dict(env, RBK_BUDGET="1K"))
        time.sleep(delay)
        # SIGINT goes to the leader only (a terminal would hit the group; both covered by the next round)
        if sig == signal.SIGINT and ctx.rng.random() < 0.5:
            p.p.send_signal(sig)
        else:
            p.kill(sig)
        p.wait()
        if p.hung:
            judge(ctx, scen, p)
        time.sleep(0.3)
        set_slow(w2, 0)
        for wt in (w2, m):
            q = rbk(ctx, "after", ["cargo", "build", "--offline"], wt, env).wait()
            judge(ctx, scen, q)
        # the killed run may have left an orphaned cargo; give it a moment then check
        deadline = time.monotonic() + 20
        while procs_inside(ctx.root) and time.monotonic() < deadline:
            time.sleep(0.2)
        verify(ctx, scen, [(m, env, "main"), (w2, env, "wt2")])
        check_json(ctx, scen, [f"{m}/target", f"{w2}/target"])
        check_locks(ctx, scen)
        rm_fixture(d)
        disk_guard(ctx)


def sc_lock(ctx):
    """RBK_LOCK contention: a sleeping holder delays a build; a SIGKILLed holder
    releases the lock."""
    scen = "rbk-lock"
    print(f"- {scen}", flush=True)
    d = ctx.fixture_dir("lock")
    make_repo(f"{d}/r", 1)
    env = base_env(ctx)
    hold = "import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT); " \
           "fcntl.flock(fd,fcntl.LOCK_EX); print('held',flush=True); time.sleep(float(sys.argv[2]))"
    h = Proc(ctx, "holder", [sys.executable, "-c", hold, ctx.lock, "2"], d, env)
    h.p.stdout.readline()
    t0 = time.monotonic()
    p = rbk(ctx, "build", ["cargo", "build", "--offline"], f"{d}/r", env).wait()
    judge(ctx, scen, p)
    if time.monotonic() - t0 < 1.5:
        ctx.find(scen, "LOCK", "build did not wait for a held RBK_LOCK")
    h.wait()
    h = Proc(ctx, "holder", [sys.executable, "-c", hold, ctx.lock, "600"], d, env)
    h.p.stdout.readline()
    p = rbk(ctx, "build", ["cargo", "build", "--offline"], f"{d}/r", env, timeout=30)
    time.sleep(1)
    h.kill(signal.SIGKILL)
    h.wait()
    judge(ctx, scen, p.wait())
    # a held lock must not affect non-heavy commands
    h = Proc(ctx, "holder", [sys.executable, "-c", hold, ctx.lock, "600"], d, env)
    h.p.stdout.readline()
    for args in (["cargo", "metadata", "--offline", "--format-version", "1", "--no-deps"], ["du"],
                 ["timings"], ["doctor"]):
        judge(ctx, scen, rbk(ctx, args[0], args, f"{d}/r", env, timeout=20).wait())
    h.kill(signal.SIGKILL)
    h.wait()
    check_locks(ctx, scen)
    rm_fixture(d)


def sc_json(ctx, rounds):
    """Parallel tracked builds in ONE target (RBK_LOCK=off): the used-units file
    and the timing log must stay valid JSON, and rbk must not crash."""
    scen = "json"
    print(f"- {scen} x{rounds}", flush=True)
    d = ctx.fixture_dir("json")
    make_repo(f"{d}/r", 3)
    env = base_env(ctx, RBK_LOCK="off")
    judge(ctx, scen, rbk(ctx, "prime", ["cargo", "build", "--offline"], f"{d}/r", env).wait())
    for _ in range(rounds):
        ps = [rbk(ctx, "check", ["cargo", ctx.rng.choice(["check", "build"]), "--offline", "-q"], f"{d}/r", env)
              for _ in range(ctx.rng.randint(3, 5))]
        for p in ps:
            judge(ctx, scen, p.wait())
        check_json(ctx, scen, [f"{d}/r/target"])
    check_locks(ctx, scen)
    rm_fixture(d)


def sc_worktree_removed(ctx, rounds):
    """`git worktree remove` of one worktree while another sweeps everything."""
    for rnd in range(rounds):
        scen = f"worktree-removed#{rnd}"
        print(f"- {scen}", flush=True)
        d = ctx.fixture_dir(f"wtrm{rnd}")
        wts, _ = layouts(ctx)["wt3"](d)
        for wt, env, _ in wts:
            judge(ctx, scen, rbk(ctx, "prime", ["cargo", "build", "--offline"], wt, env).wait())
        add_fake_units(f"{d}/wt3/target", 1500)
        add_fake_units(f"{d}/wt3/target", 1, prof="release")
        env = wts[0][1]
        s = rbk(ctx, "sweep", ["sweep", "--budget", "1K"], wts[0][0], dict(env, RBK_PROTECT_S="0"))
        time.sleep(ctx.rng.uniform(0.05, 0.6))
        subprocess.run(["git", "worktree", "remove", "--force", f"{d}/wt3"], cwd=wts[0][0],
                       capture_output=True, timeout=60)
        shutil.rmtree(f"{d}/wt3", ignore_errors=True)
        judge(ctx, scen, s.wait())
        verify(ctx, scen, wts[:2])
        check_locks(ctx, scen)
        rm_fixture(d)


def sc_seed_race(ctx, rounds):
    """Two rbk builds seeding the same empty target at once (RBK_LOCK=off), and
    a seed racing a sweep of its source."""
    for rnd in range(rounds):
        scen = f"seed-race#{rnd}"
        print(f"- {scen}", flush=True)
        d = ctx.fixture_dir(f"seedrace{rnd}")
        wts, _ = layouts(ctx)["wt2"](d)
        (m, env, _), (w2, _, _) = wts
        judge(ctx, scen, rbk(ctx, "prime", ["cargo", "build", "--offline"], m, env).wait())
        add_fake_units(f"{m}/target", 800)
        e = dict(env, RBK_LOCK="off")
        ps = [rbk(ctx, "build", ["cargo", c, "--offline", "-q"], w2, e) for c in ("build", "check")]
        ps.append(rbk(ctx, "sweep", ["sweep", "--budget", "1K"], m, dict(e, RBK_PROTECT_S="0")))
        for p in ps:
            judge(ctx, scen, p.wait())
        verify(ctx, scen, [(m, env, "main"), (w2, env, "wt2")])
        check_locks(ctx, scen)
        rm_fixture(d)


def sc_golden(ctx):
    """`rbk golden build` in main, then a fresh worktree is seeded from the
    golden layer (no registry crate recompiled), a tiny-budget sweep never
    touches the golden layer, and the worktree still builds its own code."""
    scen = "golden"
    print(f"- {scen}", flush=True)
    d = ctx.fixture_dir("golden")
    wts, _ = layouts(ctx)["wt3"](d)
    (m, env, _), (w2, _, _), (w3, _, _) = wts
    env = dict(env, RBK_GOLDEN_DIR=f"{d}/golden-root")
    g = judge(ctx, scen, rbk(ctx, "golden-build", ["golden", "build", "--force", "--", "build", "--offline"],
                             m, env).wait())
    gt = None
    for dp, dn, fn in os.walk(f"{d}/golden-root"):
        if os.path.basename(dp) == "target":
            gt = dp
            break
    if not g or not gt:
        ctx.find(scen, "GOLDEN", f"no golden target under {d}/golden-root")
        rm_fixture(d)
        return
    names = os.listdir(os.path.join(gt, "debug", ".fingerprint"))
    if any(n.startswith(("app-", "applib-", "extra-")) for n in names):
        ctx.find(scen, "GOLDEN", f"local crates left in the golden layer: {names}")
    if not any(n.startswith("depa-") for n in names):
        ctx.find(scen, "GOLDEN", f"registry crate missing from the golden layer: {names}")
    p = rbk(ctx, "build-seeded", ["cargo", "build", "--offline"], w2, env).wait()
    judge(ctx, scen, p)
    if f"seed: from {gt}" not in (p.err or ""):
        ctx.find(scen, "NO-SEED", f"wt2 was not seeded from the golden layer: {(p.err or '')[-400:]}")
    if "Compiling depa" in (p.err or "") or "Compiling depb" in (p.err or ""):
        ctx.find(scen, "NO-SEED", "wt2 recompiled a golden registry crate")
    before = sorted(os.path.join(dp, f) for dp, _, fn in os.walk(gt) for f in fn)
    for wt in (m, w2, w3):
        judge(ctx, scen, rbk(ctx, "sweep", ["sweep", "--budget", "1K"], wt, dict(env, RBK_PROTECT_S="0")).wait())
    after = sorted(os.path.join(dp, f) for dp, _, fn in os.walk(gt) for f in fn)
    if before != after:
        ctx.find(scen, "GOLDEN", f"a sweep changed the golden layer: {len(before)} -> {len(after)} files")
    q = rbk(ctx, "du", ["du"], m, env).wait()
    judge(ctx, scen, q)
    if "golden layer" not in (q.out or ""):
        ctx.find(scen, "GOLDEN", f"du does not show the golden layer: {q.out!r}")
    for wt in (w2, w3):  # swept to nothing: topped up from golden again
        judge(ctx, scen, rbk(ctx, "rebuild", ["cargo", "build", "--offline"], wt, env).wait())
    judge(ctx, scen, rbk(ctx, "status", ["golden", "status"], m, env).wait())
    verify(ctx, scen, wts)
    check_locks(ctx, scen)
    rm_fixture(d)


def sc_queue(ctx, rounds):
    """The lock queue under a held RBK_LOCK: fresh builds and test runs finish
    without it, dirty builds wait for it, leads and jobs all finish once it is
    released, and nothing is left running."""
    hold = "import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT); " \
           "fcntl.flock(fd,fcntl.LOCK_EX); print('held',flush=True); time.sleep(float(sys.argv[2]))"
    for rnd in range(rounds):
        scen = f"queue#{rnd}"
        print(f"- {scen}", flush=True)
        d = ctx.fixture_dir(f"queue{rnd}")
        wts, _ = layouts(ctx)["wt3"](d)
        for wt, env, _ in wts:
            judge(ctx, scen, rbk(ctx, "prime", ["cargo", "test", "--offline", "--no-run", "-q"], wt, env).wait())
        h = Proc(ctx, "holder", [sys.executable, "-c", hold, ctx.lock, "600"], d, wts[0][1])
        h.p.stdout.readline()
        # nothing to compile: must not wait
        for wt, env, _ in wts:
            for args in (["cargo", "build", "--offline", "-q"], ["cargo", "test", "--offline", "-q"],
                         ["cargo", "test", "--offline", "--", "--list"]):
                p = rbk(ctx, "fresh", args, wt, env, timeout=40).wait()
                judge(ctx, scen, p)
                if "waiting for" in (p.err or ""):
                    ctx.find(scen, "QUEUED", f"`{' '.join(args)}` with nothing to compile waited for the lock")
        # work to do: must wait, then all finish after the release
        ps = []
        for i, (wt, env, _) in enumerate(wts):
            set_mark(wt, f"q{i}")
            e = dict(env, RBK_PRIORITY=ctx.rng.choice(["", "lead"]))
            ps.append(rbk(ctx, "dirty", ["cargo", ctx.rng.choice(["build", "test"]), "--offline", "-q"], wt, e))
            time.sleep(ctx.rng.uniform(0.1, 0.5))
        time.sleep(1.5)
        for p in ps:
            if p.p.poll() is not None:
                ctx.find(scen, "NOLOCK", f"a dirty build finished while RBK_LOCK was held: {p.p.returncode}")
        h.kill(signal.SIGKILL)
        h.wait()
        for p in ps:
            judge(ctx, scen, p.wait())
        for i, (wt, env, _) in enumerate(wts):
            set_mark(wt, ["main", "wt2", "wt3"][i])
        verify(ctx, scen, wts)
        check_locks(ctx, scen)
        rm_fixture(d)


SCENARIOS = ["layouts", "hardlinks", "lock", "abba", "concurrent", "sweep_during_build", "kill", "json",
             "worktree_removed", "seed_race", "golden", "queue"]


def main():
    global HOST, RBK
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="~1-2 min instead of ~10")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--root", default="/dev/shm/rbkload" if os.path.isdir("/dev/shm") else "/tmp/rbkload")
    ap.add_argument("--only", default="", help="comma-separated: " + ",".join(SCENARIOS))
    ap.add_argument("--keep", action="store_true", help="keep the root afterwards")
    ap.add_argument("--report", help="write a JSON report here")
    ap.add_argument("--rbk", help="the rbk to test (default: ../rbk next to this file)")
    a = ap.parse_args()
    if a.rbk:
        RBK = os.path.abspath(a.rbk)
    HOST = subprocess.run(["rustc", "-vV"], capture_output=True, text=True).stdout.split("host: ")[1].split()[0]
    global CTX
    ctx = CTX = Ctx(a)
    if os.path.exists(ctx.root):
        shutil.rmtree(ctx.root)
    os.makedirs(ctx.root)
    only = set(a.only.split(",")) - {""}
    q = a.quick
    plan = {"layouts": lambda: sc_layouts(ctx), "hardlinks": lambda: sc_hardlinks(ctx),
            "lock": lambda: sc_lock(ctx), "abba": lambda: sc_abba(ctx, 2 if q else 6),
            "concurrent": lambda: sc_concurrent(ctx, 4 if q else 30),
            "sweep_during_build": lambda: sc_sweep_during_build(ctx, 1 if q else 4),
            "kill": lambda: sc_kill(ctx, 3 if q else 16), "json": lambda: sc_json(ctx, 3 if q else 12),
            "worktree_removed": lambda: sc_worktree_removed(ctx, 1 if q else 5),
            "seed_race": lambda: sc_seed_race(ctx, 1 if q else 5),
            "golden": lambda: sc_golden(ctx), "queue": lambda: sc_queue(ctx, 1 if q else 4)}
    if q and not only:
        only = {"layouts", "hardlinks", "lock", "abba", "concurrent", "kill", "json", "seed_race", "golden",
                "queue"}
    t0 = time.monotonic()
    stopped = None
    try:
        for name in SCENARIOS:
            if only and name not in only:
                continue
            ts = time.monotonic()
            try:
                plan[name]()
            except SystemExit:
                raise
            except Exception as e:  # a harness bug must not hide the rest
                import traceback
                ctx.find(name, "HARNESS", traceback.format_exc())
            print(f"  ({name}: {time.monotonic() - ts:.0f}s)", flush=True)
    except SystemExit as e:
        stopped = str(e)
        print(f"STOPPED: {stopped}", flush=True)
    dur = time.monotonic() - t0
    rep = {"seed": a.seed, "quick": q, "duration_s": round(dur, 1), "counts": ctx.counts,
           "peak_root_mb": round(ctx.peak_bytes / 2 ** 20, 1),
           "min_free_gb": round(getattr(ctx, "min_free", 0) / 2 ** 30, 2), "stopped": stopped, "findings": ctx.findings}
    if a.report:
        json.dump(rep, open(a.report, "w"), indent=1)
    if not a.keep:
        shutil.rmtree(ctx.root, ignore_errors=True)
    print(f"\n{len(ctx.findings)} finding(s) in {dur:.0f}s; {ctx.counts['procs']} processes, "
          f"{ctx.counts['hangs']} hang(s), peak root {rep['peak_root_mb']} MB")
    kinds = {}
    for f in ctx.findings:
        kinds.setdefault((f["kind"], f["scenario"].split("#")[0].split(":")[0]), 0)
        kinds[(f["kind"], f["scenario"].split("#")[0].split(":")[0])] += 1
    for (k, s), n in sorted(kinds.items()):
        print(f"  {k:<13} {s:<22} x{n}")
    return 1 if ctx.findings or stopped else 0


if __name__ == "__main__":
    sys.exit(main())
