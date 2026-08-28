import SwiftUI
import ZippieCompanionKit

/// The screen that answers the two-second question.
///
/// REFUSED, deliberately: the hero-metric template every bonding app ships -
/// enormous throughput number, small unit label, a row of supporting stats.
/// Throughput is not the question on a dashboard at 70mph, and a number cannot
/// say whether YOUR phone is one of the legs. The hero here is a sentence,
/// because the answer is a sentence.
///
/// Also refused: cards. This page is hairlines and space. A card around each
/// leg would add three edges and a shadow to say something the whitespace
/// already says, and nested cards would be worse.
struct BondScreen: View {
    @ObservedObject var model: BondModel

    var body: some View {
        // NAVIGATION LIVES HERE, not around the TabView. A stack per tab is
        // what keeps a push on Status from also pushing on Relay and Probe,
        // and it is why the tab bar stays put when a leg is opened.
        NavigationStack {
            content
                // TOP RIGHT, and reachable from the screen people already open.
                // A diagnostics screen buried behind a tab nobody visits is a
                // diagnostics screen that does not exist at the moment it is
                // needed - which is when the phone has gone quiet and the
                // operator is deciding whether to drive home.
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        NavigationLink {
                            NextDNSSettingsScreen()
                        } label: {
                            Image(systemName: "gearshape")
                                .accessibilityLabel("Settings")
                        }
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        NavigationLink {
                            // The router's HOST, not its URL. This name is
                            // shown to a human as "via suzu", so a full
                            // http://10.20.0.1:8787 would be noise in the one
                            // sentence that has to be read quickly.
                            DiagnosticsScreen(
                                consoleHost: Settings.consoleCandidates
                                    .compactMap { URL(string: $0.url)?.host }
                                    .first)
                        } label: {
                            Image(systemName: "stethoscope")
                                .accessibilityLabel("Diagnostics")
                        }
                    }
                }
        }
    }

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header
                // THE GLANCE ANSWER, above the detail that explains it. This
                // is the shape people expect from a bonding app, and the leg
                // rows below are what it decomposes into.
                if model.bond != nil {
                    SectionHead(title: "Throughput",
                                note: "Stacked by connection. The top edge is the total.")
                    BondThroughput(samples: model.series,
                                   order: model.seriesOrder,
                                   labels: model.legLabels)
                }
                legs
                if model.budget != nil { budget }
                footer
            }
            .padding(.horizontal, Space.margin)
            .padding(.bottom, Space.major)
        }
        .background(Ink.ground.ignoresSafeArea())
        .refreshable { await model.refresh() }
        // No large title: the headline sentence IS the title, and a navigation
        // title above it would say the same thing twice in less useful words.
        .navigationBarTitleDisplayMode(.inline)
    }

    // MARK: - the answer

    private var header: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            Text(model.headline)
                .font(Kind.display())
                // Tight tracking at display size; SF sets loose for UI by
                // default and a 40pt line at default tracking reads soft.
                .tracking(-0.8)
                .foregroundStyle(Ink.primary)
                .fixedSize(horizontal: false, vertical: true)

            Text(model.subhead)
                .font(Kind.body())
                .foregroundStyle(Ink.secondary)
                .fixedSize(horizontal: false, vertical: true)

            // WHICH JOB, directly under the state. The two modes are close to
            // opposites and a glance at the leg list cannot tell them apart:
            // on the router's network the bond has five legs, away from it
            // this phone IS the bond.
            Text(model.modeText)
                .font(Kind.caption())
                .foregroundStyle(Ink.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, Space.hair)
        }
        .padding(.top, Space.section)
        // SectionHead below adds its own top padding; matching it here stacked
        // two full sections into a dead band above the chart.
        .padding(.bottom, Space.tight)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    // MARK: - what backs it

    private var legs: some View {
        VStack(alignment: .leading, spacing: 0) {
            // The heading has to tell the truth about SCOPE. When the router
            // answers, these rows are the whole bond; when it does not, the
            // only row is this phone. One fixed heading would be wrong half
            // the time, and wrong in the flattering direction.
            Text(model.legsHeading)
                .font(Kind.section())
                .foregroundStyle(Ink.primary)
                .padding(.bottom, Space.snug)

            if model.legs.isEmpty {
                Text(model.emptyLegsMessage)
                    .font(Kind.body())
                    .foregroundStyle(Ink.secondary)
                    .padding(.vertical, Space.base)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ForEach(Array(model.legs.enumerated()), id: \.element.id) { index, leg in
                    if index > 0 { Hairline() }
                    // The row itself is the affordance. A separate chevron
                    // column or an edit button would add chrome to a screen
                    // whose whole point is being readable in two seconds from
                    // a dashboard.
                    NavigationLink {
                        LegDetail(leg: leg, model: model)
                    } label: {
                        LegRow(leg: leg)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    // MARK: - what it costs

    private var budget: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            Text("Session total")
                .font(Kind.section())
                .foregroundStyle(Ink.primary)

            if let b = model.budget {
                HStack(alignment: .firstTextBaseline, spacing: Space.tight) {
                    Text(b.usedText)
                        .font(Kind.figure(17, .medium))
                        .foregroundStyle(Ink.primary)
                    Text(b.ofText)
                        .font(Kind.body())
                        .foregroundStyle(Ink.secondary)
                }
                if let warning = b.warning {
                    Text(warning)
                        .font(Kind.caption())
                        .foregroundStyle(Ink.degraded)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.top, Space.section)
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: Space.tight) {
            Hairline()
                .padding(.bottom, Space.base)
            // Said once, plainly, because it is true and load-bearing: this is
            // why lending a phone's cellular is not a privacy question.
            Text("This phone forwards traffic without reading it.")
                .font(Kind.caption())
                .foregroundStyle(Ink.tertiary)
            Text(model.sourceText)
                .font(Kind.caption())
                .foregroundStyle(Ink.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            if let stamp = model.freshnessText {
                Text(stamp)
                    .font(Kind.caption())
                    .foregroundStyle(Ink.tertiary)
            }
        }
        .padding(.top, Space.section)
    }
}
