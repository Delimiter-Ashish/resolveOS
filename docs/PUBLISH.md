# Publish ResolveOS to GitHub

Target: https://github.com/Delimiter-Ashish/resolveOS

## 1. Merge the kit safely

On SCC, place `ResolveOS_GitHub_Kit.zip` in `/projectnb/medaihack/team4/`. Back up the README **outside** the project before overwriting it:

```bash
cd /projectnb/medaihack/team4/ResolveOS
cp README.md ../ResolveOS_README_before_showcase.md
unzip -o ../ResolveOS_GitHub_Kit.zip
bash tools/merge_public_gitignore.sh
```

The ZIP contains documentation, `docs/assets`, and `tools` only. It is not the backend source code. Your existing source files remain in place.

## 2. Check secrets before publishing

You have previously displayed API keys in screenshots or chat. **Rotate those secrets and invalidate the originals** before any public hosting. Do not paste any keys into the README or screenshots.

```bash
git status --short
ls -lah .env .env.keys 2>/dev/null || true
```

Do not publish `runtime/`, `results/`, Python virtual environments, model weights, `.env`, `.env.keys` or database storage. Excluded files should never be force-added.

## 3. Initialize and connect the GitHub remote

```bash
cd /projectnb/medaihack/team4/ResolveOS
if [ ! -d .git ]; then git init; fi
git branch -M main
git remote -v
# Only if no origin is configured:
git remote add origin git@github.com:Delimiter-Ashish/resolveOS.git
```

If `origin` already exists, compare it against the target; do not blindly replace a remote you are using for other work.

## 4. Stage, inspect, commit, push

```bash
git add .
python3 tools/public_preflight.py
git diff --cached --stat
git diff --cached --name-only
git status --short
```

Review the staged file list carefully and verify there are **no secrets**, hidden local files, model weights, logs, or customer information. If safe:

```bash
git commit -m "feat: publish ResolveOS v5 with Mission Control showcase"
git push -u origin main
```

For the SCC-to-GitHub SSH workflow, verify your authentication with `ssh -T git@github.com` (the greeting may say that shell access is unavailable; that is normal). If you already have an SSH `origin`, leave it unchanged. If a different HTTPS `origin` points to this same repo, you may run `git remote set-url origin git@github.com:Delimiter-Ashish/resolveOS.git`. Do not paste private SSH keys or tokens in chat. Never force-push over unfamiliar history.

## If the remote is not empty

Do **not** force push. Fetch and inspect existing history. A safe approach is to clone the remote into a different directory, copy only vetted project files over, commit, then push; or merge histories after reviewing them.

## After pushing

Open the repository, check that the README renders and images load. Configure topics such as `agentic-ai`, `langgraph`, `fastapi`, `postgresql`, `llmops`, and `ai-agents`. Next prepare hosting in a separate environment with **new credentials**, proper login, least privilege, HTTPS, data isolation and rate limits.
