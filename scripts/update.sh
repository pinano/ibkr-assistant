#!/bin/bash
set -e

TARGET_TAG=$1

echo "Fetching latest tags from remote repository..."
git fetch --tags --quiet

if [ -n "$TARGET_TAG" ]; then
    # Add 'v' prefix if missing
    if ! [[ "$TARGET_TAG" == v* ]]; then
        TARGET_TAG="v$TARGET_TAG"
    fi
    if ! git show-ref --tags --verify --quiet "refs/tags/$TARGET_TAG"; then
        echo "Error: Tag '$TARGET_TAG' does not exist in the repository."
        exit 1
    fi
    LATEST_TAG=$TARGET_TAG
else
    # Find the latest version tag across the whole repo
    LATEST_TAG=$(git tag -l --sort=-v:refname | head -n 1)
fi

if [ -z "$LATEST_TAG" ]; then
    echo "Error: No release tags found in the repository."
    exit 1
fi

# Find current checked out tag
CURRENT_TAG=$(git describe --tags --exact-match 2>/dev/null || true)

if [ "$CURRENT_TAG" == "$LATEST_TAG" ]; then
    echo "Status: You are already running the latest release ($LATEST_TAG)."
    exit 0
fi

echo "Update Available: $LATEST_TAG (Current: ${CURRENT_TAG:-not on a tag})"
read -p "Do you want to upgrade the codebase to $LATEST_TAG now? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Upgrade cancelled."
    exit 0
fi

git checkout "$LATEST_TAG" --quiet
echo "Success: Codebase updated to $LATEST_TAG."
echo ""
read -p "Do you want to apply these changes now? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Rebuilding and starting..."
    make rebuild
    make start
    echo "Stack updated successfully!"
else
    echo "Note: To apply these changes manually later, run:"
    echo "  make rebuild"
    echo "  make start"
fi
