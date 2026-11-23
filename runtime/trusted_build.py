"""Installed-code-only, disposable reconstruction of a compatible source commit.

This is not a package installer or an executor for fetched source. Callers must
bind the complete remote build-input inventory to ``build_inputs`` before staging
these frozen installed bytes. Dependencies are operator-installed prerequisites.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile

from .orchestrator import RuntimeError


INPUT_TREES = ('src', 'scripts', 'schema', 'public')
ROOT_INPUTS = ('astro.config.mjs', 'tsconfig.json', 'package.json',
               'package-lock.json', '.node-version', '.npmrc')
# Everything else is an input candidate, not silently ignored configuration.
# These paths cannot participate in the installed build entry point. Local
# output/cache/dependency directories are never staged or fetched as inputs.
NON_BUILD = frozenset(('data', 'dist', '.git', '.gitignore', 'README.md',
                       'renovate.json', '.github', 'docs', 'tests', 'runtime',
                       'integrations', 'node_modules', '.astro', '.cache', '__pycache__'))


def is_build_input(relative):
    return relative.split('/')[0] not in NON_BUILD


def build_inputs(installed, remaining):
    """Freeze the full regular-file inventory; refuse unknown installed config.

    .npmrc is bound including its absence (the current project has none). Adding
    another build config requires a reviewed extension here, not auto-discovery
    that might copy an operator's .env/credentials into public build code.
    """
    result = {}

    def visit(path):
        remaining()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(mode):
            result[path.relative_to(installed).as_posix()] = path.read_bytes()
        else:
            raise RuntimeError('nonregular installed production build input')

    try:
        for name in INPUT_TREES:
            if not (installed / name).is_dir() or (installed / name).is_symlink():
                raise RuntimeError('missing installed production build inputs')
        for name in ROOT_INPUTS[:-1]:
            if not (installed / name).is_file():
                raise RuntimeError('missing installed production build inputs')
        for path in sorted(installed.iterdir()):
            if not is_build_input(path.name):
                continue
            if path.name not in (*INPUT_TREES, *ROOT_INPUTS):
                raise RuntimeError('unsupported installed production build input')
            visit(path)
    except OSError as exc:
        raise RuntimeError('cannot read installed production build inputs') from exc
    return result


def write_public(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    # The parent workspace stays 0700; source-data validators require public
    # files to be 0644 even under an operator's restrictive umask/archive mode.
    path.chmod(0o644)


def stage(installed, root, inputs):
    """Stage only frozen installed inputs; isolate both Astro and Vite caches."""
    dependencies = installed / 'node_modules'
    if not dependencies.is_dir() or not all((dependencies / name).is_file() for name in
            ('astro/bin/astro.mjs', 'ajv/package.json', 'ajv-formats/package.json')):
        raise RuntimeError('installed production build dependencies required; no automatic install')
    for name, raw in inputs.items():
        write_public(root / name, raw)
    (root / 'node_modules').symlink_to(dependencies.resolve(), target_is_directory=True)
    # This wrapper is installed runtime policy, never downloaded configuration.
    # Default Astro/Vite caches otherwise traverse the shared dependency link.
    (root / 'astro.config.mjs').rename(root / 'base-astro.config.mjs')
    write_public(root / 'astro.config.mjs', b"import config from './base-astro.config.mjs';\n"
                 b"export default {...config,cacheDir:'./.cache/astro',"
                 b"vite:{...config.vite,cacheDir:'.cache/vite'}};\n")
    for directory in ('home', 'tmp', '.cache'):
        (root / directory).mkdir(mode=0o700, exist_ok=True)
    # No inherited HOME, NODE_OPTIONS, npm settings, source provenance, GitHub
    # tokens, mail credentials, or current installed data/output paths.
    return dict(PATH='/usr/bin:/bin', HOME=str(root / 'home'), TMPDIR=str(root / 'tmp'),
                XDG_CACHE_HOME=str(root / '.cache'), LANG='C.UTF-8', TZ='UTC',
                ASTRO_TELEMETRY_DISABLED='1', ASTRO_DISABLE_UPDATE_CHECK='true',
                TWIHN_DATA_DIR=str(root / 'data'))


def run_node(node, args, root, env, remaining, *, max_output=20*1024*1024):
    """Bound a whole Node process group, including build.mjs's Astro child."""
    remaining()
    try:
        with tempfile.TemporaryFile(dir=root / 'tmp') as output:
            with subprocess.Popen([node, *args], cwd=root, env=env, stdout=output,
                                  stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                  start_new_session=True, umask=0o022) as process:
                try:
                    process.wait(timeout=remaining())
                except BaseException:
                    # A timed-out build must not outlive the workspace/deadline.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise
                remaining()
                output.seek(0)
                raw = output.read(max_output + 1)
                if len(raw) > max_output:
                    raise RuntimeError('production expectation byte limit exceeded')
                return process.returncode, raw
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('production local verification failed or exceeded deadline') from exc
