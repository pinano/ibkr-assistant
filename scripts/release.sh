#!/bin/bash
set -e

# Calculate current date version
TODAY=$(date +"%Y.%m.%d")
VERSION=$TODAY

# Guard 1: Uncommitted changes
if ! git diff-index --quiet HEAD --; then
    echo "Error: You have uncommitted changes. Please commit or stash them before running make release."
    exit 1
fi

# Guard 2: No new commits since last tag
PREV_TAG=$(git describe --tags --abbrev=0 2>/dev/null || true)
if [ -n "$PREV_TAG" ]; then
    NEW_COMMITS=$(git log ${PREV_TAG}..HEAD --oneline)
    if [ -z "$NEW_COMMITS" ]; then
        echo "Error: No new commits since last release ($PREV_TAG). Aborting."
        exit 1
    fi
fi

# Guard 3: Must be on main branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "Error: You must be on the 'main' branch to create a release. Currently on: $CURRENT_BRANCH"
    exit 1
fi

# Check if today's version already exists
if git rev-parse "v$VERSION" >/dev/null 2>&1; then
    # We need a micro version, e.g. 2026.05.23.1
    LATEST_TAG=$(git tag -l "v$VERSION*" --sort=-v:refname | head -n 1)
    if [ "$LATEST_TAG" == "v$VERSION" ]; then
        VERSION="$VERSION.1"
    else
        # Extract the micro version and increment it
        MICRO=$(echo "$LATEST_TAG" | awk -F. '{print $4}')
        VERSION="$TODAY.$((MICRO + 1))"
    fi
fi

echo "Creating new release: $VERSION"

# 1. Update VERSION file
echo "$VERSION" > VERSION

# 2. Generate Changelog
TMP_CHANGELOG=$(mktemp)
echo "## v$VERSION ($(date +"%Y-%m-%d"))" > "$TMP_CHANGELOG"
echo "" >> "$TMP_CHANGELOG"

# Get the previous tag to calculate the log. If no previous tag, get all history.
PREV_TAG=$(git describe --tags --abbrev=0 2>/dev/null || true)
if [ -z "$PREV_TAG" ]; then
    git log --pretty=format:"- %s (%h)" >> "$TMP_CHANGELOG"
else
    git log ${PREV_TAG}..HEAD --pretty=format:"- %s (%h)" >> "$TMP_CHANGELOG"
fi

echo "" >> "$TMP_CHANGELOG"
echo "" >> "$TMP_CHANGELOG"

# If CHANGELOG.md exists, prepend the new log
if [ -f CHANGELOG.md ]; then
    cat CHANGELOG.md >> "$TMP_CHANGELOG"
fi
mv "$TMP_CHANGELOG" CHANGELOG.md

# 3. Commit and Tag
git add VERSION CHANGELOG.md
git commit -m "chore: release v$VERSION"
git tag -a "v$VERSION" -m "Release v$VERSION"

echo "Release v$VERSION created successfully!"
echo "Run 'git push origin main --tags' to publish it."
