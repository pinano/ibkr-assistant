#!/bin/bash
set -e

echo "Fetching latest tags from remote repository..."
git fetch --tags --quiet

echo "Recent versions:"
TAGS=($(git tag -l --sort=-v:refname | head -n 10))

if [ ${#TAGS[@]} -eq 0 ]; then
    echo "Error: No release tags found in the repository."
    exit 1
fi

# Find current checked out tag
CURRENT_TAG=$(git describe --tags --exact-match 2>/dev/null || true)

for i in "${!TAGS[@]}"; do
    if [ "${TAGS[$i]}" == "$CURRENT_TAG" ]; then
        echo "  $((i+1))) ${TAGS[$i]} (Current)"
    else
        echo "  $((i+1))) ${TAGS[$i]}"
    fi
done

echo ""
read -p "Select a version to rollback/update to (1-${#TAGS[@]}, or press Enter to cancel): " choice

if [[ -z "$choice" ]]; then
    echo "Cancelled."
    exit 0
fi

if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#TAGS[@]}" ]; then
    echo "Invalid choice. Aborting."
    exit 1
fi

SELECTED_TAG=${TAGS[$((choice-1))]}

if [ "$SELECTED_TAG" == "$CURRENT_TAG" ]; then
    echo "Status: You are already running $SELECTED_TAG."
    exit 0
fi

echo ""
echo "Selected Version: $SELECTED_TAG"
read -p "Are you sure you want to change the codebase to $SELECTED_TAG? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Operation cancelled."
    exit 0
fi

git checkout "$SELECTED_TAG" --quiet
echo "Success: Codebase changed to $SELECTED_TAG."
echo ""
read -p "Do you want to apply these changes and start the stack now? [y/N] " -n 1 -r
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
