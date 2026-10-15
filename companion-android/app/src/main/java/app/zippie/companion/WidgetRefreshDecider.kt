package app.zippie.companion

/**
 * When the home-screen widget redraws, and when it must not.
 *
 * A Glance widget redraws when we ask it to - there is no data binding. The
 * relay publishes a report every ~2s for as long as it runs, so redrawing on
 * every publish would churn the launcher and burn battery for zero new
 * information. The rule:
 *
 * - the first draw always happens (a widget that has never drawn must draw,
 *   or the home screen shows nothing until something changes);
 * - afterwards, redraw if and only if the visible content actually changed.
 *
 * Split out for the same reason RelayReportFile is split from
 * RelayStatusStore: the Android half (performing the update through
 * GlanceAppWidgetManager) cannot be unit-tested, but the decision can and
 * must be - a widget that silently stops refreshing is the exact failure
 * mode rule 2 of #176 exists to close.
 *
 * [WidgetContent] is a data class, so equality is structural: any change to
 * the headline, the sentence, the tone, or the leg list counts. That is
 * deliberate - the tone dot is the glanceable signal and the sentence
 * carries the verdict's news, so either changing is something the home
 * screen must show.
 */
object WidgetRefreshDecider {
    fun shouldRefresh(last: WidgetContent?, new: WidgetContent): Boolean =
        last != new
}
