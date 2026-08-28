#!/usr/bin/env python3
"""App Store Connect helper for the TestFlight CI workflow (fastlane-free).

Reads the ASC API key from the environment (ASC_KEY_ID, ASC_ISSUER_ID,
ASC_KEY_P8 = the .p8 PEM contents). Subcommands:

  next-build-number          Print max TestFlight build for the app + 1.
  distribute <build_number>  Wait for that build to finish processing, then add
                             it to the internal "all builds" beta group so it
                             releases to the internal testers' phones.
  set-notes <build_number>   Upsert the build's TestFlight "What to Test" notes
                             (whatsNew) from the TESTFLIGHT_NOTES env var so each
                             build tells testers what changed.

The "Internal All Builds" group has hasAccessToAllBuilds=true, but builds
uploaded outside a distribution step land processed-but-unreleased; adding the
build to the group is what actually pushes it to the testers (learned the hard
way: Xcode Cloud archives uploaded fine yet never reached the phone).
"""
import os
import sys
import time

import jwt
import requests

BASE = "https://api.appstoreconnect.apple.com"
APP_ID = os.environ.get("ASC_APP_ID", "6780286800")  # Macchina
# "Internal All Builds" beta group (hasAccessToAllBuilds=true).
GROUP_ID = os.environ.get("ASC_BETA_GROUP_ID", "3fc37752-0cdb-45e7-9fa2-8c753a2b53b6")
# Locale for the "What to Test" notes. ASC requires the build's primary locale.
NOTES_LOCALE = os.environ.get("ASC_BETA_LOCALE", "en-US")
# ASC hard-caps whatsNew at 4000 chars.
NOTES_MAX = 4000


def _token() -> str:
    key_id = os.environ["ASC_KEY_ID"].strip()
    issuer = os.environ["ASC_ISSUER_ID"].strip()
    private_key = os.environ["ASC_KEY_P8"]
    now = int(time.time())
    return jwt.encode(
        {"iss": issuer, "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"},
        private_key,
        algorithm="ES256",
        headers={"kid": key_id, "typ": "JWT"},
    )


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def _get(path: str, **params) -> dict:
    r = requests.get(BASE + path, headers=_headers(), params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def next_build_number() -> int:
    data = _get(
        "/v1/builds",
        **{"filter[app]": APP_ID, "sort": "-version", "limit": 1, "fields[builds]": "version"},
    )["data"]
    latest = int(data[0]["attributes"]["version"]) if data else 0
    return latest + 1


def _find_build(version: str) -> str | None:
    data = _get(
        "/v1/builds",
        **{
            "filter[app]": APP_ID,
            "filter[version]": version,
            "limit": 1,
            "fields[builds]": "version,processingState",
        },
    )["data"]
    return data[0]["id"] if data else None


def distribute(version: str, timeout_s: int = 1800) -> None:
    """Wait for the build to finish processing, then add it to the group."""
    deadline = time.time() + timeout_s
    build_id = None
    while time.time() < deadline:
        bid = _find_build(version)
        if bid:
            detail = _get(
                f"/v1/builds/{bid}",
                **{"fields[builds]": "processingState"},
            )["data"]["attributes"]["processingState"]
            print(f"build {version}: {detail}", flush=True)
            if detail == "VALID":
                build_id = bid
                break
            if detail in ("INVALID", "FAILED"):
                sys.exit(f"build {version} processed {detail}")
        else:
            print(f"build {version}: not yet visible in App Store Connect", flush=True)
        time.sleep(30)
    if not build_id:
        sys.exit(f"timed out waiting for build {version} to process")

    # The "Internal All Builds" group has hasAccessToAllBuilds=true, so once a
    # build reaches VALID it is auto-added to the group. If that already
    # happened (the build is IN_BETA_TESTING), the explicit add below returns
    # 422 "Cannot add internal group to a build" - which is success, not
    # failure. Check the live state first and treat already-distributed as done.
    def internal_state() -> str:
        return internal_build_state(build_id)

    if internal_state() == "IN_BETA_TESTING":
        print(f"build {version} already released to internal testers (auto-added)", flush=True)
        return

    r = requests.post(
        BASE + f"/v1/betaGroups/{GROUP_ID}/relationships/builds",
        headers=_headers(),
        json={"data": [{"type": "builds", "id": build_id}]},
        timeout=60,
    )
    if r.status_code in (200, 201, 204):
        # Verified, not assumed. A 204 says the relationship was created, which
        # is NOT the same as the build being released - a build missing its
        # export-compliance declaration accepts the group assignment and still
        # reaches nobody.
        _require_distributed(build_id, version)
        return
    # A 422 "Cannot add internal group to a build" IS success for an all-builds
    # internal group (hasAccessToAllBuilds=true): the VALID build auto-distributes
    # to internal testers and cannot be explicitly group-assigned. The
    # `internalBuildState` flip to IN_BETA_TESTING lags VALID by ~a minute, so do
    # NOT require it to have flipped already -- the specific 422 is itself the
    # auto-distribution signal (this is exactly why build 113 false-failed).
    if r.status_code == 422 and (
        "internal group" in r.text.lower() or internal_state() == "IN_BETA_TESTING"
    ):
        # NOT trusted on the strength of the message alone. A 422 whose text
        # merely MENTIONS an internal group was read as success and printed a
        # cheerful line while the build reached nobody: zippie build 4 was being
        # POSTed to a MACCHINA group (ASC_BETA_GROUP_ID unset, see the workflow),
        # so of course it was refused. The step went green and the build sat
        # undistributed. Confirm reality instead of parsing the excuse.
        _require_distributed(build_id, version)
        return
    sys.exit(f"add-to-group failed: {r.status_code} {r.text[:300]}")


def _require_distributed(build_id: str, version: str) -> None:
    """Fail unless the build really is released to internal testers.

    internalBuildState lags VALID by up to a minute or so, hence the poll rather
    than a single read -- requiring an instant flip is what false-failed build
    113. But "it might flip later" is not a reason to declare success now, which
    is the mistake this replaces.

    MISSING_EXPORT_COMPLIANCE is called out by name because it is silent,
    common, and not a code problem: the build is fine and simply may not be
    distributed until the encryption declaration exists. Saying so beats a
    generic timeout that sends someone into the build logs.
    """
    deadline = time.time() + 300
    state = ""
    while time.time() < deadline:
        state = internal_build_state(build_id)
        if state == "IN_BETA_TESTING":
            print(f"build {version} confirmed released to internal testers", flush=True)
            return
        if state == "MISSING_EXPORT_COMPLIANCE":
            sys.exit(
                f"build {version} cannot be distributed: MISSING_EXPORT_COMPLIANCE. "
                "Set ITSAppUsesNonExemptEncryption in the app Info.plist, or "
                "declare usesNonExemptEncryption on the build."
            )
        print(f"build {version}: internalBuildState={state}, waiting", flush=True)
        time.sleep(15)
    sys.exit(
        f"build {version} was NOT released to internal testers "
        f"(internalBuildState={state!r} after 5 minutes)"
    )


def internal_build_state(build_id: str) -> str:
    d = _get(f"/v1/builds/{build_id}/buildBetaDetail",
             **{"fields[buildBetaDetails]": "internalBuildState"})["data"]
    return d["attributes"].get("internalBuildState", "")


def _clean_notes(raw: str) -> str:
    """Turn a git commit message into something readable on a PHONE.

    A commit body is hard-wrapped at ~72 columns for terminals and `git log`.
    TestFlight renders it in a narrow, variable-width column and wraps it
    AGAIN, so every one of those hard breaks becomes a ragged mid-sentence
    newline. The result is technically the right words in an unreadable shape.

    So: unwrap. Paragraphs (blank-line separated) become single long lines and
    let the phone do the wrapping. Lines that were deliberately structured -
    list items, indented continuations - are left alone, because re-flowing a
    numbered list into prose destroys the one thing that made it scannable.

    Also drops the conventional-commit prefix from the subject: "fix(zippie
    companion): " is meaningful in a git log and noise to a tester looking at
    a phone.
    """
    drop_prefixes = (
        "Co-Authored-By:", "Co-authored-by:", "Claude-Session:",
        "Generated with", "Signed-off-by:",
        # Provenance trailers: real and useful in the repo, meaningless here.
        "AI-REVIEW(", "Refs ", "Part of ", "Closes ",
    )
    lines = [
        ln for ln in raw.splitlines()
        if not any(ln.strip().startswith(p) for p in drop_prefixes)
    ]

    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append(" ".join(x.strip() for x in buf))
            buf.clear()

    def is_item(t: str) -> bool:
        return (
            t[:2] in ("- ", "* ")
            or (t[:1].isdigit() and t[1:3] in (". ", ") "))
        )

    in_item = False
    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            flush()
            in_item = False
            if out and out[-1] != "":
                out.append("")
            continue
        if is_item(stripped):
            # A new item ends the previous one. The item and every indented
            # continuation under it become ONE line, so the list keeps its shape
            # while each entry wraps naturally on a narrow screen.
            flush()
            in_item = True
            buf.append(stripped)
            continue
        if in_item and ln[:1].isspace():
            buf.append(stripped)
            continue
        # Ordinary prose. An indented line that is NOT under a list item is
        # left as-is: someone indented it deliberately.
        if ln[:1].isspace():
            flush()
            in_item = False
            out.append(ln.rstrip())
            continue
        if in_item:
            flush()
            in_item = False
        buf.append(stripped)
    flush()

    text = "\n".join(out).strip()
    # "fix(scope): summary" -> "Summary". The scope is for the repo, not a phone.
    if "\n" in text or text:
        first, _, rest = text.partition("\n")
        if ":" in first:
            head, _, tail = first.partition(":")
            if len(head) < 40 and "(" in head or head.islower():
                first = tail.strip()
                if first:
                    first = first[0].upper() + first[1:]
        text = first + ("\n" + rest if rest else "")
    return text[:NOTES_MAX]


def set_notes(version: str) -> None:
    """Upsert the build's en-US betaBuildLocalization whatsNew from env."""
    notes = _clean_notes(os.environ.get("TESTFLIGHT_NOTES", ""))
    if not notes:
        print("set-notes: TESTFLIGHT_NOTES empty after cleaning; skipping", flush=True)
        return
    build_id = _find_build(version)
    if not build_id:
        sys.exit(f"set-notes: build {version} not found in App Store Connect")

    # One localization per locale already exists once the build is visible; PATCH
    # it if present, else POST a fresh one related to the build.
    existing = _get(
        f"/v1/builds/{build_id}/betaBuildLocalizations",
        **{"fields[betaBuildLocalizations]": "locale", "limit": 50},
    )["data"]
    loc_id = next(
        (d["id"] for d in existing if d["attributes"].get("locale") == NOTES_LOCALE),
        None,
    )
    if loc_id:
        r = requests.patch(
            BASE + f"/v1/betaBuildLocalizations/{loc_id}",
            headers=_headers(),
            json={"data": {"type": "betaBuildLocalizations", "id": loc_id,
                           "attributes": {"whatsNew": notes}}},
            timeout=60,
        )
    else:
        r = requests.post(
            BASE + "/v1/betaBuildLocalizations",
            headers=_headers(),
            json={"data": {
                "type": "betaBuildLocalizations",
                "attributes": {"locale": NOTES_LOCALE, "whatsNew": notes},
                "relationships": {"build": {"data": {"type": "builds", "id": build_id}}},
            }},
            timeout=60,
        )
    if r.status_code in (200, 201):
        print(f"set-notes: wrote {len(notes)} chars to build {version}", flush=True)
        return
    sys.exit(f"set-notes failed: {r.status_code} {r.text[:300]}")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: asc.py {next-build-number|distribute <version>|set-notes <version>}")
    cmd = sys.argv[1]
    if cmd == "next-build-number":
        print(next_build_number())
    elif cmd == "distribute":
        if len(sys.argv) < 3:
            sys.exit("usage: asc.py distribute <version>")
        distribute(sys.argv[2])
    elif cmd == "set-notes":
        if len(sys.argv) < 3:
            sys.exit("usage: asc.py set-notes <version>")
        set_notes(sys.argv[2])
    else:
        sys.exit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
