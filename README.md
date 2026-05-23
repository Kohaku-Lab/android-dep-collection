# android-dep-collection

Cross-compiled **Android wheels** for native-code Python packages that
Chaquopy's curated index doesn't carry — published as GitHub Release
assets so downstream consumers (KohakuTerrarium's Android build,
other Kohaku-Lab projects) can `pip install` them via direct URL
refs.

## Why this exists

[Chaquopy 13](https://chaquo.com/pypi-13.1/) (the Python-on-Android
runtime Briefcase Android uses) ships a curated index of native
Android wheels.  Common things like `pyyaml`, `numpy`, `Pillow`,
`lxml`, `bcrypt` are there.  But a number of Rust/PyO3 + C-binding
packages aren't, and pip on Android can't build them from sdist
(no native toolchain at install time on the device build env).

Packages we currently cross-compile:

| Package | Why we need it | Upstream |
|---|---|---|
| `pydantic-core` | Required by `pydantic` ≥ 2.0; used pervasively in KohakuTerrarium | [pydantic/pydantic-core](https://github.com/pydantic/pydantic-core) |

Easy to add more — see [Adding a wheel](#adding-a-wheel) below.

## Wheel tags

Wheels use **PEP 738** `android_<api>_<abi>` platform tags
(specifically `android_24_arm64_v8a` + `android_24_x86_64`), the
form Chaquopy ≥ 13 expects.  See
[PEP 738](https://peps.python.org/pep-0738/) for the canonical
spec; the same tagging pattern Chaquopy uses for its own curated
index (e.g. `pyyaml-6.0.3-0-cp313-cp313-android_24_arm64_v8a.whl`).

API level **24** = Android 7.0, matches Chaquopy's minSdk floor.

## Build pipeline

```
manifest.toml            ← pinned list of upstream package + version
       │
       │ tag push → CI matrix per ABI
       ▼
maturin / cargo-ndk      ← cross-compile via PyO3/maturin-action
       │                   (v1.13.3+, with ANDROID_API_VERSION=24
       │                    so it emits proper android_24_<abi> tags
       │                    directly — no retag step)
       ▼
GitHub Release           ← every wheel uploaded as a release asset
       │                   at a stable URL: tag/<wheel-filename>.whl
       ▼
consumer postcreate.py   ← direct URL ref in pip requirements.txt
                           per ABI via platform_machine markers
```

## Adding a wheel

1. Add an entry to `manifest.toml`:

   ```toml
   [[wheel]]
   name = "package-name"
   version = "X.Y.Z"
   # Optional — defaults to {name} on PyPI
   pypi_name = "package_name_underscored"
   ```

2. Tag a release: `git tag v$(date +%Y.%m.%d) && git push --tags`.
   CI builds + uploads.

3. In the consumer (KohakuTerrarium's `postcreate.py`), add the
   URL refs:

   ```python
   patched.append(
       f"package-name @ {base}/package_name-X.Y.Z-cp313-cp313-android_24_arm64_v8a.whl"
       " ; platform_machine == 'aarch64'"
   )
   patched.append(
       f"package-name @ {base}/package_name-X.Y.Z-cp313-cp313-android_24_x86_64.whl"
       " ; platform_machine == 'x86_64'"
   )
   ```

## Layout

```
.
├── README.md
├── manifest.toml            ← what to build (versions pinned here)
├── build.py                 ← fetches sdist + invokes maturin
├── .github/
│   └── workflows/
│       ├── build.yml        ← matrix: each wheel × {arm64, x86_64}
│       └── ci.yml           ← PR smoke: build arm64-only on each wheel
└── (no upstream source — fetched at build time by build.py)
```

## Versioning

Releases are tagged with the wheel-set version, e.g.
`v2026.05.23`.  The release's assets are wheels for every package
in `manifest.toml` at the versions pinned there.  Consumers
URL-ref to a specific release tag, so bumping `manifest.toml` is
a deliberate operator decision (no surprise upstream pull-in).

## License

This repo bundles build tooling only.  Wheels distributed by this
repo carry the upstream package's license (e.g. pydantic-core is
MIT).  See each wheel's `LICENSE` / `METADATA` for details.
