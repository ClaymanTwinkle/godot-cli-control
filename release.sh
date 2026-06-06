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
#   ./release.sh --roll-changelog [...]  # CHANGELOG [Unreleased] 非空时：
#                                 # 自动滚动为新版本段、commit、push，再打 tag
#
# Preflight: must be on main, clean working tree, in sync with origin/main.
# Refusing on stale main is intentional — tags point at HEAD, and a stale
# HEAD is exactly the trap that triggered this script's existence.
#
# CHANGELOG gate (#140): [Unreleased] 段非空时拒绝打 tag——发版必须先把
# 变更归档到版本段，否则 CHANGELOG 与 tag 脱节（0.2.x 曾欠账 12 个版本）。
# 加 --roll-changelog 让脚本代劳：把 [Unreleased] 重命名为新版本段（含日期）、
# 在其上重开空的 [Unreleased]、commit 并 push 到 main，然后再打 tag。

set -euo pipefail

cd "$(dirname "$0")"

CHANGELOG="addons/godot_cli_control/CHANGELOG.md"

DRY_RUN=0
ROLL_CHANGELOG=0
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --roll-changelog) ROLL_CHANGELOG=1 ;;
    *) echo "release.sh: unknown flag: $1" >&2; exit 1 ;;
  esac
  shift
done
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

# --- changelog gate (#140) -----------------------------------------------------
# [Unreleased] 段（到下一个 '## [' 为止）剔除空行后还有内容 → 必须先归档。

[[ -f "$CHANGELOG" ]] || die "$CHANGELOG not found"

UNRELEASED_BODY="$(awk '/^## \[Unreleased\]/{f=1; next} /^## \[/{f=0} f' "$CHANGELOG" \
  | grep -v '^[[:space:]]*$' || true)"

CHANGELOG_PLAN="empty — nothing to roll"
if [[ -n "$UNRELEASED_BODY" ]]; then
  if (( ROLL_CHANGELOG )); then
    CHANGELOG_PLAN="roll [Unreleased] → [$NEW] - $(date +%F), commit & push, then tag"
  else
    echo "release.sh: CHANGELOG [Unreleased] 段非空：" >&2
    echo "$UNRELEASED_BODY" | head -5 | sed 's/^/    /' >&2
    die "先把 [Unreleased] 归档为版本段（或加 --roll-changelog 让脚本代劳）"
  fi
fi

# --- plan ---------------------------------------------------------------------

cat <<EOF
HEAD:        $LOCAL
latest tag:  ${LATEST_TAG:-<none>}
bump:        $ARG
new tag:     $TAG
changelog:   $CHANGELOG_PLAN
will push:   git push origin $TAG  →  triggers release.yml
EOF

if (( DRY_RUN )); then
  echo "(dry run, not tagging)"
  exit 0
fi

# --- act ----------------------------------------------------------------------

if [[ -n "$UNRELEASED_BODY" ]] && (( ROLL_CHANGELOG )); then
  awk -v new_header="## [$NEW] - $(date +%F)" '
    /^## \[Unreleased\]$/ && !done { print; print ""; print new_header; done=1; next }
    { print }
  ' "$CHANGELOG" > "$CHANGELOG.tmp"
  mv "$CHANGELOG.tmp" "$CHANGELOG"
  git add "$CHANGELOG"
  git commit -m "docs(changelog): roll [Unreleased] → v$NEW"
  git push origin main
  echo "rolled changelog: [Unreleased] → [$NEW]"
fi

git tag "$TAG"
git push origin "$TAG"
echo "pushed $TAG — release.yml will run on GitHub Actions."
