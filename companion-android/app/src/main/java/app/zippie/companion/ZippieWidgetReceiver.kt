package app.zippie.companion

import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver

/**
 * The manifest entry point for the home-screen widget. The launcher talks
 * to this; all rendering lives in [ZippieWidget].
 */
class ZippieWidgetReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = ZippieWidget()
}
