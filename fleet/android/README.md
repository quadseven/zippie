# Fleet Android policy fragments

Fleet stores Android configuration profiles as raw Android Management API
policy JSON (`mdm_android_configuration_profiles.raw_json`), so anything the
API expresses can be applied here.

## allow-developer-settings.json

**Why it exists.** A device enrolled through Fleet is fully managed (Device
Owner), and the Android Management API sets security policy to its most secure
values by default. `advancedSecurityOverrides.developerSettings` therefore
defaults to `DEVELOPER_SETTINGS_DISABLED`, which is why Developer options
cannot be turned on from the phone's own Settings. The phone is not broken and
nothing is misconfigured - it is doing what a managed device is supposed to do.

`developerSettings` replaces the deprecated `debuggingFeaturesAllowed` and
`safeBootDisabled`.

**When to use it.** For adb access to a managed device - reading logs, running
diagnostics. That is what it actually buys.

**It does NOT enable sideloading, and this was tested rather than assumed.**
With both `DEVELOPER_SETTINGS_ALLOWED` and
`untrustedAppsPolicy: ALLOW_INSTALL_DEVICE_WIDE` applied and verified on the
device, `adb install` reports Success, the package appears with a path, and then
GOOGLE PLAY DELETES IT within seconds:

    Finsky: Uninstaller: ... package=app.zippie.companion, status=UNINSTALLED

On a fully managed device, managed Google Play enforces the enterprise app set
and removes anything not in the policy's `applications` list. No security
override changes that, because it is not a security check - it is Play
reconciling the device against the policy.

So there is no sideload path onto a Fleet-managed device. Publishing the
companion as a private app is not the preferred route, it is the ONLY route.

**Turn it back off afterwards.** This relaxes a security default that Google
explicitly recommends leaving alone, and it does so on a device that carries
household traffic. Remove the profile once the app is installed, rather than
leaving the phone permanently debuggable because it was convenient once.

## Before applying, check how Fleet composes profiles

VERIFY THIS RATHER THAN ASSUMING IT. It is not established here whether Fleet
MERGES a profile into the policy it already sends, or REPLACES the policy with
the profile's contents. A fragment this small is safe under merge and
potentially destructive under replace - it could drop every other setting the
default policy carries.

The cheap check is to apply it to a device that carries nothing, then read the
resulting policy back and confirm the settings you expected are still present.
The spare phone exists for exactly this kind of question; the leg carrying the
household's traffic does not.
