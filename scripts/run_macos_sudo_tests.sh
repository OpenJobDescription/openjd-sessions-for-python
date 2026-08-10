#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Runs the POSIX user-impersonation tests on macOS.
#
# The Linux equivalent (scripts/run_sudo_tests.sh) gets its users, groups and
# sudoers rule from a throwaway Docker container. macOS cannot be containerized,
# so the same layout has to be created on the host itself. That makes this script
# the counterpart to that Dockerfile rather than to the docker command: it
# provisions, runs, and then removes what it created.
#
# Provisioned layout (mirrors testing_containers/localuser_sudo_environment/Dockerfile):
#   <you>            -- runs the pytests; joined to the shared group
#   openjd-target    -- the impersonated user; also in the shared group
#   openjd-disjoint  -- shares no group with you (temp-dir permission tests)
#
# USAGE
#   scripts/run_macos_sudo_tests.sh              # provision, test, clean up
#   scripts/run_macos_sudo_tests.sh --keep       # leave the environment in place
#   scripts/run_macos_sudo_tests.sh --cleanup-only
#   scripts/run_macos_sudo_tests.sh -- -k test_basic_operation   # extra pytest args
#
# Requires sudo. On your own machine prefer the default (cleaning up) run: this
# creates real local accounts, a real /etc/sudoers.d file and a symlink in
# /usr/local/bin, none of which you want left behind.
#
# NOT undone by the teardown: the impersonated user needs to read the test support
# files, so this adds o+r to test/openjd/sessions_v0/support_files and o+x (traverse
# only) to the directories leading there, plus o+rX on HATCH_DATA_DIR. Those bits stay
# after the run. The rest of the working tree is left alone, so untracked or
# credential-bearing files elsewhere under the repo are unaffected.

set -euo pipefail

if ! test -d scripts; then
    echo "Must run from the root of the repository"
    exit 1
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This script is for macOS. On Linux use scripts/run_sudo_tests.sh."
    exit 1
fi

export OPENJD_TEST_SUDO_TARGET_USER="${OPENJD_TEST_SUDO_TARGET_USER:-openjd-target}"
export OPENJD_TEST_SUDO_SHARED_GROUP="${OPENJD_TEST_SUDO_SHARED_GROUP:-openjd-shared}"
export OPENJD_TEST_SUDO_DISJOINT_USER="${OPENJD_TEST_SUDO_DISJOINT_USER:-openjd-disjoint}"
export OPENJD_TEST_SUDO_DISJOINT_GROUP="${OPENJD_TEST_SUDO_DISJOINT_GROUP:-openjd-disjointgrp}"

# Hatch's default data dir lives under ~/Library, which other users cannot
# traverse. The impersonation tests execute the hatch venv's python AS the target
# user, so the venv has to sit somewhere world-traversable.
export HATCH_DATA_DIR="${HATCH_DATA_DIR:-/opt/hatch}"

# macOS's per-user temp dir (/var/folders/<hash>/T, mode 700) is not traversable
# by the impersonated user, and /var is a symlink to /private/var (which TempDir
# resolves but gettempdir() does not). Use a dedicated, already-resolved,
# world-writable temp root so the tests get the /tmp semantics they have on Linux.
#
# Deliberately NOT configurable. cleanup() does `rm -rf` on this path, so a
# caller-supplied value turns a typo (or TMPDIR=/tmp) into a destructive run. The
# tests only need *a* world-traversable, already-resolved directory, so there is
# nothing to gain by making the choice adjustable.
export TMPDIR=/private/tmp/openjd-tests

# SUDO_USER is inherited from the environment, and TEST_USER is interpolated into a
# /etc/sudoers.d file below. Validate it before it is used anywhere: a value containing a
# newline can append arbitrary rules, and `visudo -cf` does not save us because the file is
# written before it runs (so a rejected file still lands on disk) and because a payload
# ending in '#' comments out the remainder and validates cleanly. Also guards the
# dseditgroup and chown calls that take this value.
TEST_USER="${SUDO_USER:-$(id -un)}"
if [[ ! "${TEST_USER}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "ERROR: refusing to run with an unusual user name: ${TEST_USER}"
    echo "       (TEST_USER comes from SUDO_USER, or from 'id -un' when that is unset)"
    exit 1
fi
if ! id -u "${TEST_USER}" > /dev/null 2>&1; then
    echo "ERROR: '${TEST_USER}' is not a local user on this host."
    exit 1
fi
SUDOERS_FILE=/etc/sudoers.d/openjd-cross-user-tests
PYTHON_SHIM=/usr/local/bin/python
# Whether *this* script created PYTHON_SHIM. On a developer machine that path is
# often a real symlink managed by pyenv, Homebrew or a python.org installer, so
# cleanup must only remove it if we were the one who put it there.
PYTHON_SHIM_CREATED="False"
# Likewise for the temp root: cleanup() does `rm -rf` on it, so only remove it if we
# were the one who made it.
TMPDIR_CREATED="False"

KEEP="False"
CLEANUP_ONLY="False"
PYTEST_ARGS=()
while [[ "${1:-}" != "" ]]; do
    case $1 in
        -h|--help)
            sed -n '4,31p' "$0" | sed 's/^# \{0,1\}//'
            exit 1
            ;;
        --keep)          KEEP="True" ;;
        --cleanup-only)  CLEANUP_ONLY="True" ;;
        --)              shift; PYTEST_ARGS=("$@"); break ;;
        *)
            echo "Unrecognized parameter: $1"
            exit 1
            ;;
    esac
    shift
done

cleanup() {
    echo "--- Removing the cross-user test environment ---"
    # Best-effort throughout: a partially-provisioned environment must still be
    # removable, so nothing here may abort the teardown.
    sudo rm -f "${SUDOERS_FILE}" || true
    # Only remove the python alias if provision() created it; see PYTHON_SHIM_CREATED.
    if [[ "${PYTHON_SHIM_CREATED}" == "True" ]]; then
        sudo rm -f "${PYTHON_SHIM}" || true
    fi
    for u in "${OPENJD_TEST_SUDO_TARGET_USER}" "${OPENJD_TEST_SUDO_DISJOINT_USER}"; do
        sudo sysadminctl -deleteUser "${u}" > /dev/null 2>&1 || true
        # -deleteUser leaves the self-named group behind when we created it ourselves.
        sudo dseditgroup -o delete "${u}" > /dev/null 2>&1 || true
    done
    for g in "${OPENJD_TEST_SUDO_SHARED_GROUP}" "${OPENJD_TEST_SUDO_DISJOINT_GROUP}"; do
        sudo dseditgroup -o delete "${g}" > /dev/null 2>&1 || true
    done
    # Only remove the temp root if provision() created it: the path is fixed, but a
    # developer may already have one there from an earlier interrupted run or their own use.
    if [[ "${TMPDIR_CREATED}" == "True" ]]; then
        sudo rm -rf "${TMPDIR}" || true
    fi
    sudo dscacheutil -flushcache || true
    echo "--- Done ---"
}

if [[ "${CLEANUP_ONLY}" == "True" ]]; then
    # --cleanup-only recovers from an interrupted run, where provision() never set
    # PYTHON_SHIM_CREATED in this process. Only claim the alias if it points at the
    # target we would have used; a pyenv/Homebrew symlink points somewhere else and
    # is left alone.
    if [[ -L "${PYTHON_SHIM}" && "$(readlink "${PYTHON_SHIM}")" == "/usr/bin/python3" ]]; then
        PYTHON_SHIM_CREATED="True"
    fi
    cleanup
    exit 0
fi

provision() {
    echo "--- Provisioning users and groups (requires sudo) ---"
    sudo dseditgroup -o create "${OPENJD_TEST_SUDO_SHARED_GROUP}"
    sudo dseditgroup -o create "${OPENJD_TEST_SUDO_DISJOINT_GROUP}"

    # Target user: impersonated by the tests; shares a group with the test user.
    sudo sysadminctl -addUser "${OPENJD_TEST_SUDO_TARGET_USER}" \
        -fullName "OpenJD Test Target" -password "OpenJD-ci-test-1!" -shell /bin/zsh
    sudo createhomedir -c -u "${OPENJD_TEST_SUDO_TARGET_USER}" > /dev/null
    sudo dseditgroup -o edit -a "${OPENJD_TEST_SUDO_TARGET_USER}" -t user "${OPENJD_TEST_SUDO_SHARED_GROUP}"

    # Linux useradd gives every user a self-named group and test_cleanup_posix_user
    # chowns to "user:user"; macOS does not, so create it explicitly. The test user
    # must NOT be a member of it.
    sudo dseditgroup -o create "${OPENJD_TEST_SUDO_TARGET_USER}"
    sudo dseditgroup -o edit -a "${OPENJD_TEST_SUDO_TARGET_USER}" -t user "${OPENJD_TEST_SUDO_TARGET_USER}"

    # Disjoint user: no group in common with the test user.
    sudo sysadminctl -addUser "${OPENJD_TEST_SUDO_DISJOINT_USER}" \
        -fullName "OpenJD Test Disjoint" -password "OpenJD-ci-test-1!" -shell /bin/zsh
    sudo createhomedir -c -u "${OPENJD_TEST_SUDO_DISJOINT_USER}" > /dev/null
    sudo dseditgroup -o edit -a "${OPENJD_TEST_SUDO_DISJOINT_USER}" -t user "${OPENJD_TEST_SUDO_DISJOINT_GROUP}"

    # The test-running user joins the shared group (matches the Docker layout).
    sudo dseditgroup -o edit -a "${TEST_USER}" -t user "${OPENJD_TEST_SUDO_SHARED_GROUP}"

    # Passwordless sudo to the target user (and to itself), mirroring the hostuser
    # rule in the Linux test container. Validated before it is trusted: a malformed
    # file in /etc/sudoers.d breaks sudo host-wide.
    echo "${TEST_USER} ALL=(${OPENJD_TEST_SUDO_TARGET_USER},${TEST_USER}) NOPASSWD: ALL" \
        | sudo tee "${SUDOERS_FILE}" > /dev/null
    sudo chmod 440 "${SUDOERS_FILE}"
    sudo visudo -cf "${SUDOERS_FILE}"

    # test_basic_operation runs a bare `python` as the target user via `sudo -i`;
    # macOS ships python3 only, so provide the alias -- but only if nothing is there
    # already. pyenv, Homebrew and the python.org installers all manage this path,
    # and clobbering (or later deleting) a developer's `python` is not ours to do.
    sudo mkdir -p "$(dirname "${PYTHON_SHIM}")"
    if [[ -e "${PYTHON_SHIM}" || -L "${PYTHON_SHIM}" ]]; then
        echo "${PYTHON_SHIM} already exists; leaving it in place"
    else
        sudo ln -s /usr/bin/python3 "${PYTHON_SHIM}"
        PYTHON_SHIM_CREATED="True"
    fi

    sudo dscacheutil -flushcache

    if [[ -d "${TMPDIR}" ]]; then
        echo "${TMPDIR} already exists; reusing it and leaving it in place"
    else
        sudo mkdir -p "${TMPDIR}"
        TMPDIR_CREATED="True"
    fi
    # Owned by the test user, group staff: BSD filesystems give a new file the
    # group of its PARENT directory rather than the creator's gid, and the
    # same-user TempDir test asserts the created directory has the creating
    # process's gid.
    sudo chown "${TEST_USER}:staff" "${TMPDIR}"
    sudo chmod 1777 "${TMPDIR}"
}

verify() {
    echo "--- Verifying the isolation invariants the tests rely on ---"
    id "${OPENJD_TEST_SUDO_TARGET_USER}"
    id "${OPENJD_TEST_SUDO_DISJOINT_USER}"
    id "${TEST_USER}"
    id -Gn "${TEST_USER}" | tr ' ' '\n' | grep -qx "${OPENJD_TEST_SUDO_SHARED_GROUP}"
    id -Gn "${OPENJD_TEST_SUDO_TARGET_USER}" | tr ' ' '\n' | grep -qx "${OPENJD_TEST_SUDO_SHARED_GROUP}"
    if id -Gn "${OPENJD_TEST_SUDO_DISJOINT_USER}" | tr ' ' '\n' | grep -qx "${OPENJD_TEST_SUDO_SHARED_GROUP}"; then
        echo "disjoint user must not be in the shared group" && exit 1
    fi
    # Cross-user execution works at all
    sudo -u "${OPENJD_TEST_SUDO_TARGET_USER}" -i /usr/bin/true
    sudo -u "${OPENJD_TEST_SUDO_TARGET_USER}" -i python -c \
        'import getpass; print("bare python runs as", getpass.getuser())'
}

if [[ "${KEEP}" != "True" ]]; then
    trap cleanup EXIT
fi

provision
verify

echo "--- Creating the test environment ---"
sudo mkdir -p "${HATCH_DATA_DIR}"
sudo chown "${TEST_USER}" "${HATCH_DATA_DIR}"
# On Python 3.9, virtualenv 21 removed virtualenv.discovery.builtin.propose_interpreters,
# which the hatch version resolvable there still calls; `hatch env create` then fails with
# "Environment `default` is incompatible". Say so rather than letting that message stand on
# its own, since it names neither virtualenv nor the fix.
if ! hatch env create; then
    echo ""
    echo "ERROR: 'hatch env create' failed."
    if python3 -c 'import sys; sys.exit(0 if sys.version_info < (3, 10) else 1)'; then
        echo "On Python 3.9 this is usually virtualenv 21, which dropped an API hatch still"
        echo "uses. Try:  pip install --upgrade hatch 'virtualenv<21'"
    fi
    exit 1
fi
# The target user executes the venv python and reads the test support files, so both
# must be world-readable/traversable.
#
# Scoped to exactly those two, NOT the whole checkout. `chmod -R o+rX .` would make
# every file in the working tree world-readable, including untracked files, .env-style
# files and anything else a developer happens to keep under the repo root, and nothing
# here puts those bits back.
SUPPORT_FILES="test/openjd/sessions_v0/support_files"
chmod -R o+rX "${HATCH_DATA_DIR}"
chmod -R o+rX "${SUPPORT_FILES}"
# Traversal (o+x) only, no read, on the directories leading to the support files.
chmod o+x . test test/openjd test/openjd/sessions_v0

echo "--- Which interpreter the setsid shim resolves to ---"
# NOTE: no braces in this inline script -- hatch run applies its own {...}
# template substitution to the arguments it receives.
hatch run python -c "
from openjd.sessions._subprocess import _macos_shim_interpreter, _MACOS_FALLBACK_SHIM_INTERPRETER
picked = _macos_shim_interpreter()
branch = 'FALLBACK' if picked == _MACOS_FALLBACK_SHIM_INTERPRETER else 'BASE-INTERPRETER'
print('shim interpreter:', picked, '(' + branch + ' branch)')
"

echo "--- Running the cross-user impersonation tests ---"
# -rxX lists (x)failed and (X)passed-unexpectedly tests so the check below can
# tell a real run from one that silently xfailed into a no-op.
LOG_FILE="$(mktemp)"
hatch run test -- \
    test/openjd/sessions_v0/test_subprocess.py \
    test/openjd/sessions_v0/test_tempdir.py \
    --no-cov -rxX "${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}" 2>&1 | tee "${LOG_FILE}"

# The impersonation tests xfail (rather than fail) when the OPENJD_TEST_SUDO_*
# variables are missing, so a broken environment would otherwise look like a pass.
if grep -q "Must define environment vars OPENJD_TEST_SUDO" "${LOG_FILE}"; then
    echo "ERROR: impersonation tests were skipped -- the environment is not being picked up."
    rm -f "${LOG_FILE}"
    exit 1
fi
rm -f "${LOG_FILE}"

echo "--- Cross-user tests passed ---"
