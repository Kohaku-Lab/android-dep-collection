"""Build Android wheels for every entry in ``manifest.toml``.

Runs in CI (and locally for testing).  For each ``[[wheel]]`` entry:

1. Clone the upstream repo at the pinned tag into a workspace dir.
2. Stage the Chaquopy Python cross-prefix so PyO3 can find the
   target libpython (PyO3 needs a target Python lib to link
   against during cross-compile — Chaquopy publishes one as a
   Maven artifact).
3. Run ``maturin build --release --target <triple>`` with
   ``ANDROID_API_VERSION=<api>`` set, so maturin emits a wheel
   tagged ``cp313-cp313-android_<api>_<abi>``.
4. Place the resulting wheel in ``dist/``.

Designed to be invoked once per (wheel × ABI) cell from a GitHub
Actions matrix so each cell runs on its own runner.  Locally, pass
``--wheel <name> --abi <abi>`` to build a single cell.

Usage::

    python build.py --wheel pydantic-core --abi arm64-v8a
    python build.py --all  # serial, all cells; for local sanity
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
MANIFEST = REPO_ROOT / "manifest.toml"
WORKSPACE = REPO_ROOT / ".build-workspace"
DIST = REPO_ROOT / "dist"

# ABI → Rust target triple.  PyO3/maturin uses the Rust triple
# but emits wheel tags from the Android ABI string.
_ABI_TO_TRIPLE: dict[str, str] = {
    "arm64-v8a": "aarch64-linux-android",
    "x86_64": "x86_64-linux-android",
    "armeabi-v7a": "armv7-linux-androideabi",
    "x86": "i686-linux-android",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel",
        help="Build only the named wheel (defaults: all in manifest)",
    )
    parser.add_argument(
        "--abi",
        help=(
            "Build only the named ABI (defaults: all ABIs in manifest).  "
            "Valid: arm64-v8a, x86_64, armeabi-v7a, x86."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Build every wheel × every ABI in the manifest (slow; CI matrix is better)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=MANIFEST,
        help="Path to manifest.toml",
    )
    parser.add_argument(
        "--dist",
        type=Path,
        default=DIST,
        help="Output directory for built wheels",
    )
    args = parser.parse_args(argv)

    manifest = _load_manifest(args.manifest)
    abis: list[str] = list(manifest["abis"])
    wheels: list[dict] = list(manifest["wheel"])
    api_level: int = int(manifest["android_api_level"])
    py_full: str = str(manifest["chaquopy_python_full_version"])

    target_wheels = (
        [w for w in wheels if w["name"] == args.wheel] if args.wheel else wheels
    )
    if args.wheel and not target_wheels:
        print(f"error: no wheel named {args.wheel!r} in manifest", file=sys.stderr)
        return 2

    target_abis = [args.abi] if args.abi else abis
    if args.abi and args.abi not in abis:
        print(
            f"error: ABI {args.abi!r} not in manifest abis {abis}",
            file=sys.stderr,
        )
        return 2

    args.dist.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    for wheel in target_wheels:
        for abi in target_abis:
            print(f"\n=== build {wheel['name']} v{wheel['version']} for {abi} ===")
            try:
                build_one(
                    wheel=wheel,
                    abi=abi,
                    api_level=api_level,
                    py_full=py_full,
                    dist=args.dist,
                )
            except Exception as e:
                print(f"!!! {wheel['name']}/{abi} failed: {e}", file=sys.stderr)
                failures.append(f"{wheel['name']}/{abi}")
    if failures:
        print(f"\nfailures: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


def build_one(
    *,
    wheel: dict,
    abi: str,
    api_level: int,
    py_full: str,
    dist: Path,
) -> None:
    """Build one (wheel × ABI) cell."""
    triple = _ABI_TO_TRIPLE[abi]
    name = wheel["name"]
    version = wheel["version"]
    upstream = wheel["upstream"]
    upstream_tag = wheel["upstream_tag"]
    # Optional ``subdir`` lets us point maturin at a sub-package
    # inside a monorepo (huggingface/safetensors and
    # huggingface/tokenizers both keep their Python bindings under
    # ``bindings/python/``).  Defaults to repo root.
    subdir = wheel.get("subdir")

    source_dir = _checkout_upstream(name, version, upstream, upstream_tag)
    build_dir = source_dir / subdir if subdir else source_dir
    if not build_dir.is_dir():
        raise RuntimeError(
            f"manifest declares subdir={subdir!r} for {name} but "
            f"it does not exist under {source_dir}; the upstream "
            "tag may have moved its Python package."
        )

    # ``disable_abi3``: opt-out for upstreams whose abi3 build is
    # mis-tagged (the wheel claims abi3 stability but actually
    # references non-stable-ABI CPython internals).  Stripping the
    # ``abi3-pyXX`` feature from pyo3 deps in Cargo.toml + the
    # ``py-limited-api`` setting from pyproject.toml makes maturin
    # emit a ``cp313-cp313`` wheel instead of ``cpXX-abi3``.  We do
    # this AFTER checkout but BEFORE invoking maturin so the patch
    # is invisible to the upstream repo state (the workspace dir is
    # discarded between builds).  Idempotent against re-builds of
    # the cached checkout — re-applying the strip on already-stripped
    # files is a no-op.
    if wheel.get("disable_abi3"):
        _strip_abi3_features(source_dir)

    prefix_lib = _stage_chaquopy_prefix(triple, py_full)

    env = os.environ.copy()
    env["ANDROID_API_VERSION"] = str(api_level)
    env["PYO3_CROSS_LIB_DIR"] = str(prefix_lib)
    env["PYO3_CROSS_PYTHON_VERSION"] = py_full.rsplit(".", 1)[0]  # "3.13"

    # Point Cargo + cc-rs at the NDK's clang-as-linker for this
    # target.  Without this, ``cargo rustc`` falls back to the
    # host's ``/usr/bin/ld`` (x86_64 GNU ld) to link aarch64
    # object files, which fails with the obscure
    #     /usr/bin/ld: ... Relocations in generic ELF (EM: 183)
    #     /usr/bin/ld: ... error adding symbols: file in wrong format
    # error.  ``EM: 183`` is the ELF machine number for AArch64 —
    # host ld doesn't understand it.  The fix is documented in
    # PyO3/maturin's Android cross-compile guide; PyO3/maturin-action
    # sets these env vars automatically when given a *-linux-android
    # target, but we invoke maturin directly via build.py so we
    # have to wire them ourselves.
    _set_ndk_linker_env(env, triple=triple, api_level=api_level)

    # ``-L native=`` so the linker finds libpython3.13.so even if
    # maturin's internal cross-config probes ``prefix/lib/python3.13``
    # instead (libpython lives one level up).
    existing_rustflags = env.get("RUSTFLAGS", "").strip()
    env["RUSTFLAGS"] = f"-L native={prefix_lib} {existing_rustflags}".strip()

    print(f"  source:  {source_dir}")
    print(f"  triple:  {triple}")
    print(f"  prefix:  {prefix_lib}")
    print(f"  api:     {api_level}")
    print(
        f"  linker:  {env.get('CARGO_TARGET_' + _cargo_var(triple) + '_LINKER', '(unset)')}"
    )

    # Some upstream pyproject configs ship maturin tooling specs.
    # Use whatever maturin the env has — CI pins via pip install.
    cmd = [
        sys.executable,
        "-m",
        "maturin",
        "build",
        "--release",
        "--target",
        triple,
        "--interpreter",
        f"python{py_full.rsplit('.', 1)[0]}",
        "--out",
        str(dist.absolute()),
        # Critical: --compatibility off so maturin doesn't try to
        # auditwheel-ify the result against manylinux policy.
        "--compatibility",
        "off",
    ]
    print(f"  cmd:     {' '.join(cmd)}")
    print(f"  cwd:     {build_dir}")
    subprocess.run(cmd, cwd=build_dir, env=env, check=True)
    print(f"=== {name}/{abi} done ===")


_ABI3_FEATURE_RE = re.compile(r'"abi3(?:-py\d+)?"\s*,?\s*')
_PY_LIMITED_API_RE = re.compile(r'^\s*py-limited-api\s*=\s*"[^"]*"\s*$', re.MULTILINE)


def _strip_abi3_features(source_dir: Path) -> None:
    """Remove ``abi3`` opt-ins from every Cargo.toml + pyproject.toml
    under ``source_dir``.

    Two stripping passes:

    1. **Cargo.toml**: strip ``"abi3"`` and ``"abi3-pyXX"`` feature
       strings from any features array.  pyo3's abi3 feature is what
       triggers maturin's abi3 wheel-tag detection — removing it
       makes maturin emit ``cp313-cp313`` instead.

    2. **pyproject.toml**: strip ``py-limited-api = "..."`` from any
       ``[tool.maturin]`` table.  Some upstreams (primp) set this
       directly even when their Cargo features already imply abi3;
       maturin honours it and tags the wheel ``abi3`` even after
       Cargo.toml has been stripped.

    Idempotent — re-applying on an already-patched tree is a no-op.
    Logs each file that changed.
    """
    cargo_count = 0
    for cargo_toml in source_dir.rglob("Cargo.toml"):
        text = cargo_toml.read_text(encoding="utf-8")
        new_text = _ABI3_FEATURE_RE.sub("", text)
        # Tidy up any trailing ``, ]`` artefacts from removing the
        # last item in a features array.  ``"foo", "abi3-py310"]``
        # → ``"foo", ]`` → ``"foo"]``.
        new_text = re.sub(r",\s*\]", "]", new_text)
        if new_text != text:
            cargo_toml.write_text(new_text, encoding="utf-8")
            cargo_count += 1
            print(
                f"  patched abi3 features out of "
                f"{cargo_toml.relative_to(source_dir)}"
            )

    pyproject_count = 0
    for pyproject_toml in source_dir.rglob("pyproject.toml"):
        text = pyproject_toml.read_text(encoding="utf-8")
        new_text = _PY_LIMITED_API_RE.sub("", text)
        if new_text != text:
            pyproject_toml.write_text(new_text, encoding="utf-8")
            pyproject_count += 1
            print(
                f"  patched py-limited-api out of "
                f"{pyproject_toml.relative_to(source_dir)}"
            )

    if cargo_count == 0 and pyproject_count == 0:
        print(
            "  disable_abi3 set but no abi3 features / py-limited-api "
            "found to strip (already patched, or upstream doesn't use abi3)"
        )


def _cargo_var(triple: str) -> str:
    """Cargo env var suffix for a Rust target triple.

    Cargo accepts ``CARGO_TARGET_<TRIPLE_UPPERCASE_UNDERSCORED>_LINKER``
    etc.; this helper does the case + hyphen → underscore swap.
    """
    return triple.upper().replace("-", "_")


def _ndk_host_arch() -> str:
    """Detect the NDK prebuilt-host folder for the current OS.

    NDK ships clang under
    ``$ANDROID_NDK_ROOT/toolchains/llvm/prebuilt/<host>/bin/`` —
    ``<host>`` is ``linux-x86_64`` on a GitHub ubuntu runner,
    ``darwin-x86_64`` on macOS, ``windows-x86_64`` on Windows.
    Returns the matching string.
    """
    p = sys.platform
    if p == "linux":
        return "linux-x86_64"
    if p == "darwin":
        return "darwin-x86_64"
    if p == "win32":
        return "windows-x86_64"
    raise RuntimeError(f"unsupported NDK host platform: {p}")


def _ndk_clang_name(triple: str, api_level: int) -> str:
    """Per-triple clang binary name shipped by the NDK.

    NDK uses ``<triple-without-vendor>-clang`` for most ABIs +
    the API level baked in:

      aarch64-linux-android24-clang
      x86_64-linux-android24-clang
      armv7a-linux-androideabi24-clang   (note: ``armv7a``, not ``armv7``)
      i686-linux-android24-clang
    """
    if triple == "armv7-linux-androideabi":
        # NDK names this one differently from the Rust target triple.
        return f"armv7a-linux-androideabi{api_level}-clang"
    return f"{triple}{api_level}-clang"


def _set_ndk_linker_env(env: dict[str, str], *, triple: str, api_level: int) -> None:
    """Populate Cargo + cc-rs env vars so the toolchain uses the
    NDK clang as both the linker and the C compiler for this
    target.

    Sets:
      CARGO_TARGET_<TRIPLE>_LINKER       — Cargo invokes this to link
      CARGO_TARGET_<TRIPLE>_AR           — Cargo invokes this to archive
      CC_<triple>                        — cc-rs reads this when crates
                                            need a C compiler at build
      AR_<triple>                        — cc-rs archive
    """
    ndk_root = env.get("ANDROID_NDK_ROOT") or env.get("ANDROID_NDK_HOME")
    if not ndk_root:
        raise RuntimeError(
            "ANDROID_NDK_ROOT / ANDROID_NDK_HOME unset; CI workflow "
            "must run nttld/setup-ndk first"
        )
    host = _ndk_host_arch()
    bin_dir = Path(ndk_root) / "toolchains" / "llvm" / "prebuilt" / host / "bin"
    clang = bin_dir / _ndk_clang_name(triple, api_level)
    ar = bin_dir / "llvm-ar"
    if not clang.is_file():
        raise RuntimeError(
            f"NDK clang missing: {clang}.  NDK layout may have "
            "changed; check $ANDROID_NDK_ROOT/toolchains/llvm/prebuilt/"
        )

    cargo_suffix = _cargo_var(triple)
    env[f"CARGO_TARGET_{cargo_suffix}_LINKER"] = str(clang)
    env[f"CARGO_TARGET_{cargo_suffix}_AR"] = str(ar)
    # cc-rs uses the unmodified triple in its env var names.
    env[f"CC_{triple}"] = str(clang)
    env[f"AR_{triple}"] = str(ar)
    # Add the NDK bin to PATH so any plain ``clang`` / ``llvm-ar``
    # lookups in build scripts find the NDK versions.
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"


def _checkout_upstream(name: str, version: str, repo_url: str, tag: str) -> Path:
    """Clone the upstream repo at ``tag`` into the workspace.

    Idempotent — if the dir already exists at the right commit,
    reuse it.  Tags are version-stable, so a cached clone is
    safe across CI runs.
    """
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    target = WORKSPACE / f"{name}-{version}"
    if target.is_dir() and (target / ".git").is_dir():
        # Already cloned; ensure the checkout is on the right tag.
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=target,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        target_sha = subprocess.run(
            ["git", "rev-parse", f"refs/tags/{tag}^{{}}"],
            cwd=target,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if head and head == target_sha:
            print(f"  cached at {target}")
            return target
        # Re-clone fresh — simplest way to recover from a dirty
        # cache.
        shutil.rmtree(target)

    print(f"  cloning {repo_url} @ {tag} -> {target}")
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            tag,
            repo_url,
            str(target),
        ],
        check=True,
    )
    return target


def _stage_chaquopy_prefix(triple: str, py_full: str) -> Path:
    """Download + unpack the Chaquopy Python cross-prefix for
    ``triple``.  Returns the path to the prefix's ``lib`` dir
    (where ``libpython3.13.so`` lives).
    """
    cache_root = Path.home() / ".cache" / "android-dep-collection" / "chaquopy-prefix"
    cache_root.mkdir(parents=True, exist_ok=True)
    prefix_root = cache_root / f"{py_full}-{triple}"
    lib_dir = prefix_root / "prefix" / "lib"
    libpython = lib_dir / f"libpython{py_full.rsplit('.', 1)[0]}.so"
    if libpython.is_file():
        return lib_dir

    url = (
        "https://repo.maven.apache.org/maven2/com/chaquo/python/python/"
        f"{py_full}/python-{py_full}-{triple}.tar.gz"
    )
    tarball = prefix_root / "python.tar.gz"
    prefix_root.mkdir(parents=True, exist_ok=True)
    print(f"  fetching prefix: {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        tarball.write_bytes(resp.read())
    subprocess.run(
        ["tar", "-xzf", str(tarball), "-C", str(prefix_root)],
        check=True,
    )
    if not libpython.is_file():
        raise RuntimeError(
            f"libpython missing after extract: {libpython}; "
            f"listing of {lib_dir}: {list(lib_dir.glob('*')) if lib_dir.is_dir() else 'no dir'}"
        )
    return lib_dir


def _load_manifest(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found: {path}")
    with open(path, "rb") as f:
        return tomllib.load(f)


if __name__ == "__main__":
    sys.exit(main())
