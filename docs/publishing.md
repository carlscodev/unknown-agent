# Publishing & GitHub workflow

How this repository is pushed, released, and kept secure. Written for
maintainers; the user-facing install story lives in the README.

---

## 1. Secure GitHub authentication (HTTPS + PAT)

GitHub personal access tokens replace passwords for Git over HTTPS. The rules
this project follows without exception:

**Never put the token where it persists.**

- ❌ `git remote add origin https://USER:TOKEN@github.com/...` — the token
  lands in `.git/config` in plaintext, shows up in `git remote -v`, and leaks
  into logs and backups.
- ❌ Tokens in shell history: any command with the token as a visible argument
  survives in `~/.bash_history`.
- ❌ Tokens in source, `.env.example`, CI files, or commit messages.

**Do one of these instead (best first):**

```bash
# a) Git credential manager / helper: Git prompts once, the helper stores it
#    encrypted (libsecret on Linux, Credential Manager on Windows).
git config --global credential.helper libsecret   # Debian/Ubuntu: git-credential-libsecret
git push -u origin main                           # username: your login; password: paste the PAT

# b) gh CLI: authenticates once, keeps the token in the system keychain.
gh auth login          # choose HTTPS, paste the token when asked
git push -u origin main

# c) Ephemeral, scriptable, nothing persisted: a one-shot credential helper fed
#    by an environment variable that you set WITHOUT putting it in history
#    (read -s keeps it off screen and out of history):
read -rs GITHUB_TOKEN && export GITHUB_TOKEN
git -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f' \
    push -u origin main
unset GITHUB_TOKEN
```

Pattern (c) is what automation should use: the token exists only in the
process environment, the remote URL stays clean
(`https://github.com/OWNER/REPO.git`), and nothing is written to disk.

**Token hygiene**

- Use *fine-grained* PATs scoped to the single repository, `contents: write`
  only.
- Set a short expiry; rotate rather than extend.
- If a token is ever pasted into a chat, terminal recording, or log: **revoke
  it immediately** at https://github.com/settings/tokens. Treat it as burned.

## 2. First push checklist

```bash
git remote add origin https://github.com/OWNER/REPO.git   # URL contains NO token
git push -u origin main                                   # auth via helper (above)
```

Before the first push, confirm no secrets are tracked:

```bash
git grep -iE "(api[_-]?key|token|secret)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}" -- ':!docs/publishing.md' || echo clean
```

`.gitignore` already excludes `.env`, credentials files, and local state.
`.env.example` ships empty values only.

## 3. Releases

1. Bump `src/unknown_agent/version.py` (single source of truth).
2. Update `CHANGELOG.md`.
3. Tag and push: `git tag v1.1.0 && git push origin v1.1.0`.
4. The `release.yml` workflow: verifies tag == version, runs tests, builds
   sdist+wheel, checksums every asset (**including `install.sh`**) into
   `SHA256SUMS`, and attaches everything to a GitHub Release.

Users can then verify the installer before executing it:

```bash
curl -fsSLO https://github.com/OWNER/REPO/releases/latest/download/install.sh
curl -fsSLO https://github.com/OWNER/REPO/releases/latest/download/SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing && sh install.sh
```

## 4. PyPI (later)

The workflow deliberately does not publish to PyPI yet. When the name is
registered, add a `pypi` job using **trusted publishing** (OIDC, no long-lived
API token in secrets) to `release.yml`. Until then, all install paths use
`git+https://github.com/OWNER/REPO`, and `install.sh --from-pypi` is the
forward-compatible switch.

## 5. Placeholder policy

`OWNER/REPO` appears in README, install.sh defaults, and unit files. On first
publish, replace it once:

```bash
git grep -l "OWNER/REPO" | xargs sed -i 's#OWNER/REPO#youruser/unknown-agent#g'
```
