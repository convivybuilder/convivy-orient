---
id: deck/runbooks/ci-runner-own-account
kind: runbook
project: deck
title: Move Deck's CI runner to its own macOS account
status: current
audience: [team]
applies_to: [CLAUDE.md, .github/workflows]
last_reviewed: 2026-09-27
authors: [operator]
topics: [ci, security, self-hosted-runner, credentials]
---

# Move Deck's CI runner to its own macOS account

## Why

`com.convivy.deck-ci-runner` is a LaunchAgent in the operator's login session, so every
workflow step runs with their file access. Nothing in Deck's CI needs it: the one
step that authenticates uses `${{ github.token }}`, the ephemeral token GitHub
issues per job. The access is ambient rather than required, which is what makes
it worth removing.

Filed as `issue_01M2MARFKRAW0XMMPK9CERMFXY` after the Library inventoried what
acts as the operator on the fleet host. Deck's own `CLAUDE.md` names this as the first of
three steps and ties them to a collaborator being added or the repo becoming
public; the operator asked on 2026-09-15 for the account move regardless, because they want
their credentials out of reach of CI.

**This is not the machine-wide credentials epic.** Every other agent on the fleet
host still runs as the operator. This changes one job.

## Two machines

The runner moves off the mini and onto the Studio (`macstudio`) at the same time as it
moves into its own account. Every step below is headed with where it runs and as
whom:

- **Studio, as the operator**: The operator's own login on the Studio, at its screen or over SSH.
- **Studio, as `deck-runner`**: a shell as the new account, opened from the operator's
  session with `sudo -iu deck-runner`. The account never logs in to the GUI.
- **Mini, as the operator**: The operator's login on the mini, at its screen or over SSH.

The order is chosen so that CI always has a working runner and the new runner
never takes a job before its separation is checked:

1. Steps 1 to 5 prepare and check the Studio runner. The mini's runner keeps
   taking every job.
2. Step 6 starts the Studio runner. From here until step 7, a job can land on
   either runner. That is safe only because step 5 already passed.
3. Step 7 stops the mini's runner, without unregistering it, and proves the
   Studio runner with a real job. If that fails, one command restarts the mini's.
4. Step 8 removes the mini's runner for good.

## What the runner actually needs

Swift, full Xcode with its Metal toolchain, `gh`, and a checkout. All are
machine-wide. The Command Line Tools alone are not enough: SwiftTerm compiles a
Metal shader (`Shaders.metal`), and the build fails with `unable to spawn process
'metal'` without Xcode's toolchain. `ci.yml` also runs `xcodebuild -version`.
Nothing in `.github/` or `Package.swift` reads a path under the operator's home.

The runner runs as a **system LaunchDaemon with `UserName` set to `deck-runner`**,
so it starts at boot with no login for that account. Thirteen of Deck's test
files open AppKit windows, and whether they can do that without a GUI session of
their own was checked on the Studio on 2026-09-27 for one of them.
`CockpitRevealRenderTests`, which opens an `NSWindow`, hosts a SwiftUI view in
`NSHostingView` and draws it with `cacheDisplay`, ran as such a daemon with
`deck-runner` not logged in: 8 tests, 0 failures, the same result as in the operator's own
session. The other twelve files first run this way at step 7.

**That run had the operator's account logged in at the console, and the design depends on
it.** The Studio logs the operator's account in automatically at boot, a setting the steward
made on 2026-09-27 so the machine recovers unattended; its 10:11 MST reboot that
day had the console session up 12 seconds after boot. Whether the window tests
pass with nobody at the console, at the login window, was not tested. Before
step 6, confirm auto-login is on, as the operator:

```bash
sysadminctl -autologin status   # expect: Automatic login user: <operator>
```

The steward's recovery check, `studio-migration/macstudio-recovery-check` in the
substrate repo, tests the same thing after every boot and fails a boot whose
console login came more than 120 seconds late. **If auto-login is ever turned
off, the runner still starts at boot, but treat red window tests as this cause
until shown otherwise.**

**The console session must also stay unlocked, so macstudio's screen lock is off.**
The AppKit tests' windows draw on the console session, and a locked console
leaves them occluded. On 2026-09-29 that failed
`CockpitRevealRenderTests.testARevealScrollsAChildBelowTheFoldIntoView` on
every run, locally and in CI, including on a commit that had passed CI two days
earlier. The other render tests kept passing, so it read as a code regression.
Waking the display did not fix it; unlocking the console did (2848 tests, 0
failures). The operator turned the lock off in System Settings that day. Check it with
`sysadminctl -screenLock status`, which must print `screenLock is off`, and
re-check after any macOS update. The display may still sleep.

## Before you start

- About 30 minutes at the Studio with an admin password, plus two steps on the
  mini (7 and 8). The Xcode download in step 3 adds its own time and needs the operator's
  Apple ID.
- Deck's own CLAUDE.md section "CI runs as the operator — an accepted risk, named" is the
  record this changes; update it in the same PR as the verification step below.

## Steps

### 1. Close the operator's home to other accounts — Studio, as the operator

The operator's home is `drwxr-x---+`, group-readable by `staff`, and a standard account
joins `staff` on creation. Anything group- or world-readable under it would be
readable by `deck-runner`. A sweep on the Studio on 2026-09-27 found 34 such files,
among them `~/Library/Keychains/login.keychain-db`, `~/.library/library.db` (the
merge gate's trust root), `~/.config/convivy/session-agent.env`, and the `.env`
files of several project checkouts. The migration copied files from the mini with
their modes, so the Studio carries every exposure the mini had.

Tightening files one at a time misses whatever is added next. Close the home
instead:

```bash
chmod 700 $OPERATOR_HOME
ls -led $OPERATOR_HOME
```

Expect `drwx------+`, and under it exactly one access-list entry,
`0: group:everyone deny delete`. That entry is the macOS default; it only denies,
so it grants nothing that `chmod` has taken away. **Any `allow` entry here means
the home is still open**: remove it with `chmod -a# <n> $OPERATOR_HOME` before
going on. Step 5 checks the result from the other side.

This changes one thing beyond CI: other accounts can no longer browse
`~/Public`. Nothing in the fleet relies on that.

### 2. Create the account — Studio, as the operator

A standard account, not an administrator:

```bash
sudo sysadminctl -addUser deck-runner -fullName "Deck CI Runner" -password -
```

`-password -` prompts, so the password never lands in shell history. Then confirm
it is not an admin:

```bash
dscl . -read /Groups/admin GroupMembership
```

`deck-runner` must not appear.

### 3. Install Xcode and its Metal toolchain — Studio, as the operator

Install Xcode with `xcodes`, in a real terminal, since it prompts for the operator's Apple
ID and a two-factor code:

```bash
xcodes install 27.0 --experimental-unxip
```

Then select it, accept the licence and fetch the Metal toolchain, which Xcode
downloads separately:

```bash
sudo xcode-select -s /Applications/Xcode-27.0.0.app
sudo xcodebuild -license accept
sudo xcodebuild -runFirstLaunch
xcodebuild -downloadComponent MetalToolchain
```

`xcode-select` is machine-wide, so `deck-runner` gets the same toolchain. Check it:

```bash
xcode-select -p          # expect: /Applications/Xcode-27.0.0.app/Contents/Developer
xcrun metal --version    # expect: Apple metal version ...
```

`xcrun -f metal` finds a path even before the toolchain is downloaded, so it is
not a check; `metal --version` is.

### 4. Install and register the runner — Studio, as `deck-runner` (one command as the operator)

Open a shell as the new account:

```bash
sudo -iu deck-runner
```

In it:

```bash
mkdir -p ~/.local/share/actions-runner-deck && cd ~/.local/share/actions-runner-deck
url=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
      | grep -o 'https://[^"]*osx-arm64[^"]*\.tar\.gz' | head -1)
curl -fsSL -o actions-runner.tar.gz "$url"
```

**Check the tarball against GitHub's published hash before extracting it.** This
archive is about to execute under a new account, and an unverified download is
the one step here that could hand that account to someone else. The release page
at <https://github.com/actions/runner/releases/latest> prints the expected
SHA-256 for each asset; compare it against:

```bash
shasum -a 256 actions-runner.tar.gz
```

Only once they match:

```bash
tar xzf actions-runner.tar.gz && rm actions-runner.tar.gz
```

**Studio, as the operator**, in a second terminal: generate the registration token from
The operator's own session, since it needs their GitHub auth:

```bash
gh api -X POST repos/convivy/convivy-deck/actions/runners/registration-token --jq .token
```

Then, back in the `deck-runner` shell, run `config.sh` without `--token` and
without `--unattended`, and paste the token when it prompts for it. A token passed
as an argument would sit in shell history and in the process list, where any
account can read it with `ps`; at the prompt it goes to neither. Accept the
default at any other prompt (the runner group).

```bash
./config.sh --url https://github.com/convivy/convivy-deck \
  --name deck-ci-macstudio --labels self-hosted,macOS,deck --work _work
```

**The labels must match `runs-on: [self-hosted, macOS, deck]` exactly** or no job
will ever pick it up, and a mismatched label reads as a hung queue rather than as
a misconfiguration.

Registering does not start the runner, so it takes no job yet. It shows as
`offline` on GitHub until step 6.

### 5. Check the separation before the runner starts — Studio, as `deck-runner`

In the `sudo -iu deck-runner` shell:

```bash
ls $OPERATOR_HOME                                     # expect: Permission denied
cat $OPERATOR_HOME/.config/gh/hosts.yml              # expect: Permission denied
ls $OPERATOR_HOME/.ssh                                # expect: Permission denied
cat $OPERATOR_HOME/.claude/settings.json              # expect: Permission denied
cat $OPERATOR_HOME/.config/convivy/llm-gateway.env    # expect: Permission denied
```

The first line is the one step 1 turns into a denial; the other four are the
CI-relevant paths, tested rather than assumed. **A command that prints anything
but a denial means the separation failed.** Stop and fix it before step 6.

**The Keychain, which file permissions say nothing about.** Confirm an item known
to be in the operator's login keychain is unreachable:

```bash
security find-generic-password -s <item-name>   # expect: failure
```

A second account cannot unlock the operator's login keychain. This is what turns that from
a belief into an observation.

### 6. Start the runner — Studio, as the operator

A LaunchDaemon runs as root unless its plist says otherwise. **`UserName` is the
one key that keeps CI away from root**, and a job can start within seconds of
the runner coming up, so check the file before loading it rather than the
process after.

Write the plist exactly as below:

```bash
sudo tee /Library/LaunchDaemons/com.convivy.deck-ci-runner.plist >/dev/null <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.convivy.deck-ci-runner</string>
  <key>UserName</key><string>deck-runner</string>
  <key>GroupName</key><string>staff</string>
  <key>WorkingDirectory</key><string>$RUNNER_HOME/.local/share/actions-runner-deck</string>
  <key>ProgramArguments</key>
  <array><string>$RUNNER_HOME/.local/share/actions-runner-deck/run.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$RUNNER_HOME</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$RUNNER_HOME/Library/Logs/deck-ci-runner.log</string>
  <key>StandardErrorPath</key><string>$RUNNER_HOME/Library/Logs/deck-ci-runner.log</string>
</dict>
</plist>
EOF
sudo chown root:wheel /Library/LaunchDaemons/com.convivy.deck-ci-runner.plist
sudo chmod 644 /Library/LaunchDaemons/com.convivy.deck-ci-runner.plist
```

A daemon gets no `HOME` or `PATH` from a login, which is why both are set. The
log sits in `deck-runner`'s home because that account cannot write the operator's.

**Before loading it**, check the file:

```bash
plutil -lint /Library/LaunchDaemons/com.convivy.deck-ci-runner.plist             # expect: OK
plutil -extract UserName raw /Library/LaunchDaemons/com.convivy.deck-ci-runner.plist   # expect: deck-runner
```

**Anything other than `OK` and `deck-runner` means stop.** Do not load the file
until both lines read as expected.

Then load it and check the running process:

```bash
sudo launchctl bootstrap system /Library/LaunchDaemons/com.convivy.deck-ci-runner.plist
sudo launchctl print system/com.convivy.deck-ci-runner | grep -E '^\s*(state|pid) ='   # expect: state = running, and a pid
ps -o user= -p "$(pgrep -f actions-runner-deck/bin/Runner.Listener)"                # expect: deck-runner
```

**If `ps` prints anything but `deck-runner`, unload the runner at once**, then
find the cause before going on:

```bash
sudo launchctl bootout system/com.convivy.deck-ci-runner
```

### 7. Prove the Studio runner with a real job — Mini, as the operator, then anywhere

Stop the mini's runner, leaving it registered so it can come back:

```bash
launchctl bootout gui/$(id -u)/com.convivy.deck-ci-runner
```

Confirm only the Studio runner is online:

```bash
gh api repos/convivy/convivy-deck/actions/runners --jq '.runners[] | "\(.name) \(.status)"'
```

That should print `deck-ci-macstudio online` and `deck-ci-mini offline`, in either
order.

Then open a trivial PR, mark it ready, and confirm `build-test` runs on
`deck-ci-macstudio` and goes green. The job must not be waiting on a runner: a label
typo looks exactly like a busy queue. This is the first run of the full suite with
no GUI session for `deck-runner`; a failure in a test that opens a window points
at that, not at the change under test.

**If it does not go green**, restart the mini's runner and CI is back as it was:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.convivy.deck-ci-runner.plist
```

### 8. Remove the mini's runner — Mini, as the operator

Only once step 7's PR is green on `deck-ci-macstudio`:

```bash
cd ~/.local/share/actions-runner-deck
./config.sh remove --token "$(gh api -X POST repos/convivy/convivy-deck/actions/runners/remove-token --jq .token)"
rm ~/Library/LaunchAgents/com.convivy.deck-ci-runner.plist
cd ~ && rm -rf ~/.local/share/actions-runner-deck
```

Confirm GitHub agrees, rather than trusting the command:

```bash
gh api repos/convivy/convivy-deck/actions/runners --jq '.runners[] | "\(.name) \(.status)"'
```

That should print exactly `deck-ci-macstudio online`.

## Then make the separation self-verifying

Add a step to `.github/workflows` that fails the job if the runner can read the operator's
gh token, beside the existing privacy and lane guards, which run before any PR
code is checked out:

```yaml
      - name: Runner must not be able to read the operator's credentials
        run: |
          if [ -r $OPERATOR_HOME/.config/gh/hosts.yml ]; then
            echo "::error::This runner can read the operator's GitHub token — it is running as the wrong account." >&2
            exit 1
          fi
          echo "credential separation: clean"
```

Without it, nothing notices if the runner is ever reinstalled under the operator again,
which is how the original arrangement survived unnoticed for months.

**Run it against the OLD arrangement first and watch it fail**, before trusting a
pass on the new one. A check whose passing and failing states produce the same
evidence is not a check.

## What this does not fix

- **The launcher.** `Deck.app`'s bundle executable fetches `main` and compiles it
  on a Dock click, as the operator, on the laptop, with none of CI's guards in front of it.
  Deck's `CLAUDE.md` names this third of three; it is unchanged by this runbook.
- **The guard that cannot protect itself.** A `pull_request` event runs the
  workflow from the PR's head commit, so a PR that deletes the privacy assertion
  runs without it. Only write-access control covers that; branch protection
  requiring review of any `.github/` change is the fix, and it is not done.
- **Every other agent on the fleet host still runs as the operator.** That is the
  machine-wide credentials epic, not this.
