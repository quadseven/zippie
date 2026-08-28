package app.zippie.companion

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.unit.dp
import app.zippie.companion.design.Ink
import app.zippie.companion.design.Kind
import app.zippie.companion.design.Space
import kotlin.math.max

// CHART GEOMETRY, NOT SPACING. Space's smallest step is 4dp because it is a
// scale for layout rhythm, and a 4dp bar with a 4dp gap fits about forty bars
// across a phone - which would turn a twenty-minute window into a chart of ten
// minutes and pretend the rest never happened. These three numbers are the mark
// itself, the way a stroke width is, and borrowing the spacing scale for them
// would be using a token because one exists rather than because it means this.
private val BarWidth = 3.dp
private val BarGap = 1.dp
private val ChartHeight = 72.dp
private val LegendDot = 6.dp

/**
 * The bond's throughput over time, stacked by connection.
 *
 * All the arithmetic is in BondThroughput, which is plain Kotlin with unit
 * tests behind it; this file only paints. That split is not tidiness - there is
 * no Android SDK on the machine most of this is written on, so anything worth
 * proving has to be provable without one.
 *
 * A CANVAS RATHER THAN A ROW OF BOXES. Ninety bars of five stacked segments is
 * four hundred and fifty composables re-measured every five seconds for a
 * shape that is nine rectangles' worth of information. One draw pass costs
 * nothing and cannot drop a frame on a phone that is also relaying packets.
 */
@Composable
fun BondThroughputChart(chart: BondThroughput.Chart, modifier: Modifier = Modifier) {
    Column(modifier) {
        if (!chart.hasTraffic) {
            // NOT AN EMPTY CHART. A blank axis reads as "broken"; words read as
            // "nothing is moving", which is a different and often correct state.
            Text(
                chart.emptyMessage,
                style = Kind.caption,
                color = Ink.tertiary,
                modifier = Modifier.padding(vertical = Space.base),
            )
            return@Column
        }
        Bars(chart)
        Spacer(Modifier.height(Space.tight))
        Legend(chart)
    }
}

@Composable
private fun Bars(chart: BondThroughput.Chart) {
    // Resolved OUTSIDE the draw lambda: Ink's getters are @Composable, and a
    // DrawScope is not a composition. Reading them here also means a theme
    // change repaints the chart, which it would not if they were captured once.
    val palette = palette()
    val rule = Ink.rule
    val summary = chart.accessibilitySummary
    Canvas(
        Modifier
            .fillMaxWidth()
            .height(ChartHeight)
            // The bars are decoration to a screen reader; the sentence is the
            // content. Without this, TalkBack announces an unlabelled graphic.
            .clearAndSetSemantics { contentDescription = summary },
    ) {
        val slot = (BarWidth + BarGap).toPx()
        val visible = max(1, (size.width / slot).toInt())
        val shown = chart.bars.takeLast(visible)
        // NEWEST AT THE RIGHT EDGE, and a short history starts partway across
        // rather than being stretched to fit. A chart that always fills its
        // width hides how much of the window has actually been observed.
        val startX = max(0f, size.width - shown.size * slot)
        val barWidth = BarWidth.toPx()

        shown.forEachIndexed { index, bar ->
            val x = startX + index * slot
            // Nothing is known about this interval - the app was not looking.
            // Drawn as literally nothing, so the gap in the record is visible
            // as a gap.
            if (!bar.measured) return@forEachIndexed
            if (bar.total <= 0.0) {
                // Measured, and nothing moved. A mark on the baseline, because
                // "we watched and it was quiet" is a fact worth having on the
                // chart and is not the same as the blank above.
                drawRect(
                    color = rule,
                    topLeft = Offset(x, size.height - 1f),
                    size = Size(barWidth, 1f),
                )
                return@forEachIndexed
            }
            // Stacked from the baseline up, in the router's priority order, so
            // the leg that is meant to be doing the work sits on the bottom and
            // does not jump up and down the stack as the ones above it change.
            var y = size.height
            chart.order.forEachIndexed { legIndex, leg ->
                val bps = bar.slices.firstOrNull { it.leg == leg }?.bps
                if (bps != null && bps > 0) {
                    // FLOORED AT ONE PIXEL. A leg carrying half a percent of the
                    // peak rounds to a zero-height rectangle and disappears,
                    // which on this screen reads as "that leg is doing nothing"
                    // - the one sentence the whole app exists to stop being
                    // wrong about.
                    val h = max((size.height * (bps / chart.peakBps)).toFloat(), 1f)
                    y -= h
                    drawRect(
                        color = palette[legIndex % palette.size],
                        topLeft = Offset(x, y),
                        size = Size(barWidth, h),
                    )
                }
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun Legend(chart: BondThroughput.Chart) {
    val palette = palette()
    // FLOWING, NOT ONE ROW. Four legs on one line truncated every name to "Co-operator
    // iP..." and "Repeat...", which identifies nothing - a legend whose labels
    // are unreadable is decoration. Wrapping costs one line and keeps the names
    // whole.
    FlowRow(
        horizontalArrangement = Arrangement.spacedBy(Space.base),
        verticalArrangement = Arrangement.spacedBy(Space.hair),
    ) {
        // ONLY THE LEGS THAT CARRIED. A key entry for a leg that contributed no
        // pixels sends someone hunting the chart for a colour that is not on it.
        chart.carrying.forEach { leg ->
            val index = chart.order.indexOf(leg)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Spacer(
                    Modifier
                        .size(LegendDot)
                        .background(palette[index.coerceAtLeast(0) % palette.size], CircleShape),
                )
                Spacer(Modifier.width(Space.hair))
                Text(chart.label(leg), style = Kind.caption, color = Ink.secondary)
            }
        }
    }
    Text("peak ${Fmt.rate(chart.peakBps)}", style = Kind.figure(12), color = Ink.tertiary)
}

/**
 * Colour by POSITION in the chart's own stable order, not by hashing the name -
 * a hash gives two legs the same colour often enough to matter with five of
 * them.
 *
 * Spending `live` on the first leg is the one place in the language where it
 * does not mean "carrying right now" - and it still nearly does: order is the
 * router's priority order, so the first leg is the one meant to be doing the
 * work, and only legs that actually carried appear in the legend at all.
 */
@Composable
private fun palette(): List<Color> =
    listOf(Ink.live, Ink.degraded, Ink.down, Ink.secondary, Ink.tertiary)
