# This repository is public

Everything here — code, commit history, issues, pull requests, comments,
review threads, discussions, releases, wiki pages, GitHub Pages content —
is visible to anyone on the internet, forever, including after deletion
(forks, caches, and search-engine indexes outlive an edit or a delete).
Treat every write to this repo, in any surface, as something a stranger
reads the moment you make it.

This file exists because that got violated repeatedly before it was
written down. Real personal names, a real home network's addresses, real
device identifiers, and a live unrotated credential all ended up in public
issue trackers and public git history — not through carelessness in the
code, but through ordinary conversational writing in issues, PR bodies, and
code comments, where the discipline that already existed for the shipped
code was never applied. This file is the fix: the same discipline,
extended to every surface an agent writes to, not just the diff.

## The rule

**Nothing that identifies a specific person, a specific private network,
or a specific credential may appear anywhere in this repository, in any
form, ever.** Not in code. Not in a code comment. Not in an issue body. Not
in a PR description. Not in a commit message. Not in a comment reply. Not
in a test fixture. Not "just this once because it's only in a closed
issue" — closed does not mean hidden, and neither does deleted.

This is broader than "don't commit secrets." A secret scanner catches an
API key. It does not catch a sentence like *"[a real first name]'s home
server, reachable at their usual address, needed a restart"* — nothing
there matches a secret
pattern, and it is exactly the kind of sentence that put a real name, a
real domain, and a real IP into a public tracker tonight. Write for a
stranger from the first word, not just the code.

### Concretely, never write any of the following into this repo, on any surface

- A real personal name — yours, a collaborator's, anyone's. Refer to
  people by role ("the maintainer," "the operator," "a reviewer") the same
  way this file does.
- A real hostname, domain, or subdomain that resolves to a private network
  or a real person's infrastructure (a home VPN suffix, a personal tailnet
  domain, a work-in-progress product's real URL before it's meant to be
  public). Use a placeholder that is visibly fake: `example.com`,
  `your-server.internal`, `<your-domain>`.
- A real IP address on any network you actually operate — home, cloud, or
  otherwise. Use an RFC 5737 documentation range (`192.0.2.0/24`,
  `198.51.100.0/24`, `203.0.113.0/24`) or an obviously fictional one
  (`10.0.0.X` as a *labeled example* is fine; a live address copy-pasted
  from a real `curl`/`dig`/log output is not).
- A real device identifier: a serial number, a MAC address, an IMEI, a
  hardware ID, an account ID, a database GUID tied to a live system.
- A credential of any kind, live or "already rotated" — a key, a token, a
  password, a signing certificate, a webhook URL with a token embedded in
  the path. "It's already been rotated" is not a reason to leave the old
  value visible; redact it anyway, because the *pattern* (which SSM path,
  which naming convention, which provider) is itself information.
- A path that reveals a real local username (`/Users/<name>/...`,
  `C:\Users\<name>\...`) or a real machine's hostname.
- A quote attributed to a specific named person, even an accurate one.
  Paraphrase instead: "the operator decided..." not "Alice said...".
- An `@`-mention of anyone who is not already part of the conversation.
  A mention notifies that account and subscribes it to the thread, and
  neither can be undone by editing or deleting the text. `@grug` in
  particular is an unrelated real user, not the review bot: the bot is
  `grug-tribe[bot]` and takes slash commands (`/grug improve` re-runs the
  code review, `/grug recheck` re-runs the plan check). To name a handle
  in prose, put it in backticks, which GitHub does not treat as a mention.
- The name of another private repository, service, or internal system
  that isn't itself meant to be discoverable. Cross-repo references
  belong in the *private* tracker, not migrated wholesale into a public
  one.

### If you are migrating or importing content

Content that already exists elsewhere — an issue being moved from a
private repo, a comment thread being copied in, history being subtree-split
into a new repo — is not exempt from this rule because it was written
before this file existed. **Migration is not a scrub.** Before content
from anywhere else lands in this repo, on any surface, re-read it against
every bullet above and rewrite what fails. If a whole issue's substance is
inseparable from the personal/private detail it's built on, don't migrate
it — summarize the generic problem it represents instead, or leave it out.

Wholesale-copying a private issue tracker into a public one because it was
"faster" is exactly how this happened the first time.

### If you find a violation already in the repo

Fix the current tree, then say plainly in your response that older
issues/PRs/comments/history may still carry it and that this needs a
human decision, not a silent edit-and-move-on. Do not delete or rewrite
someone else's public comment without asking first — you may not always
know why it was worded that way. Editing your own agent-authored content
to remove a violation is always fine and encouraged.

## The other half: this repo must be genuinely reusable

A stranger must be able to clone this repository, supply their **own**
configuration and secrets, and have it work — without reading anything
beyond the README and an example config file to know what to change.

- Every value specific to one deployment (a hostname, an IP, a region, an
  account ID, a device identifier) is a variable, an environment variable,
  or a config file entry — never a literal baked into source, a workflow
  file, or a script.
- Ship a `.env.example` / `config.example.*` alongside any file that reads
  real config, with every key present and an obviously-placeholder value
  (`YOUR_DOMAIN_HERE`, not a real one with the last octet changed).
- If a CI/CD pipeline assumes infrastructure that doesn't ship with the
  repo (a specific runner pool, a specific cloud account, a specific
  private reusable workflow), say so explicitly in the README rather than
  let a stranger discover it as a mysterious failure. "This requires your
  own self-hosted runner and your own AWS account" is an honest
  dependency; a silent reference to `uses: <this-operator>/infra-private/...`
  is not.
- Prefer this repo's own already-public reusable workflows
  (`quadseven/infra-public/...`) over hand-rolled CI where one already
  exists — they're already written to take config as input rather than
  assume it.

## Why this file, not just a smarter secret scanner

A pattern-matching scanner catches shapes: an AWS key, a PEM block, a
32-character hex string. It cannot catch a paragraph of ordinary prose
that happens to name a real person or describe a real network in plain
words — which is where nearly everything this file exists to prevent
actually showed up. Scanners still belong in CI as a backstop for the
shapes they *can* catch; this file is the layer above that, for the judgment
a scanner doesn't have.

<!-- Everything above the next line is synced from quadseven/infra-public and replaced on every sync. Put this repo's own content below it. -->
<!-- repo-specific below -->

# AGENTS.md

For anyone changing this code, human or model. zippie is self-hosted per-packet multipath bonding: a travel router joins every WAN it can reach and carries traffic over them to an exit on the operator's home uplink.

This file is short on purpose: the reasoning lives in `ARCHITECTURE.md` and `DECISIONS.md`, and its job is to stop a change walking into what has already stranded a router.

Read `CONTEXT.md` first for the vocabulary, then `ARCHITECTURE.md` for the tour. `DECISIONS.md` records why things have their shape; each entry names the failure that caused it. `docs/state-of-play.md` holds dated live state.

## Layout

    travel/bond-agent/    multipath control plane (Python 3.9, the router's version)
    travel/datapath-go/   Go packet datapath; mobile/ is a nested module
    travel/gl-mt3000/     router scripts, rollback, muster refresh
    home/bond-server/     home exit
    hub/                  control-plane hub for every node
    deploy/oke/           Kubernetes manifests; targets.tsv lists what deploys
    companion/            iOS relay app
    companion-android/    Android relay app
    design/               tokens and generated files
    scripts/              installers, deploy, smoke test, guards

## Commands

```bash
# The lane check in .loom/validation.conf: ruff, go vet, agent pytest in one line.
uvx ruff@0.15.10 check . && uvx ruff@0.15.10 format --check .
(cd travel/datapath-go && go vet ./...)
(cd travel/bond-agent && uv run --with 'pytest>=8.0' --with tomli --python 3.9 python -m pytest -q)

# The rest of check.agent-tests.yml
(cd hub && uv run --with 'pytest>=9.0.3' --with pyyaml --python 3.12 python -m pytest -q)
(cd deploy/oke && uv run --with 'pytest>=9.0.3' --python 3.12 python -m pytest -q)
node hub/tests/hub_js.mjs
(cd travel/datapath-go && go test ./... -race -count=1 && go test ./zippie/ -run 'Fuzz' -count=1)
(cd travel/datapath-go/mobile && go test ./... -race -count=1)

# check.operator-hosts.yml, on every PR
bash scripts/check-no-operator-hosts.sh --self-test
bash scripts/check-no-operator-hosts.sh
python3 travel/bond-agent/tests/test_readme_standard.py

python3 design/generate.py --check   # design/ or generated tokens
make sync-home-pkg                   # after editing a module shipped to the home pod
make smoke                           # offline proof: no root, no real WANs
pre-commit run --all-files           # the managed hooks; same ruff pin as CI
```

Prove a guard fails without its fix. Run mutations with `python -B` and clear `__pycache__` first: a same-second `.pyc` otherwise serves stale bytecode and reports a mutant as killed when nothing ran.

Local traps:

- On macOS, 14 tests in `test_transport.py` fail on Python 3.9 because `socket.SO_BINDTODEVICE` does not exist there, so the lane check is red on a Mac. `TestPacedLoopbackSurvivesSlowTravelReader` also timed out on macOS on 2026-10-31. CI runs Linux and is the judge.
- `make lint` ends in `|| true` and cannot fail, and `make test` uses the system Python. Use the commands above.
- `scripts/smoke-test.sh` needs its own `travel/bond-agent/.venv`. A venv left by `uv run` has no `pip`; delete it and rerun.

## Checks and shipping

Required on `main`: a PR, `zippie datapath go test` and `Grug - Chief`. Most other jobs are not required, and the binding set is the merge gate below. Other PR checks: the leak scan, `operator hosts ratchet`, `zippie agent pytest` and the hub and deploy suites (path-filtered), `ruff (check + format)` on Python changes, the design token check, and the Android and iOS builds on their paths. This repo calls no workflow hygiene, size ratchet, CI config freshness or close completeness guard.

**Trust gate here.** The `guard` job admits `OWNER`, `MEMBER`, `COLLABORATOR` and `loom-lanes[bot]`. Any other PR author fails it, and `zippie datapath go test` fails with it, even on a PR that touches no Go, because that job requires the gate.

What a merge does:

| Path | Effect |
|---|---|
| `travel/**` except `*.md` and tests, `scripts/deploy-openwrt.sh` | `deploy.travel-router.yml` deploys to the live router |
| `deploy/oke/**` | `deploy.oke-manifests.yml` applies to the cluster; it also reconciles every 6 hours |
| `travel/datapath-go/**`, `deploy/oke/zippie-clienthome/**` | `build.clienthome-image.yml` publishes the image |
| `companion/`, `companion-android/` | nothing ships |

TestFlight and the Android release are `workflow_dispatch` only. Run them only on an operator's ask.

Issue numbers restarted on 2026-08-28, so a `#N` in older text can mean the earlier repo. `docs/agents/issue-tracker.md` lists the live epics.

## The one thing that makes this repo different

**The travel router's only uplink is the bond this code implements.** Its default route is `dev pbz0`. There is no separate management path, no WAN sitting there as a backstop, and no console reachable from anywhere else.

So a change that breaks the agent removes the router's own route to the internet, and with it every way of fixing it. That has happened. It is why the deploy has its shape, and why the rules below are not ceremony.

## Before touching anything under `travel/`

**Merging to `main` deploys to a live router.** There is no staging device. If the operator is traveling, the operator is traveling on the thing that just changed.

**The rollback must fire before anyone relies on it.** `travel/gl-mt3000/deploy-rollback.sh` is armed before the change and disarms itself on success. The discipline that matters is not arming it: it is deliberately firing it while nothing is broken, confirming the router comes back, then re-arming and making the real change. A rollback path that has never executed is an assumption.

Three specific traps, each found the hard way:

- `setsid` and `nohup &` do not survive on this hardware, busybox has no `date -d "+N min"`, and backgrounded sleeps are unreliable over long windows. The only dependable mechanism is a self-removing cron one-shot at a fixed wall-clock minute. **Read the crontab line back**, because a malformed line is accepted silently and never fires.
- An ssh-driven restart cannot survive the link it tears down. Hand the restart to the router's own cron.
- "The new files are on disk" is not "the new agent is running". Fingerprints are computed from files, so an old process reports the new fingerprint. Gate on the process, not the digest.

**A scrubbed placeholder that reaches the router fails silently.** A scrub replaced the home endpoint with `dns-e.example-home.invalid`, that config reached the router, and RFC 2606 guarantees `.invalid` never resolves. At home it was invisible, because a leg on the house network uses `lan_endpoints` instead. Away from home, nothing would have matched and the bond would have had nothing to dial.

`scripts/deploy-openwrt.sh` now refuses to ship reserved-range values. Do not work around it. A value that is dialed, resolved or compared at runtime gets a parameter or is read from the router, never a plausible fake.

## The datapath is shipped twice, and the copies must match

`travel/bond-agent/zippie/transport.py` and `deploy/oke/zippie-home/zippie-pkg/transport.py` are the same file for the travel end and the home end. `deploy.oke-manifests.yml` enforces that the running pod matches the commit, and the file list is held *equal* to the `configMapGenerator` in `deploy/oke/zippie-home/kustomization.yaml` rather than merely overlapping.

**Adding a module means editing both**, plus `test_manifest_copy_in_sync.py`. A module that ships without being listed is not drift-checked, so nothing reports a pod running something other than the commit.

Merging anything under `deploy/oke/**` rolls the home pod, which is the far end of the bond. Expect the tunnel to drop while it restarts. Measured recovery has ranged from about 95 seconds to about 6 minutes.

## Datapath authentication

`travel/bond-agent/zippie/auth.py`. Four rungs: `OFF`, `OBSERVE`, `SIGN`, `REQUIRE`.

**`OFF` is the zero value, deliberately.** An unconfigured transport emits byte-identical v2 frames, and a test pins that, so the code can land without changing anything on the wire, and the rollout is a config change rather than a deploy. Both ends must never be more than one rung apart; home moves first.

Residual risks are documented in the module: no replay protection (the MAC cannot cover a NAT-rewritten source address), an attacker who knows the current epoch can hold the takeover window shut against a genuine restart until `REQUIRE`, and no forward secrecy or automatic rotation.

**Rotating the key is a four-step overlap, home first at every step.** An end signs with `<auth_key_file>` and also accepts `<auth_key_file>.previous` while that file exists; each step is a file change at one end followed by a restart of that end, because the key files are read once when the identity is built. Home: new key into `.previous`. Router: new key to current, old key to `.previous`. Home: new key to current, remove `.previous`. Router: remove `.previous`. The new key goes into `.previous` at home first because home moving its signing key first would have it signing with a key the router cannot yet verify. Removing `.previous` is the operator's act; the router's key refresh writes it and never deletes it.

## This repository is public

`scripts/check-no-operator-hosts.sh` and the `operator hosts ratchet` check exist because a scrub removed the topology and nothing stopped it returning. No real hostnames, addresses, SSIDs or personal identifiers: not in code, comments or fixtures.

**The guard checks ADDRESSES, not names, and a hostname has already come back through that gap.** PART 2 of that script is an allowlist of permitted IP ranges, because a guard that failed on the household /24 would have to write the household /24 in a public file. It has no equivalent for a bare word, so the travel router's own name walked back into `musterwrt.py`, its test and an ADR, twelve occurrences, and passed CI each time.

Names are caught by the leak scan's deny-list layer (`.github/workflows/check.leak-scan.yml`, #38), which reads host codenames and other shapeless terms at run time from SSM and fails the PR on a hit. It reads each PR's diff, title and body, not the whole tree or issue bodies, and it only knows the terms on its list. Grep before pushing anyway, and write "the travel router", which is also what its certificate says.

## There is a shaper on the bond, and the agent sets its rate

`sqm-scripts` runs cake on `pbz0`, the bond after decryption, where every client flow is visible. On `apclix0` the traffic is one WireGuard conversation, so cake would see a single flow and could not do its job.

Measured 2026-08-31 on a repeater'd house wifi, same 50 MB fetch with a ping beside it: **85 / 355 / 920 ms at 143 KB/s without it, 24 / 78 / 281 ms at 403 KB/s with it.** Both numbers improved, because the upstream buffer had been so full the link was carrying packets already too late to be useful.

**A shaper only controls a queue while its rate is below what the path can carry.** A fixed rate is right behind one house wifi and wrong on a motorway, and cake's `autorate-ingress` estimated 65 Mbit/s against a real 5. So the agent estimates the rate from the carrying legs and moves it live with `tc qdisc change` (`zippie/shaper.py`, #41). `policy.shaper_auto_rate` defaults to on.

The deploy creates the `sqm.pbz0` section once and never re-pins it. With `shaper_auto_rate` off, the rate is `uci set sqm.pbz0.download=<kbit>; uci commit sqm; /etc/init.d/sqm restart`, and it survives the next merge.

**Check the qdisc, not the config.** `uci get` proves a file was written. sqm silently does nothing when its interface is absent at start, and the bond can come up after the init script, which is why the agent restarts sqm from the place that creates `pbz0`. A shaper that is configured and not running looks like a working one until a download starts. `tc qdisc show dev pbz0` is the only proof.

## The router renews its own certificate

`travel/gl-mt3000/muster-refresh.sh` runs hourly and does three things: checks how long the certificate has left, tries a renewal once a day, then refreshes the datapath key. All three are safe to fail: `musterwrt` keeps what is on disk on every failing path, and the agent never waits for any of it.

**The router does not decide when to renew.** It asks; muster answers 409 if it is too early. `renew_after` is computed from a certificate's own dates on the server, and a copy of that arithmetic here would be a second definition of it, on the one box in the estate whose clock can read 1970 at boot.

**`openssl req -new -key`, never `-newkey`.** The first signs a request for the key this router already has; the second generates one, overwriting the only credential the box can prove itself with, on hardware that may be in another state. A test pins the CSR's public key against the key file.

**A new certificate is verified against the private key before it replaces the working one.** A mismatch is a router that cannot prove itself to anything and cannot renew its way out, because renewing needs the proof it just discarded. Recovery is physical, and this project has already paid for that once.

**A 403 means revoked, and it deletes nothing.** `musterwrt.Revoked` is loud; before it existed a 403 came back as `Unreachable` and was logged at the same volume as a hotel captive portal. It keeps the cached key: a router that wiped its own key on a 403 would island itself the moment somebody revoked the wrong key_id.

## Related

[muster](https://github.com/quadseven/muster) exists because of a decision made here: a device should fetch its own identity over its own credential rather than have a deploy pipeline splice a static secret into its config. It does not use mTLS, and its README explains why. Read that before assuming a TLS handshake is available at that end.

## How work moves here

Several AI brains work this repo beside the operator: Claude Code, Muse, Dot and the Loom factory. Loom has a station that dispatches, a brain that briefs and merges, and lanes that build. No brain outranks another. Each acts under its own GitHub App, never a person's account, and signs posts `-- <name>`.

**Claims.** Put `claim:<name>` on a ticket before starting, and create the label if this repo lacks it. Check first and skip a ticket already held. One claim per ticket: the station keeps the earliest and removes the rest. A claim expires after 2 hours with no real activity, unless a comment holds it with `waiting: <what> until <time>`, or an open PR the claimer wrote and referenced holds it for up to 7 days. `claim:quadseven` is a person: leave those tickets alone. On stopping, push the work and say what is done, what is next, and the branch and head. Take over only an expired or plainly dead claim, after reading the holder's comments and open PRs.

**Labels.** Loom sets one state label per ticket:

`loom:triage` (not ready) -> `loom:queued` (no lane) -> `loom:briefed` + `lane:<name>` -> `loom:running` -> `loom:pr` -> `loom:merged` -> `loom:shipped` -> `loom:verified`, then closed.

`loom:ticket` marks every ticket. `loom:failed` means the runner gave up; the reason is on the ticket. `wait:*` names why a ticket is held. `needs:human` is an ask for a person. `loom:factory` marks a fault in the factory itself.

**CI and required checks.** Required checks differ per repo, so read them live: `gh api repos/<owner>/<repo>/rules/branches/<default-branch>`. Every repo needs a PR and `Grug - Chief`; most add test jobs; some add `Grug - Elder`. Run each command in `.loom/validation.conf` before pushing, and read the failing workflow file before guessing at a red check.

- Shared workflows come from `quadseven/infra-public`, pinned by full SHA.
- Leak scan: reads the diff and the PR title and body, and a scan that cannot run is red. It has 9 rules: tailnet hosts, tailnet IPs, private-range IPs, server names, node names, user paths, MAC addresses, the stray mention, and cross-repo refs. A ref of the form `name#N` or `owner/name#N` to any other repo fails, public or not, unless `allowed-repo-refs` in `check.leak-scan.yml` names that repo. Bare `#N` means this repo. A line with `leak-guard-allow` is exempt and visible in review; use it only for a real false positive.
- Trust gate: a job on self-hosted workflows that admits collaborators only and can reject an App token.
- Other guards, where a repo calls them: workflow hygiene, size ratchet, CI config freshness, and close completeness, which reopens an issue closed with unticked boxes.
- Agent Apps cannot push `.github/workflows` changes.

**`grug`.** The bot is `grug-tribe[bot]`, driven by slash commands in a PR comment. Its handle takes no at-sign.

- `Grug - Chief` reads the PR body. It wants `## Why` (5+ words), `## Acceptance criteria` (3+ bullets), `## Out of scope`, `Size: XS|S|M|L`, and `Refs <owner>/<repo>#N` or `Refs #N` to a ticket that is an epic or sits under one. Text in HTML comments does not count. Closing keywords are out: tickets close when their boxes are ticked.
- A body with an agent marker (such as a session link) from an App author, or a Loom lane footer, gets a neutral Chief: `Hunt Plan skipped`. Neutral passes. A body with no marker gets the full check, so write the full shape every time.
- `Grug - Elder` reviews the code by LLM. `/grug improve` re-runs it. `/grug recheck` re-runs Chief only. Five pushes in ten minutes pause Elder until `/grug resume`.
- A degraded or partial review says `Degraded review (<reason>): not a verdict`. It is not a pass, whether it concludes failure or neutral. Run `/grug improve` once the backend recovers.
- `Grug - Guard` scans for security problems and is advisory unless the repo turns on blocking. Haunt, once called Sentinel, comments when a PR closes over an unresolved critical finding. It posts no check; answer its comment and re-verify.

**Merge gate.** Rulesets enforce less than this, and this binds: CI green, `Grug - Elder` success on the head (neutral is not enough), `Grug - Chief` success or neutral, not behind base, every bot thread answered, and a test that fails without the fix. `gh pr checks` does not show threads, so read `reviewThreads` by GraphQL and `pulls/<n>/reviews`. Anyone merges what they built. Loom is in shadow on this repo: it runs every gate and records each merge or close for the operator instead of taking it, until the operator graduates the repo. After graduation, Loom merges docs, test and app code tiers alone and holds deploy, security and cross-repo tiers for the operator. A change to how brains merge, close or rewrite needs a fleet chat line and a 5 minute wait. Never use `--admin`.

**Human line.** People do: permanent deletion of records, creating, rotating or entering credentials, access grants, new spending, accepting terms. People decide: another brain's role and the fleet goal text. A second brain must agree, in writing, to clear a lane demotion, lift another brain's hold, or reopen a sound close. Everything else needs nobody. Ask with `needs:human` and one exact question. A relayed yes is not a yes. Never print a credential.

**When stuck.** Labels and stages: `docs/BRAIN.md` in `quadseven/loom`. Reusable workflows: `docs/reusables.md` in `quadseven/infra-public`. Chief's rules and every `/grug` verb: the README in `quadseven/grug`, whose `make chief-check` tests a body before posting. Fleet log: issue 30 in `quadseven/loom`. File a factory fault there as `loom:factory`. Poll `gh` at most once per 5 minutes.
