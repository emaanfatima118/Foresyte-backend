# Push ForeSyte Backend to a new repository

## Step 1: Create the new repo on GitHub

1. Go to **https://github.com/new**
2. Set **Repository name** (e.g. `ForeSyte_Backend` or `foresyte-backend`)
3. Set **Visibility** (Public or Private)
4. **Do not** add a README, .gitignore, or license (you already have these locally)
5. Click **Create repository**

## Step 2: Point this folder to the new repo and push

Open PowerShell in this folder (`ForeSyte_Backend`), then run:

### Option A – Keep full history (use only if you already ran filter-branch and removed large files)

```powershell
git remote set-url origin https://github.com/YOUR_USERNAME/YOUR_NEW_REPO_NAME.git
git push -u origin main
```

### Option B – Fresh start (one commit, no old history) – recommended

This pushes only your current code as a single commit. No large files, no old history.

```powershell
# Create a new branch with no history (orphan)
git checkout --orphan new-main

# Stage everything that isn't ignored (large files are in .gitignore now)
git add -A
git commit -m "Initial commit: ForeSyte Backend"

# Rename branch to main and remove old history
git branch -D main
git branch -m main

# Point to your NEW repo
git remote set-url origin https://github.com/emaanfatima118/Foresyte-backend.git

# Push (no force needed; new repo has no history)
git push -u origin main
```

Replace `YOUR_USERNAME` and `YOUR_NEW_REPO_NAME` with your GitHub username and the new repo name.

## Step 3: Optional – remove backup refs from filter-branch

If you used filter-branch before and want to clean up:

```powershell
rm -r .git/refs/original 2>$null; git reflog expire --expire=now --all; git gc --prune=now --aggressive
```

---

**Note:** Your `.gitignore` already ignores `model-train/**/weights/*.pt` and `model-train/**/runs/`, so those files will stay only on your machine and won’t be pushed.
