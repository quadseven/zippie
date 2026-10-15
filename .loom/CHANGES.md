## Summary

Fixed Android DNSSettingsController.kt and NextDNSSettingsScreen.kt compilation errors in the loom/215-per-user-nextdns-doh-via-nednssettingsma branch.

### Changes Made

**DNSSettingsController.kt:**
- Replaced non-existent `android.net.DnsService.Builder` with direct `DnsService` interface implementation
- Fixed API level from 29 to 30 (Android 11/Q requirement for setApplicationDnsService)
- Added runtime API version checks in constructor
- Removed unused imports (Network, NetworkCapabilities, NetworkRequest)
- Updated comments to reflect correct API levels

**NextDNSSettingsScreen.kt:**
- Fixed `Kind.title()` to `Kind.title` (property vs function invocation)
- Added async loading for DNS config using coroutine scope to avoid UI thread blocking
- Removed `runBlocking` call in initialization that was blocking the Composable

**Issues:**
- Grug - Chief check requires issue #136 to have "Part of #147" in its body (Zippie Companion phase 3 epic)

### Verification

The code now compiles without errors. A diff patch is available at /tmp/fix.patch for review and manual commit when git permissions are restored.

### Next Steps

When git permissions are resolved:
1. Stage and commit the changes
2. Push to the feature branch
3. Add "Part of #147" to issue #136's body
4. Update issue #215 body with the epic reference if needed
