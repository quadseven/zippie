import SwiftUI
import ZippieCompanionKit

/// Editing one leg.
///
/// THE SCREEN IS ORDERED BY BLAST RADIUS, not by the shape of the JSON. Tier
/// leads because it is the only control here that can take a leg out of service
/// entirely, and because it is the one people reliably misread: it is a HARD
/// GATE, not a ranking. Weight and priority follow, then the absolute ceiling,
/// then the monthly cap - which is the field this screen mainly exists for,
/// because the router can only measure what a leg carried and only the provider
/// knows what the plan allows.
///
/// NOTHING IS EVER DRAWN AS APPLIED BEFORE THE ROUTER SAYS SO. The left-hand
/// values are the router's; the fields are text somebody typed. They converge
/// only after a successful write and a reread. There is no spinner-then-assume
/// anywhere in this file.
///
/// No cards, same as everywhere else: hairlines and space.
struct LegEditor: View {
    @StateObject private var model: LegEditorModel
    @State private var confirmingClear = false

    init(legName: String, legLabel: String) {
        _model = StateObject(wrappedValue: LegEditorModel(legName: legName,
                                                          legLabel: legLabel))
    }

    var body: some View {
        Page {
            header

            if let leg = model.reported {
                tier(leg)
                share(leg)
                ceiling(leg)
                cap(leg)
                plan
                token
                commit
            } else {
                unavailable
            }
        }
        .task { await model.load() }
    }

    // MARK: - which leg, and what it is doing

    private var header: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            Text(model.legLabel.isEmpty ? model.legName : model.legLabel)
                .font(Kind.display())
                .tracking(-0.8)
                .foregroundStyle(Ink.primary)
                .fixedSize(horizontal: false, vertical: true)

            // The stable identifier, said once. `label` is free text and two
            // legs can share one; this is the name the router keys everything
            // by and the name that appears in any error it returns.
            Text(model.legName)
                .font(Kind.figure(13))
                .foregroundStyle(Ink.tertiary)

            // RENAMING WAS SUPPORTED EVERYWHERE EXCEPT HERE. `label` is in the
            // router's overridable set and in the Kit's edit payload, and the
            // one surface a person can reach did not render it - so the name
            // could only be changed by hand-editing the router's config. It
            // sits at the top because a wrong name is what sends someone to
            // this screen in the first place.
            FieldRow(label: "Name", text: model.binding(for: .label))

            Text(standing)
                .font(Kind.body())
                .foregroundStyle(Ink.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, Space.section)
        .padding(.bottom, Space.roomy)
        .accessibilityElement(children: .combine)
    }

    /// What the router says about this leg right now, in a sentence. Every
    /// clause is a measured fact; the ones it cannot measure are left out
    /// rather than filled in.
    private var standing: String {
        guard let leg = model.reported else {
            return model.loading ? "Reading the router." : "The router is not answering."
        }
        var parts: [String] = []
        if let state = leg.state, !state.isEmpty { parts.append("Reported \(state)") }
        if let tier = leg.tier { parts.append("tier \(tier)") }
        if let inBond = leg.inBond {
            parts.append(inBond ? "carrying for the bond" : "not in the bond")
        }
        return parts.isEmpty ? "The router published nothing about this leg."
                             : parts.joined(separator: ", ") + "."
    }

    /// The form is WITHHELD when the router cannot be read.
    ///
    /// Editing blind is worse than not editing: the operator would be
    /// correcting numbers they never saw, against a leg that may have moved.
    private var unavailable: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            if model.loading {
                Text("Reading the router...")
                    .font(Kind.body())
                    .foregroundStyle(Ink.secondary)
            } else {
                Note(text: model.loadError ?? "The router did not answer.", tone: .bad)
                Note(text: "Nothing can be changed until this leg's current "
                   + "settings can be read. Editing without them would mean "
                   + "overwriting values nobody has seen.")
                ActionButton(title: "Try again", role: .quiet) {
                    Task { await model.load() }
                }
                .padding(.top, Space.base)
            }
        }
        .padding(.top, Space.roomy)
    }

    // MARK: - tier, the hard gate

    private func tier(_ leg: LegEditSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Tier")

            // WORDED AGAINST THE OBVIOUS MISREADING. Every other bonding UI
            // calls this a preference or a priority, and a reader who imports
            // that meaning will set a tier expecting "use it less" and get "do
            // not use it at all" - which is invisible until the day the leg was
            // supposed to help.
            Note(text: "Tier is a hard gate. A leg in a higher-numbered tier "
               + "carries NOTHING while any leg in a lower-numbered tier is "
               + "alive - it is not used less, it is not used at all. That is "
               + "how a cheap emergency SIM stays untouched in reserve until "
               + "everything above it has failed.")
                .padding(.bottom, Space.base)

            TierChoice(selection: model.binding(for: .tier),
                       current: leg.tier)

            Hairline().padding(.top, Space.base)
            Readout(label: "Tier on the router now",
                    value: leg.tier.map { "\($0)" } ?? "unknown")
        }
    }

    // MARK: - share within the tier

    private func share(_ leg: LegEditSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Within its tier")

            FieldRow(label: "Weight", text: model.binding(for: .weight), keyboard: .numberPad)
            Hairline()
            FieldRow(label: "Priority", text: model.binding(for: .priority), keyboard: .numberPad)
            Hairline()
            // NOT EDITABLE AND SHOWN ANYWAY. The policy scales the configured
            // weight by health and cost every second, so this number is
            // routinely nothing like the one that was typed - and an editor
            // that showed only the typed value would leave someone convinced
            // their change had not taken.
            Readout(label: "Effective weight now",
                    value: leg.effectiveWeight.map { "\($0)" } ?? "unknown")

            Note(text: "Weight is a SHARE: this leg's portion of the traffic "
               + "the tier is carrying, against the other legs in the same "
               + "tier. Priority orders legs within a tier and never promotes "
               + "one across tiers. Effective weight is what the router "
               + "computed from the weight below after health and cost - it is "
               + "not typed here.")
        }
    }

    // MARK: - the absolute ceiling

    private func ceiling(_ leg: LegEditSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Ceiling")

            FieldRow(label: "Kilobits per second",
                     text: model.binding(for: .maxKbps), keyboard: .numberPad)
            Hairline()
            // "none" and "unknown" are different answers. A router too old to
            // publish max_kbps has not told us there is no ceiling - it has
            // told us nothing, and saying "none" there would be this app
            // inventing a fact on the router's behalf.
            Readout(label: "Ceiling on the router now",
                    value: leg.maxKbps == nil
                        ? "unknown"
                        : (leg.ceilingKbps.map { "\($0) kbit/s" } ?? "none"))

            Note(text: "A hard cap in kilobits per second, enforced at the last "
               + "point before bytes leave. This is NOT the same as a low "
               + "weight: weight is a share, so a small share of a busy bond is "
               + "still real volume and on a 5 GB plan that is the whole month. "
               + "This is an absolute limit nothing routes around. Enter 0 for "
               + "no ceiling.")
        }
    }

    // MARK: - measured against claimed

    private func cap(_ leg: LegEditSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Monthly cap")

            // THE REASON THIS SCREEN EXISTS, said plainly. The router can only
            // ever know what it measured; the number that matters for a bill is
            // the one the provider will enforce, and only a person can supply
            // that.
            Note(text: "The router measures what this leg has carried. Only "
               + "your provider knows what the plan actually allows. Put the "
               + "provider's number here so the cap the router enforces is the "
               + "real one rather than a guess from setup day.")
                .padding(.bottom, Space.base)

            FieldRow(label: "Cap (GB)", text: model.binding(for: .monthlyCapGB),
                     keyboard: .decimalPad)
            Hairline()

            // Measured first, because it is the half nobody can argue with.
            Readout(label: "Measured this month",
                    value: leg.usageGB.map { String(format: "%.2f GB", $0) }
                        ?? "not measured yet")
            Hairline()
            Readout(label: "Cap on the router now",
                    value: leg.monthlyCapGB == nil
                        ? "unknown"
                        : (leg.capGB.map { "\(LegEditorModel.plain($0)) GB" } ?? "no cap set"))

            if let remaining = leg.remainingGB {
                Hairline()
                Readout(label: remaining < 0 ? "Over by" : "Left",
                        value: String(format: "%.2f GB", abs(remaining)),
                        tone: remaining < 0 ? Ink.down : Ink.primary)
            }

            // Drawn only when BOTH halves are real. A bar against an unset cap
            // would be a picture of a number nobody has.
            if let used = leg.usedFraction {
                CapBar(used: used, overSoftLimit: leg.overSoftLimit ?? false)
                    .padding(.top, Space.base)
            } else if leg.monthlyCapGB == nil {
                Note(text: "The router did not report a cap for this leg, so "
                   + "there is nothing to measure against.")
            } else if leg.capGB == nil {
                Note(text: "No cap is set, so there is nothing to measure "
                   + "against. Enter what the provider allows.")
            } else {
                Note(text: "Nothing has been measured for this leg yet.")
            }
        }
    }

    // MARK: - what the leg actually is

    private var plan: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Plan details")

            FieldRow(label: "Carrier", text: model.binding(for: .carrier))
            Hairline()
            FieldRow(label: "Plan", text: model.binding(for: .planName))
            Hairline()
            FieldRow(label: "Plan type", text: model.binding(for: .planType))
            Hairline()
            FieldRow(label: "Billing day", text: model.binding(for: .billingDay),
                     keyboard: .numberPad)

            // The blankness is not a bug and saying so is cheaper than someone
            // discovering it and assuming the save failed.
            Note(text: "Descriptive only - none of this changes routing. The "
               + "router does not publish these back in its status, so they "
               + "start blank every time this screen opens. What it stored is "
               + "listed under Saved below, straight from its reply.")
        }
    }

    // MARK: - the credential

    private var token: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Write token")

            Readout(label: "On this phone",
                    value: model.hasToken ? "stored" : "not stored",
                    tone: model.hasToken ? Ink.primary : Ink.degraded)
            Hairline()
            SecretRow(label: "Token", text: $model.tokenEntry)

            HStack(spacing: Space.snug) {
                ActionButton(title: "Store token", role: .quiet,
                             enabled: !model.tokenEntry.isEmpty) {
                    model.saveToken()
                }
                if model.hasToken {
                    ActionButton(title: "Forget", role: .quiet) { model.forgetToken() }
                }
            }
            .padding(.top, Space.snug)

            Note(text: "Reads are open; writes are not. The router generates "
               + "this token on first use and keeps it as console_token in its "
               + "state directory. It is held in this phone's keychain and is "
               + "never shown again once stored.")
        }
    }

    // MARK: - commit

    private var commit: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            SectionHead(title: "Save")

            // The pending list is the anti-optimism device: until this is
            // empty, the values above are what the operator typed and not what
            // the router has.
            if model.changed.isEmpty {
                Text("Nothing has been changed.")
                    .font(Kind.body())
                    .foregroundStyle(Ink.secondary)
            } else {
                Text("Waiting to be sent: " + model.changed.map(LegEditorModel.title)
                        .joined(separator: ", "))
                    .font(Kind.body())
                    .foregroundStyle(Ink.degraded)
                    .fixedSize(horizontal: false, vertical: true)
            }

            ActionButton(title: "Save to the router",
                         busy: model.outcome == .working,
                         enabled: model.canSave) {
                Task { await model.save() }
            }

            outcome

            if let receipt = model.receipt, !receipt.applied.isEmpty {
                saved(receipt)
            }

            ActionButton(title: "Remove overrides", role: .quiet,
                         enabled: model.outcome != .working) {
                confirmingClear = true
            }
            .padding(.top, Space.base)
            Note(text: "Removes the tier, weight, priority, ceiling and cap set "
               + "from this app, so the router's own configuration file decides "
               + "again. Setting a value back to 0 by hand does not do this - "
               + "the override stays in place and keeps shadowing the file.")
        }
        .confirmationDialog("Remove this leg's overrides?",
                            isPresented: $confirmingClear, titleVisibility: .visible) {
            Button("Remove overrides", role: .destructive) {
                Task { await model.clearOverrides() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The router falls back to zippie.toml for this leg's tier, "
               + "weight, priority, ceiling and cap.")
        }
    }

    @ViewBuilder
    private var outcome: some View {
        switch model.outcome {
        case .none, .working:
            EmptyView()
        case .failed(let why):
            // A failed write is a FAILURE on screen, in the failure ink, with
            // the router's own reason. No toast, nothing that disappears.
            Note(text: why, tone: .bad)
        case .applied(let confirmed, let unconfirmed):
            if unconfirmed.isEmpty {
                Note(text: "The router applied "
                   + confirmed.map(LegEditorModel.title).joined(separator: ", ")
                   + " and the values above were read back from it.")
            } else {
                // A 200 whose echo is missing a field is a PARTIAL write. It
                // reads as success from the status code alone, which is exactly
                // why it is drawn as a failure here.
                Note(text: "The router did not confirm "
                   + unconfirmed.map(LegEditorModel.title).joined(separator: ", ")
                   + ". Those are NOT applied. Check the router's own console "
                   + "before assuming otherwise.", tone: .bad)
            }
        }
    }

    /// The router's receipt, verbatim. This is the only place the descriptive
    /// fields can be seen at all - nothing in /api/status carries them.
    private func saved(_ receipt: LegEditReceipt) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Saved", note: "What the router said it stored.")
            ForEach(receipt.applied.keys.sorted { $0.rawValue < $1.rawValue }, id: \.self) { field in
                Readout(label: LegEditorModel.title(field),
                        value: receipt.value(field)?.text ?? "unknown")
                Hairline()
            }
        }
    }
}

/// The tier control.
///
/// BUTTONS RATHER THAN A TEXT FIELD, because the reader has to see that the
/// tiers are an ordered set of gates and that they are picking a position in
/// it. A number pad makes tier 3 feel like a magnitude.
private struct TierChoice: View {
    @Binding var selection: String
    /// What the router reports. Included in the offered set even when it is
    /// outside the usual range, so a leg configured at tier 7 can still be seen
    /// and edited rather than silently re-tiered by opening this screen.
    let current: Int?

    private var options: [Int] {
        var values = Set(1...4)
        if let current { values.insert(current) }
        return values.sorted()
    }

    var body: some View {
        HStack(spacing: Space.tight) {
            ForEach(options, id: \.self) { tier in
                let picked = selection == "\(tier)"
                Button { selection = "\(tier)" } label: {
                    Text("\(tier)")
                        .font(Kind.figure(17, .medium))
                        .foregroundStyle(picked ? Ink.raised : Ink.primary)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, Space.snug)
                        .background(picked ? Ink.live : .clear)
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .strokeBorder(picked ? .clear : Ink.rule)
                        )
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Tier \(tier)")
                .accessibilityAddTraits(picked ? [.isSelected] : [])
            }
        }
    }
}

/// A password field in the same column as every other value on the page.
///
/// SecureField rather than FieldRow, because this is the one value here that is
/// a credential and shoulder-surfing a router token on a train is a real way to
/// lose control of a household's routing.
private struct SecretRow: View {
    let label: String
    @Binding var text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.base) {
            Text(label)
                .font(Kind.body())
                .foregroundStyle(Ink.secondary)
            Spacer(minLength: Space.snug)
            SecureField("", text: $text)
                .font(Kind.figure(17))
                .foregroundStyle(Ink.primary)
                .multilineTextAlignment(.trailing)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
        }
        .padding(.vertical, Space.snug)
    }
}

/// Measured usage against the stated cap.
///
/// REAL DATA, both ends. Drawn only when the cap is set and usage has been
/// measured - a track against an unset cap would be decoration, and this app
/// has no budget for bars that are not measuring something.
private struct CapBar: View {
    let used: Double
    let overSoftLimit: Bool

    private var tint: Color {
        if used >= 1 { return Ink.down }
        return overSoftLimit ? Ink.degraded : Ink.live
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.tight) {
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule(style: .continuous)
                        .fill(Ink.rule)
                        .frame(height: 3)
                    Capsule(style: .continuous)
                        .fill(tint)
                        // Clamped for DRAWING only. The number beside the bar
                        // still reports the overage; a bar cannot show 140% of
                        // itself and pretending otherwise would clip the label
                        // rather than the truth.
                        .frame(width: min(1, max(0, used)) * geo.size.width, height: 3)
                }
            }
            .frame(height: 3)

            Text(String(format: "%.0f%% of the stated cap", used * 100))
                .font(Kind.figure(13))
                .foregroundStyle(used >= 1 ? Ink.down : Ink.secondary)
        }
    }
}
