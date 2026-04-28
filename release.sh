#!/usr/bin/env bash
# Tag and push a release. The version comes from the git tag (hatch-vcs reads
# it for the wheel; CI patches plugin.cfg from GITHUB_REF_NAME).
#
# Usage:
#   ./release.sh                  # bump patch from latest tag (default)
#   ./release.sh patch            # explicit patch bump
#   ./release.sh minor            # minor bump (X.Y+1.0)
#   ./release.sh major            # major bump (X+1.0.0)
#   ./release.sh 0.2.0            # explicit version
#   ./release.sh --dry-run [...]  # print plan, do not tag/push
#
# Preflight: must be on main, clean working tree, in sync with origin/main.
# Refusing on stale main is intentional — tags point at HEAD, and a stale
# HEAD is exactly the trap that triggered this script's existence.

set -euo pipefail

cd "$(dirname "$0")"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi
ARG="${1:-patch}"

die() { echo "release.sh: $*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------

[[ "$(git symbolic-ref --short HEAD 2>/dev/null)" == "main" ]] \
  || die "not on main"

[[ -z "$(git status --porcelain)" ]] \
  || die "working tree dirty — commit or stash first"

git fetch origin --quiet --tags
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse origin/main)"
[[ "$LOCAL" == "$REMOTE" ]] \
  || die "local main ($LOCAL) ≠ origin/main ($REMOTE) — pull/push first"

# --- compute new version -----------------------------------------------------

LATEST_TAG="$(git tag -l 'v*' --sort=-v:refname | head -1)"
LATEST="${LATEST_TAG#v}"
LATEST="${LATEST:-0.0.0}"

if [[ ! "$LATEST" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  die "latest tag '$LATEST_TAG' is not semver X.Y.Z"
fi
IFS=. read -r MAJ MIN PAT <<<"$LATEST"

case "$ARG" in
  major) NEW="$((MAJ+1)).0.0" ;;
  minor) NEW="${MAJ}.$((MIN+1)).0" ;;
  patch) NEW="${MAJ}.${MIN}.$((PAT+1))" ;;
  [0-9]*.[0-9]*.[0-9]*) NEW="$ARG" ;;
  *) die "unknown bump: $ARG (use major|minor|patch|X.Y.Z)" ;;
esac

TAG="v$NEW"

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null \
   || git ls-remote --exit-code --tags origin "$TAG" >/dev/null 2>&1; then
  die "tag $TAG already exists locally or on origin"
fi

# --- plan ---------------------------------------------------------------------

cat <<EOF
HEAD:        $LOCAL
latest tag:  ${LATEST_TAG:-<none>}
bump:        $ARG
new tag:     $TAG
will push:   git push origin $TAG  →  triggers release.yml
EOF

if (( DRY_RUN )); then
  echo "(dry run, not tagging)"
  exit 0
fi

read -r -p "Proceed? [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "aborted"; exit 0; }

# --- act ----------------------------------------------------------------------

git tag "$TAG"
git push origin "$TAG"
echo "pushed $TAG"

if command -v gh >/dev/null 2>&1; then
  echo "waiting 3s for GitHub to register the run..."
  sleep 3
  RUN_ID="$(gh run list --workflow release.yml --limit 1 --json databaseId -q '.[0].databaseId')"
  if [[ -n "$RUN_ID" ]]; then
    echo "watching run $RUN_ID..."
    gh run watch --exit-status "$RUN_ID" || die "release workflow failed — see logs"
    echo "release $TAG completed."
  else
    echo "no run found yet; check Actions tab manually."
  fi
else
  echo "gh CLI not installed; check Actions tab manually."
fi
