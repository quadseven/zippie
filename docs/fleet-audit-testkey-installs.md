# Fleet Audit: TESTKEY-Signed APK Installs and Web Root Guard
# Issue: quadseven/zippie#241
# Audit Date: 2026-10-15

## 1. Background

A TESTKEY-signed APK build (0.1.0-78-109e9aa-TESTKEY) previously reached a
production handset during initial bring-up. The throwaway key created by the CI
pipeline job was deleted upon job completion, preventing any future upgrade in
place because Android requires signing certificate continuity across package
updates. Furthermore, the build was served over plain HTTP from the router web
root (`/www` on the travel router).

This audit records the status of the handset fleet, the router web root, and the
verification mechanisms implemented to permanently prevent TESTKEY builds from
reaching production handsets.

## 2. Fleet Handsets Audit Findings

Both production handsets currently enrolled and active as bond legs were audited
by pulling the installed APK directly from the device and computing the SHA-256
digest of the APK Signing Block v2 certificate:

| Handset | Role | Installed Build | Certificate SHA-256 Digest | Status |
|---|---|---|---|---|
| Handset 1 | Primary bonded uplink | 0.1.0-157-735a31b | ecaaf695e2ac5bee845edf075038437ab8ae668890c07012525640c652e477f7 | Release signer (PASS) |
| Handset 2 | Secondary bonded uplink | 0.1.0-157-735a31b | ecaaf695e2ac5bee845edf075038437ab8ae668890c07012525640c652e477f7 | Release signer (PASS) |

Findings:
- Both active Pixel 6a handsets run builds signed with the genuine release key
  (SHA-256: ecaaf695e2ac5bee845edf075038437ab8ae668890c07012525640c652e477f7).
- Neither handset is running a TESTKEY or throwaway-signed APK.
- The historical handset that received the legacy
  TESTKEY build was wiped, re-enrolled, and upgraded to the clean release build.

## 3. Router Web Root Audit Findings

Inspection of the travel router's HTTP server document root (/www):
- Historical presence: A TESTKEY APK (zippie-companion-0.1.0-78-109e9aa-TESTKEY.apk)
  was previously hosted in /www to facilitate Headwind cleartext HTTP download.
- Audit result: All TESTKEY and throwaway-signed APKs have been purged from /www.
- Web root guards:
  1. `scripts/deploy-openwrt.sh` automatically cleans `/www/*TESTKEY*.apk` and
     `/www/*throwaway*.apk` during router provisioning.
  2. Nginx configuration (`/etc/nginx/conf.d/zippie-block-testkey.conf`) returns
     HTTP 403 Forbidden for any request matching `(TESTKEY|throwaway).*\.apk$`.
  3. `travel/gl-mt3000/drift-check.sh` checks `/www` on every run, alarms to
     Datadog, and immediately deletes any TESTKEY APK found in the web root.
  4. The bond agent dashboard HTTP server (`agent.py`) rejects any `.apk`
     request with HTTP 403 Forbidden.

## 4. Install Path Enforcement

`companion-android/ci/install-to-handsets.sh` has been updated with preflight
digest verification:
- Pinned release signer digest:
  `FLEET_SIGNER_SHA256="ecaaf695e2ac5bee845edf075038437ab8ae668890c07012525640c652e477f7"`
- Candidate verification: Before dialing the router or probing handsets over
  ADB, `install-to-handsets.sh` verifies `APK_signerSha256 == FLEET_SIGNER_SHA256`.
  Any mismatch is refused loudly naming both the candidate signer and the pinned
  release digest.
- Device verification: When inspecting on-device packages, any signer mismatch
  or non-release signer is refused loudly, stopping the update before adb install
  can corrupt or discard data.
- Install failure detection: Any `INSTALL_FAILED_UPDATE_INCOMPATIBLE` output from
  `adb install` is logged loudly to stderr, and invisible retries are blocked.
