package app.zippie.companion

import java.lang.reflect.InvocationTargetException

/**
 * [PeerCrypto] backed by the gomobile binding, so every trust decision is the
 * Go `ClientRegistry`'s (`mobile/discovery.go`).
 *
 * REFLECTIVE, because zippie.aar is not a build dependency yet - its
 * `implementation(files("libs/zippie.aar"))` line in build.gradle.kts is
 * commented out until the Android bind is wired into the build. A direct call
 * would not compile without it. Reflection lets the discovery code ship now and
 * use the binding the moment the aar is on the classpath, with no other change.
 *
 * WITHOUT THE BINDING NOTHING IS VERIFIED. [unavailable] says so, and
 * NsdPeerDiscovery stops there without advertising; every call throws, so a
 * caller that went on anyway would still refuse every device - the fail-closed
 * answer, never a stand-in that trusts the advertisement. When the aar is
 * wired, a release build that shrinks code must keep `mobile.Mobile`.
 *
 * gomobile's Java shape: package `mobile`, class `Mobile`, static methods named
 * in lower camel case, Go `int` as a primitive `long`, Go errors as Exception.
 */
class GoPeerCrypto(bindingClass: String = "mobile.Mobile") : PeerCrypto {

    /** The binding, or why there is none. A missing class and a class whose
     *  native library will not load are the same answer: nothing can be
     *  verified, said once, up front, by [unavailable]. */
    private val loaded: Result<Class<*>> = try {
        Result.success(Class.forName(bindingClass))
    } catch (e: ClassNotFoundException) {
        Result.failure(e)
    } catch (e: LinkageError) {
        // UnsatisfiedLinkError or ExceptionInInitializerError from the static
        // initializer that loads the .so: fail closed, never crash the app.
        Result.failure(e)
    }

    override val unavailable: String? = loaded.exceptionOrNull()?.let {
        "this build has no datapath binding, so it can neither prove its pairing nor verify anyone else's ($it)"
    }

    override fun newChallenge(): String = call("newPeerChallenge")

    override fun proof(credentialJson: String, challengeHex: String, endpoint: String): String =
        call("peerProof", credentialJson, challengeHex, endpoint)

    override fun verify(rosterJson: String, claimedId: Long, challengeHex: String, proofHex: String): String =
        call("verifyPeer", rosterJson, claimedId, challengeHex, proofHex)

    private fun call(name: String, vararg args: Any): String {
        val cls = loaded.getOrElse { throw IllegalStateException(unavailable, it) }
        val types = args.map { if (it is Long) java.lang.Long.TYPE else it.javaClass }.toTypedArray()
        val method = cls.getMethod(name, *types)
        return try {
            method.invoke(null, *args) as String
        } catch (e: InvocationTargetException) {
            // The binding's own refusal ("unknown client", "frame failed
            // authentication") is the reason worth showing, not the wrapper.
            throw (e.cause as? Exception) ?: e
        }
    }
}
