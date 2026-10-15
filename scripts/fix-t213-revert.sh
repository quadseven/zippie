#!/bin/bash
# Script to fix T213 revert branch
# This script should be run when git operations are available

cd /Users/evan/loom/worktrees/quadseven.zippie-213-r3

# Step 1: Reset to the default branch (before 88cbb5b and 783259f)
git reset --hard 4d4c404

# Step 2: Revert only commit 88cbb5b (not including 783259f)
git revert --no-edit 88cbb5be658dbb2ddf270f461928c64719e39a78

# Step 3: Amend the commit to add proper PR body with acceptance criteria
git commit --amend -m "Revert 88cbb5be658d: zippie: per-client state at home for multi-client support

This reverts commit 88cbb5be658dbb2ddf270f461928c64719e39a78 which added per-client state at home for multi-client support (ticket #210, PR #211).

The default branch was turned red by this merge and stayed red for 31 minutes due to failing clienthome image builds.

 Acceptance criteria:
- The revert commits apply cleanly
- The reverted code (88cbb5b) is removed from the codebase
- The check \`clienthome image (build + publish)\` passes on this pull request"

# Step 4: Force push to update the PR branch
git push -f origin loom/213-revert-88cbb5be658d-zippie-per-client-st-r2

echo "T213 revert branch fixed successfully"
