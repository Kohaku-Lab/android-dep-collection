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

    source_dir = _checkout_upstream(name, version, upstream, upstream_tag)
    prefix_lib = _stage_chaquopy_prefix(triple, py_full)

    env = os.environ.copy()
    env["ANDROID_API_VERSION"] = str(api_level)
    env["PYO3_CROSS_LIB_DIR"] = str(prefix_lib)
    env["PYO3_CROSS_PYTHON_VERSION"] = py_full.rsplit(".", 1)[0]  # "3.13"
    # ``-L native=`` so the linker finds libpython3.13.so even if
    # maturin's internal cross-config probes ``prefix/lib/python3.13``
    # instead (libpython lives one level up).
    existing_rustflags = env.get("RUSTFLAGS", "").strip()
    env["RUSTFLAGS"] = f"-L native={prefix_lib} {existing_rustflags}".strip()

    print(f"  source:  {source_dir}")
    print(f"  triple:  {triple}")
    print(f"  prefix:  {prefix_lib}")
    print(f"  api:     {api_level}")

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
    subprocess.run(cmd, cwd=source_dir, env=env, check=True)
    print(f"=== {name}/{abi} done ===")


def _checkout_upstream(
    name: str, version: str, repo_url: str, tag: str
) -> Path:
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
    cache_root = (
        Path.home()
        / ".cache"
        / "android-dep-collection"
        / "chaquopy-prefix"
    )
    cache_root.mkdir(parents=True, exist_ok=True)
    prefix_root = cache_root / f"{py_full}-{triple}"
    lib_dir = prefix_root / "prefix" / "lib"
    libpython = (
        lib_dir / f"libpython{py_full.rsplit('.', 1)[0]}.so"
    )
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
