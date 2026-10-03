package app.zippie.companion

import android.content.Context
import androidx.glance.appwidget.GlanceAppWidgetManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * Asks the launcher to redraw the home-screen widget, when there is
 * something new to show (quadseven/zippie#176).
 *
 * A Glance widget redraws when we ask it to - there is no data binding - and
 * the relay publishes a report every ~2s for as long as it runs. Redrawing on
 * every publish would churn the launcher and burn battery for zero new
 * information, so the WHEN is [WidgetRefreshDecider.shouldRefresh]: the first
 * draw always happens, afterwards only when the visible content changed.
 *
 * Called from [RelayStatusStore.publish] (every heartbeat) and
 * [RelayStatusStore.clear] (clean stop - unthrottled by design, so a stop is
 * visible immediately rather than whenever the content next changes).
 *
 * Best-effort throughout, same posture as RelayReportFile: a launcher that
 * refuses the update, or a widget that was removed, must not be able to take
 * the heartbeat thread down with it.
 */
object ZippieWidgetRefresh {
    @Volatile private var lastContent: WidgetContent? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    fun maybeRefresh(context: Context, report: RelayReport?, nowMs: Long) {
        val content = WidgetContent.from(report, nowMs)
        if (!WidgetRefreshDecider.shouldRefresh(lastContent, content)) return
        lastContent = content
        val appContext = context.applicationContext
        scope.launch {
            try {
                val manager = GlanceAppWidgetManager(appContext)
                for (id in manager.getGlanceIds(ZippieWidget::class.java)) {
                    ZippieWidget().update(appContext, id)
                }
            } catch (e: Exception) {
                // Best-effort: see the class doc.
            }
        }
    }
}
