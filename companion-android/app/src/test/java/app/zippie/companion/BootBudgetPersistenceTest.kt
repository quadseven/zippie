package app.zippie.companion

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pins the only ledger state boot must preserve: a budget that has hit its cap. */
class BootBudgetPersistenceTest {
    private fun source(path: String): String {
        val file = File(path)
        assertTrue("expected $path from ${File(".").absolutePath}", file.isFile)
        return file.readText()
    }

    @Test
    fun `an exhausted ledger remains exhausted after serialization and restore`() {
        val budget = DataBudget(dailyBytes = 100)
        val spent = BudgetLedger(budget).apply { record(100) }
        val restored = BudgetLedgerCodec.decode(BudgetLedgerCodec.encode(spent), budget)

        assertFalse("the restored boot ledger must retain the exhausted cap", restored!!.verdict().isAllowed)
    }

    @Test
    fun `saving an exhausted ledger also updates the device protected boot mirror`() {
        val service = source("src/main/java/app/zippie/companion/RelayService.kt")
        val save = service.substringAfter("private fun saveLedger()")
            .substringBefore("// ---- foreground plumbing")
        assertTrue(
            "an exhausted ledger saved before reboot must reach BootConfigStore's " +
                "device-protected mirror",
            save.contains("BootConfigStore.persistExhaustedLedger"),
        )

        val store = source("src/main/java/app/zippie/companion/BootConfigStore.kt")
        assertTrue(
            "the boot mirror must use device-protected storage",
            store.contains("fun persistExhaustedLedger") &&
                store.contains("createDeviceProtectedStorageContext()"),
        )
    }
}
