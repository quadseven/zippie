package app.zippie.companion

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.SystemClock
import android.os.UserManager
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Starts the relay after a reboot, without a human - including a phone that
 * is rebooted and never unlocked. #54.
 *
 * THE TRAP THIS CLOSES. On file-based-encryption devices, an ordinary
 * BOOT_COMPLETED receiver is not delivered until AFTER the user's first
 * unlock. A phone that drains flat in a car, gets power back from the
 * EcoFlow, boots, and sits at the lock screen because nobody is there to pick
 * it up would fire nothing, forever - it would work perfectly on a bench,
 * where someone always unlocks it, and silently do nothing in the one case it
 * exists for. So this receiver is marked android:directBootAware="true" and
 * listens for TWO broadcasts:
 *
 *  - LOCKED_BOOT_COMPLETED. Fires before unlock, delivered only to
 *    directBootAware components. This is the one that matters: the only entry
 *    point a phone left locked in a car ever gets. Everything it needs -
 *    the router's console address, the data budget - comes from
 *    BootConfigStore, which lives in DEVICE-PROTECTED storage rather than the
 *    credential-encrypted SharedPreferences RelayConfiguration normally uses,
 *    because that storage is not readable until first unlock either (see
 *    BootConfigStore's class doc for what that means for the console token).
 *  - BOOT_COMPLETED. Fires once per boot, at first unlock - immediately if
 *    the phone was never locked to begin with, or later if it was. Handled
 *    only as a SECOND CHANCE, and only if the relay is not already running
 *    (RelayStatusStore.report is in-memory and survives within the process
 *    LOCKED_BOOT_COMPLETED already started): it exists for what the locked
 *    pass can miss - the radio not up yet that early in boot, or a
 *    device-protected mirror that was never synced because the app has never
 *    been opened - now running against the REAL, credential-encrypted
 *    RelayConfiguration, which by this broadcast is guaranteed available.
 *
 * WHAT THIS DOES NOT DO. It does not make the relay ANNOUNCE before unlock -
 * the router console's write token is deliberately kept out of
 * device-protected storage (BootConfigStore) because it is a credential, not
 * configuration. A relay started by LOCKED_BOOT_COMPLETED forwards bytes but
 * stays invisible to the bond's leg list until the phone is unlocked AND the
 * service happens to (re)load its config with the token present -
 * RelayService reads RelayConfiguration exactly once, at its own start, and
 * this file does not restart an already-running instance to force a reload
 * (doing that safely needs a completion signal RelayService's current public
 * surface - ACTION_STOP plus a fresh start - does not provide, and adding one
 * is a RelayService.kt change outside this file's ownership). So: relays,
 * does not announce, until something else restarts it. Documented, not
 * silently accepted - see the PR for #54.
 *
 * THE FGS-START-FROM-BOOT EXEMPTION IS CONFIRMED, NOT ASSUMED (AC6). Both
 * BOOT_COMPLETED and LOCKED_BOOT_COMPLETED are named explicitly in Android's
 * list of broadcasts exempt from the background foreground-service-start
 * restriction, provided the start happens synchronously while the receiver is
 * still processing that broadcast (developer.android.com/develop/background-
 * work/services/fgs/restrictions-bg-start) - which is what [start] does,
 * inside the goAsync() window, before pending.finish() runs. Android 15 (API
 * 35) added a second, separate restriction that blocks BOOT_COMPLETED from
 * starting specific foreground service types outright - dataSync, camera,
 * mediaPlayback, phoneCall, mediaProjection, microphone
 * (developer.android.com/about/versions/15/behavior-changes-15#fgs-boot-
 * completed). RelayService's type, connectedDevice, is not on that list, and
 * the restriction only binds apps that TARGET API 35+. RE-CHECKED 2026-08-12
 * when targetSdk moved 34 -> 36 for Google Play (#140), which is exactly what
 * the previous version of this comment asked for: the blocked set is dataSync,
 * camera, mediaPlayback, phoneCall, mediaProjection and microphone.
 * connectedDevice is NOT among them, so this receiver may still start
 * RelayService from BOOT_COMPLETED. Re-check again if the service type changes.
 *
 * THE DECISION ITSELF LIVES IN BootRelayDecision, Android-free, so it can be
 * proven in a plain unit test. Everything in this file that reaches the
 * network or a real Context cannot be - this module's tests run against the
 * stub android.jar, where those calls throw "Stub!" rather than execute (see
 * DataBudgetTest, RouterGuardTest for the same shape).
 */
class BootReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "ZippieBoot"

        // Named, because [decide] now branches on which pass it is - and a
        // typo'd string literal there would silently stop refreshing the boot
        // mirror while still logging the right-looking source.
        private const val LOCKED_BOOT_COMPLETED = "LOCKED_BOOT_COMPLETED"
        private const val BOOT_COMPLETED = "BOOT_COMPLETED"

        /** Our own action, sent by the alarm we schedule for a retry (#176). */
        const val ACTION_RETRY = "app.zippie.companion.BOOT_RETRY"
        private const val EXTRA_ATTEMPT = "attempt"
        private const val RETRY = "RETRY"
        /** High enough that retryDelayMs returns its slow steady-state
         *  interval: supervision should poll every 15 minutes, not
         *  re-enter the fast boot ladder. */
        private const val SUPERVISION_ATTEMPT = 20

        /** Source label for a self-update restart (see the manifest). */
        const val PACKAGE_REPLACED = "PACKAGE_REPLACED"

        /**
         * Bounds the whole decision - the device-protected read plus the
         * router probe - inside the window a broadcast receiver may hold
         * goAsync() open for. BondStatusClient's own timeouts (1.5s local,
         * 4s remote, run CONCURRENTLY - see BondStatusClient.probe) sum to
         * well under this; the margin is for a cold boot's slower disk and
         * radio, not for the probe itself.
         */
        private const val DECISION_BUDGET_MS = 8_000L
    }

    override fun onReceive(context: Context, intent: Intent) {
        val appContext = context.applicationContext
        when (intent.action) {
            Intent.ACTION_LOCKED_BOOT_COMPLETED -> runDecision(appContext, LOCKED_BOOT_COMPLETED)

            ACTION_RETRY -> runDecision(appContext, RETRY,
                intent.getIntExtra(EXTRA_ATTEMPT, 1))
            Intent.ACTION_BOOT_COMPLETED -> {
                if (RelayStatusStore.report.value != null) {
                    Log.i(TAG, "$BOOT_COMPLETED: relay already running, nothing to do")
                    return
                }
                runDecision(appContext, BOOT_COMPLETED)
            }

            // An update is a restart this app was never told about. Treated
            // like BOOT_COMPLETED, including the "already running" check, so a
            // replace that somehow left the service alive does not start a
            // second one.
            Intent.ACTION_MY_PACKAGE_REPLACED -> {
                if (RelayStatusStore.report.value != null) {
                    Log.i(TAG, "$PACKAGE_REPLACED: relay already running, nothing to do")
                    return
                }
                runDecision(appContext, PACKAGE_REPLACED)
            }

            else -> Unit
        }
    }

    private fun runDecision(context: Context, source: String, attempt: Int = 1) {
        val pending = goAsync()
        // A FRESH, THROWAWAY SCOPE - NOT A CLASS-LEVEL ONE. Android creates a
        // new BootReceiver instance per broadcast and discards it as soon as
        // onReceive returns, so there is no owning lifecycle to scope a
        // coroutine to. goAsync()'s PendingResult is the only handle the
        // system gives back; finishing it in the `finally` below is what
        // tells Android this broadcast is done being processed, and
        // withTimeoutOrNull bounds how long that can take regardless.
        // RECORDED BEFORE ANY WORK, on disk, where a reboot cannot take it.
        // "The relay never started" and "the relay was never asked to start"
        // are different failures with different fixes, and until #186 nothing
        // on the phone could tell them apart after the fact.
        BootLog.record(context, "boot", "$source: decision started (attempt $attempt)")
        CoroutineScope(Dispatchers.IO).launch {
            try {
                withTimeoutOrNull(DECISION_BUDGET_MS) { decide(context, source, attempt) }
                    ?: run {
                        // A TIMEOUT IS THE COLD-BOOT CASE, not an oddity. The
                        // router is the thing being probed and it is still
                        // booting, so the probe having nothing to talk to is the
                        // EXPECTED outcome here - and giving up on it is the
                        // same defect as giving up on UNREACHABLE (#176), just
                        // in a different branch.
                        Log.w(TAG, "$source: boot decision timed out after " +
                            "${DECISION_BUDGET_MS}ms - treating as router-not-up-yet")
                        BootLog.record(context, "boot",
                            "$source: TIMED OUT after ${DECISION_BUDGET_MS}ms " +
                                "(attempt $attempt) - router not up yet, retrying")
                        scheduleRetry(context, source, attempt)
                    }
            } catch (e: Exception) {
                // A boot receiver that crashes is invisible on a locked phone
                // in a car - nobody is watching logcat. Caught and logged
                // rather than left to bring down the process.
                //
                // Retried for the same reason as the timeout: whatever threw,
                // the phone is now sitting with no relay and nothing else will
                // ever ask again.
                Log.e(TAG, "$source: boot decision failed", e)
                BootLog.record(context, "boot",
                    "$source: FAILED (attempt $attempt) ${e.javaClass.simpleName}: ${e.message}")
                runCatching { scheduleRetry(context, source, attempt) }
            } finally {
                pending.finish()
            }
        }
    }

    private suspend fun decide(context: Context, source: String, attempt: Int) {
        // A PHONE PROVISIONED BY MDM HAS NEVER BEEN OPENED, and BootConfigStore.sync
        // is otherwise called from MainActivity alone. So an enrolled phone arrives
        // here with an empty mirror, probes RelayConfiguration's DEFAULT addresses
        // rather than the consoleLanHost the operator pushed, fails the proximity
        // gate below, and skips - every boot, forever, while sitting on the router's
        // own LAN. Observed on the Pixel 6a on 2026-08-11: enrolled, configured,
        // carrying nothing until a human opened the app and tapped start.
        //
        // UNLOCKED, NOT BOOT_COMPLETED (#255). The reasoning below is right and
        // the test was wrong: sync reads the credential-encrypted ledger mirror,
        // which is unreadable before first unlock, so calling it on the LOCKED
        // pass would throw exactly where nobody is watching. That is a fact about
        // the LOCK STATE, and it was being asked as a question about WHICH
        // BROADCAST ARRIVED.
        //
        // The two are not the same, and the gap was a trap with no exit. A phone
        // that half-starts on LOCKED_BOOT_COMPLETED relays bytes without a token
        // and cannot announce. BOOT_COMPLETED - the second chance - returns early
        // because the relay is already running. Supervision then re-enters every
        // 15 minutes carrying source=RETRY, which is not BOOT_COMPLETED, so sync
        // was skipped on every pass forever. The phone was unlocked the whole
        // time and the one call that would have fixed it was gated on the name of
        // a broadcast that had already been and gone.
        //
        // STRICTLY ADDITIVE. If the lock state cannot be read the old rule
        // applies, so this can only ever create sync opportunities, never remove
        // one that exists today.
        val unlocked = runCatching {
            context.getSystemService(UserManager::class.java)?.isUserUnlocked
        }.getOrNull()
        if (unlocked ?: (source == BOOT_COMPLETED)) {
            runCatching { BootConfigStore.sync(context) }
                .onFailure { Log.w(TAG, "$source: could not refresh boot config mirror", it) }
        }
        val snapshot = BootConfigStore.read(context)
        val (_, proximity) = BondStatusClient.probe(
            snapshot.consoleCandidates,
            wifi = SystemWifiRoute(context),
        )
        val verdict = snapshot.budgetVerdict

        when (val outcome = BootRelayDecision.decide(proximity, verdict.isAllowed, verdict.reason)) {
            is BootRelayDecision.Start -> {
                start(context, source, proximity)
                // KEEP CHECKING AFTER A SUCCESSFUL START. BootReceiver was the
                // only thing that ever started the relay, so anything that
                // stopped it later - a crash, Android reclaiming the process, a
                // wifi flap at the wrong moment - left the phone powered on and
                // doing nothing until the next reboot. On a relay phone in a car
                // that is an outage nobody can see or clear.
                //
                // The retry chain already exists and already backs off to a slow
                // poll, so the supervision is the same alarm: re-enter, notice
                // the relay is gone, start it again. A running relay makes the
                // re-entry a no-op, since startForegroundService on a live
                // service just delivers another onStartCommand.
                BootLog.record(context, TAG, "$source: started (proximity=$proximity)")
                scheduleRetry(context, source, SUPERVISION_ATTEMPT)
            }
            is BootRelayDecision.Skip -> {
                Log.i(TAG, "$source: not starting - ${outcome.reason}")
                // DURABLE (#257). BootLog records "decision started", TIMED OUT
                // and FAILED, and neither OUTCOME - so the on-disk log could say
                // whether the relay was ASKED to start but never whether it
                // did. That is the exact question a cold boot asks, and logcat,
                // which had the answer, resets on the reboot being diagnosed.
                // A managed Pixel cost two hours on 2026-08-23 for want of this
                // line: zero records, and the absence was the only evidence.
                BootLog.record(
                    context, TAG,
                    "$source: stood down - ${outcome.reason} " +
                        "(retry ${if (outcome.retryable) "armed" else "NOT armed"})",
                )
                if (outcome.retryable) {
                    scheduleRetry(context, source, attempt, outcome.retryAfterMs)
                }
            }
        }
    }

    /**
     * Ask again later, because the router may simply not be up yet (#176).
     *
     * An inexact `setAndAllowWhileIdle` alarm rather than an exact one: exact
     * alarms need SCHEDULE_EXACT_ALARM on API 31+, and nothing here needs
     * to-the-second timing - it needs to happen at all, including in Doze,
     * which is what allowWhileIdle buys.
     *
     * The attempt number rides in the Intent rather than in storage, so there
     * is no state to leave stale if the phone reboots mid-sequence - a reboot
     * legitimately restarts the whole question from attempt 1.
     */
    private fun scheduleRetry(
        context: Context,
        source: String,
        attempt: Int,
        overrideDelayMs: Long? = null,
    ) {
        // An override beats the ladder, because the ladder encodes ONE story:
        // a router that has not finished booting, worth asking about in 15
        // seconds. A data cap is a different story on a different clock and
        // climbing a boot ladder toward it just wakes the radio (#256).
        val delay = overrideDelayMs ?: BootRelayDecision.retryDelayMs(attempt)
        if (delay == null) {
            // Only attempt 0, which is not a retry. The schedule no longer ends:
            // a hung router is not an absent one, and there is nobody here to
            // tap the phone afterwards.
            return
        }
        val next = Intent(context, BootReceiver::class.java).apply {
            action = ACTION_RETRY
            putExtra(EXTRA_ATTEMPT, attempt + 1)
        }
        val pi = PendingIntent.getBroadcast(
            context, 0, next,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val am = context.getSystemService(AlarmManager::class.java)
        if (am == null) {
            Log.w(TAG, "$source: no AlarmManager; cannot retry")
            return
        }
        am.setAndAllowWhileIdle(
            AlarmManager.ELAPSED_REALTIME_WAKEUP,
            SystemClock.elapsedRealtime() + delay,
            pi,
        )
        Log.i(TAG, "$source: retrying in ${delay / 1000}s (attempt ${attempt + 1})")
    }

    private fun start(context: Context, source: String, proximity: RouterProximity) {
        try {
            // A FROZEN RELAY IS A RUNNING RELAY, and startForegroundService on
            // a live service only delivers another onStartCommand - which is
            // why supervision has been waking every 15 minutes, confirming the
            // broken state and changing nothing. Stop it first so the start
            // below is a real start.
            //
            // Only when the heartbeat has genuinely stopped: restarting a
            // healthy relay would drop the bond's leg for no reason, so
            // RelayLiveness deliberately waits longer than the screen does.
            val liveness = RelayLiveness.evaluate(
                RelayStatusStore.report.value, System.currentTimeMillis())
            if (liveness is RelayLiveness.Frozen) {
                Log.w(TAG, "$source: relay has not reported for ${liveness.quietForMs}ms " +
                    "- stopping before start, because a no-op onStartCommand cannot revive it")
                context.startService(
                    Intent(context, RelayService::class.java)
                        .setAction(RelayService.ACTION_STOP))
            }
            context.startForegroundService(Intent(context, RelayService::class.java))
            Log.i(TAG, "$source: starting relay, proximity=$proximity, liveness=$liveness")
        } catch (e: Exception) {
            // ForegroundServiceStartNotAllowedException (API 31+) is exactly
            // what the class doc's exemption reasoning exists to prevent, and
            // this is the backstop if that reasoning is ever wrong on a real
            // device: a caught, logged failure on a locked phone rather than
            // an app record that simply looks dead.
            Log.e(TAG, "$source: startForegroundService failed", e)
        }
    }
}
