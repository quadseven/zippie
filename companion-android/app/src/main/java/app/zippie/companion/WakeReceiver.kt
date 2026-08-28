package app.zippie.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * The door an MDM knocks on to bring a freshly installed app to life, silently.
 *
 * THE PROBLEM. An app installed and never launched sits in Android's STOPPED
 * state, and a stopped app receives NO broadcasts - including BOOT_COMPLETED.
 * So an MDM could install zippie, configure it correctly, set
 * `autoStartRelay=true`, reboot the handset, and the app would never run or
 * say anything. Observed exactly that on a managed Pixel on 2026-08-23: correct
 * package, correct config, relay port closed, zero lines in logcat.
 *
 * THE ONE THING THAT REACHES A STOPPED APP is an EXPLICIT intent carrying
 * `Intent.FLAG_INCLUDE_STOPPED_PACKAGES` (32). It both delivers and clears the
 * stopped state, so every subsequent BOOT_COMPLETED arrives normally. An
 * implicit broadcast with the same flag does NOT reach a stopped app - the
 * intent must name this component.
 *
 * Found by quadseven/muster reading android-36's DevicePolicyManager rather
 * than recalling it: there is no DPC API that says "unstop", and its neighbours
 * (setApplicationHidden, setPackagesSuspended, setUserControlDisabledPackages)
 * do not promise it as a side effect.
 *
 * WHY THIS RATHER THAN LAUNCHING MainActivity. A launch works and puts a window
 * on the screen of a phone sitting in a car, which is not something an MDM
 * should have to do to make a background service run. This is silent, and it is
 * the thing an MDM should be doing anyway.
 *
 * THE COMPONENT NAME IS A CONTRACT. muster names it in policy, so renaming or
 * moving this class breaks provisioning on every enrolled handset. It is
 * `app.zippie.companion/.WakeReceiver` and it should stay that.
 *
 * DECIDES NOTHING ITSELF. It defers to [AutoStartDecision], which is the same
 * gate MainActivity uses, so a phone woken by policy and a phone opened by hand
 * cannot reach different conclusions about whether to start relaying.
 */
class WakeReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val app = context.applicationContext

        // The door is unguarded (see the manifest for why the obvious guard is
        // platform-signed and unsatisfiable by any MDM). This is what stops it
        // being a way to spend somebody's battery: the work behind it is
        // idempotent, so dropping a repeat costs a real caller nothing.
        val now = System.currentTimeMillis()
        if (!WakeDebounce.shouldAnswer(lastAnsweredAtMs, now)) {
            Log.i(TAG, "wake: ignored, answered ${now - (lastAnsweredAtMs ?: 0)}ms ago")
            return
        }
        lastAnsweredAtMs = now
        // RECORDED BEFORE THE DECISION, and durably. The failure this exists to
        // end was diagnosed only by noticing that NOTHING had been written -
        // and logcat resets on the very reboot being investigated, so the
        // record has to outlive it.
        BootLog.record(app, TAG, "wake: received ${intent.action ?: "(no action)"}")

        // Mirror first: a phone woken straight after install has never synced,
        // so the locked-boot mirror is empty and the decision below would be
        // made against defaults rather than the policy just delivered.
        runCatching { BootConfigStore.sync(app) }
            .onFailure { Log.w(TAG, "wake: could not refresh boot config", it) }

        // effectiveConfig, NOT RelayConfiguration.load. load() reads stored
        // SharedPreferences only; the MDM writes RestrictionsManager, and
        // ManagedConfig.merge is what puts the second on top of the first.
        //
        // Reading load() here made this receiver stand down with "no usable
        // console configuration" on a handset whose platform held all six keys
        // - verified in dumpsys device_policy. The message was honest and the
        // question was asked of the wrong source. effectiveConfig's own doc
        // says it is "the single call site everything else should use, so no
        // code path can read stored configuration and miss the policy on top
        // of it - which would look like the MDM setting being ignored at
        // random". That is precisely what happened.
        val cfg = AndroidManagedConfig.effectiveConfig(app)
        val decision = AutoStartDecision.decide(
            managedAutoStart = AndroidManagedConfig.shouldAutoStart(app),
            alreadyRunning = RelayStatusStore.report.value != null,
            hasUsableConfig = cfg.consoleLanHost.isNotBlank(),
        )

        when (decision) {
            is AutoStartDecision.Start -> {
                BootLog.record(app, TAG, "wake: starting the relay")
                try {
                    app.startForegroundService(Intent(app, RelayService::class.java))
                } catch (e: Exception) {
                    // A background FGS start can be refused, and a caught,
                    // recorded refusal is worth more than a crash nobody sees.
                    BootLog.record(app, TAG, "wake: start refused - ${e.javaClass.simpleName}")
                    Log.e(TAG, "wake: startForegroundService failed", e)
                }
            }
            is AutoStartDecision.Stand ->
                BootLog.record(app, TAG, "wake: stood down - ${decision.reason}")
        }
    }

    companion object {
        private const val TAG = "ZippieWake"

        /** Process-scoped, which is the right lifetime: a wake that starts a
         *  fresh process is exactly the wake worth answering. */
        @Volatile
        private var lastAnsweredAtMs: Long? = null

        /** What muster sends. Named here so the contract lives beside the
         *  receiver that honours it rather than only in the MDM's policy. */
        const val ACTION_WAKE = "app.zippie.companion.action.WAKE"
    }
}
