package app.zippie.companion

import android.content.SharedPreferences
import kotlin.random.Random

/**
 * The key this phone announces itself under, and the words a person reads
 * beside it.
 *
 * THE NAME IS AN IDENTITY, NOT A LABEL. The router keys its leg table by this
 * string: announcing an existing name UPDATES that leg rather than adding one.
 * That is the good behaviour when a phone changes address, and a serious bug
 * when two different phones agree on a name - the second phone silently becomes
 * the first, inheriting its weight, tier and traffic. A bond that looked like it
 * had two legs would have one.
 *
 * That matters more here than on iOS, because `Build.MODEL` is a MODEL name, not
 * a device name: two Pixel 8s report the identical string. So the name is built
 * from two parts, exactly as LegName.swift does:
 *
 *   base   - from the model, for a human reading the leg list
 *   suffix - four hex characters, generated ONCE and persisted
 *
 * The suffix is persisted rather than re-derived so that a relaunch keeps the
 * same leg. A name that changed on every start would leave a trail of dead legs
 * behind it, each lingering for its lease.
 *
 * NOT DERIVED FROM A DEVICE IDENTIFIER. `ANDROID_ID` would be stable and would
 * also be a per-device identifier travelling to the router and into its metric
 * tags, for no gain over four random hex characters that nothing can correlate.
 */
object LegName {

    /**
     * The router's rule, which this must satisfy exactly (dynamic.py `_NAME_RE`):
     * `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$` - lowercase alphanumerics and hyphens,
     * first and last character alphanumeric, 2 to 32 characters. A name that
     * fails it is refused with a 400, so producing one here would mean a phone
     * that can never join.
     */
    const val MAX_LENGTH = 32
    const val MIN_LENGTH = 2

    /** Where the minted name lives, so it survives a restart of the service. */
    const val KEY = "legName"

    private const val FALLBACK = "phone"

    /** Characters that disappear rather than becoming a separator: the ASCII
     *  apostrophe and U+2019, the typographic one a user-set device name can
     *  carry. Written as an escape so this file stays plain ASCII. */
    private val ELIDED = setOf('\'', '\u2019')

    /**
     * Turn anything into a name the router will accept, or null if there is no
     * usable character in it at all.
     *
     * Real values that arrive here: "Pixel 8 Pro", "SM-G991B", and - if the
     * caller passes a user-set device name - apostrophes, emoji and non-Latin
     * scripts. The last two collapse to nothing, which is why [compose] has a
     * fallback instead of treating null as impossible.
     */
    fun sanitise(raw: String): String? {
        val out = StringBuilder()
        var lastWasHyphen = false
        for (c in raw.lowercase()) {
            if (c in 'a'..'z' || c in '0'..'9') {
                out.append(c)
                lastWasHyphen = false
            } else if (c in ELIDED) {
                // DROPPED, not turned into a separator: "Operator's Pixel" should
                // read "operators-pixel", not "operator-s-pixel" - both legal, one of
                // them reads as a mistake.
                continue
            } else if (out.isNotEmpty() && !lastWasHyphen) {
                // Collapse any run of junk into ONE hyphen, so "Pixel  8" does
                // not become "pixel--8".
                out.append('-')
                lastWasHyphen = true
            }
        }
        while (out.isNotEmpty() && out.last() == '-') out.deleteCharAt(out.length - 1)
        return if (out.isEmpty()) null else out.toString()
    }

    /**
     * Compose the final name, trimming the BASE rather than the whole string.
     *
     * Trimming the composed name would cut into the suffix, and a truncated
     * suffix is a collision waiting to happen - the very thing it exists to
     * prevent. A long model name loses its tail instead, which costs
     * readability and nothing else.
     */
    fun compose(base: String?, suffix: String): String {
        var head = base?.let { sanitise(it) } ?: FALLBACK
        val room = MAX_LENGTH - suffix.length - 1 // -1 for the joining hyphen
        if (head.length > room) head = head.substring(0, room)
        head = head.trimEnd('-')
        if (head.isEmpty()) head = FALLBACK
        return "$head-$suffix"
    }

    /**
     * Four hex characters. 65,536 values against a household's worth of phones:
     * the chance of a clash is small enough to ignore, and the consequence if it
     * happened is visible immediately (a leg whose label keeps changing) rather
     * than silent.
     */
    fun newSuffix(random: () -> Int = { Random.nextInt(0x10000) }): String =
        String.format("%04x", random() and 0xFFFF)

    /**
     * True if the router would accept this name. Written out rather than as a
     * regex copy so the two-character floor and the 32-character ceiling are
     * visible, but it is the same rule: a validator that is merely close would
     * pass names the router rejects at announce time, which fails on the device
     * and nowhere else.
     */
    fun isValid(name: String): Boolean {
        if (name.length < MIN_LENGTH || name.length > MAX_LENGTH) return false
        if (!isNameAlnum(name.first()) || !isNameAlnum(name.last())) return false
        return name.all { it == '-' || isNameAlnum(it) }
    }

    private fun isNameAlnum(c: Char): Boolean = c in 'a'..'z' || c in '0'..'9'

    /**
     * The stored name, or a freshly minted one on first use.
     *
     * PURE ON PURPOSE - the store is two lambdas rather than SharedPreferences,
     * because SharedPreferences is an android.jar stub that throws in a unit
     * test, and "does this phone keep the same leg across restarts" is precisely
     * the property worth proving off a device.
     */
    fun resolve(
        deviceName: String,
        read: () -> String?,
        write: (String) -> Unit,
        suffix: () -> String = { newSuffix() },
    ): String {
        val existing = read()
        if (existing != null && isValid(existing)) return existing
        val name = compose(deviceName, suffix())
        write(name)
        return name
    }

    fun resolve(prefs: SharedPreferences, deviceName: String): String = resolve(
        deviceName = deviceName,
        read = { prefs.getString(KEY, null) },
        write = { prefs.edit().putString(KEY, it).apply() },
    )
}

/**
 * What a person reads next to the leg in the router's list.
 *
 * THE ONE THING THIS PHONE KNOWS THAT AN iPHONE CANNOT. Apple removed
 * CTCarrier's carrier name in iOS 16, so the two iPhone legs are labelled by
 * hand in the router's config and keep saying "Verizon" long after a SIM swap.
 * Android reports the operator with no permission at all, so an Android leg can
 * arrive already labelled with the network it is actually spending.
 *
 * The label is sent on every announce, and the router writes it onto the leg
 * every tick - but an operator rename in legs.json is applied AFTER that, so a
 * rename still wins. Sending it repeatedly does not stamp on one.
 */
object LegLabel {

    /** The router truncates the label at 64 characters. Doing it here too means
     *  the cut is ours to choose rather than landing mid-carrier-name. */
    const val MAX_LENGTH = 64

    private const val FALLBACK = "Android phone"

    /**
     * The SERVING network, not the SIM's home carrier, when both are known: it
     * is the network this leg's bytes are actually going over and being billed
     * by. The SIM name is the fallback for a phone with no registration, which
     * is still better than no carrier at all - it says which phone this is.
     */
    fun forDevice(model: String, carrier: CarrierInfo?): String {
        val device = model.trim().ifEmpty { FALLBACK }
        val operator = carrier?.serving?.trim()?.ifEmpty { null }
            ?: carrier?.sim?.trim()?.ifEmpty { null }
        val label = if (operator == null) device else "$device ($operator)"
        return if (label.length <= MAX_LENGTH) label else label.substring(0, MAX_LENGTH).trimEnd()
    }
}
