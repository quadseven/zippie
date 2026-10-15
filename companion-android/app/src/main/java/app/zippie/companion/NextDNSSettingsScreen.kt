package app.zippie.companion

import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.provider.Settings
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp

private const val PRIVATE_DNS_SETTINGS = "android.settings.PRIVATE_DNS_SETTINGS"

/** Send the user to Android's Private DNS settings with a hostname to paste. */
@Composable
fun NextDNSSettingsScreen(onBack: () -> Unit) {
    val context = LocalContext.current
    val saved = remember { RelayConfiguration.load(context) }
    var profileId by remember { mutableStateOf(saved.nextDNSProfileId) }
    var deviceName by remember { mutableStateOf(saved.nextDNSDeviceName) }
    var error by remember { mutableStateOf<String?>(null) }
    val profile = NextDNSProfile(profileId.trim(), deviceName.trim())

    Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(20.dp)) {
        Text("NextDNS Private DNS")
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = profileId,
            onValueChange = { profileId = it },
            label = { Text("Profile ID") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = deviceName,
            onValueChange = { deviceName = it },
            label = { Text("Device name") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Text("The device name is included in the NextDNS Private DNS hostname.")
        if (profileId.isNotEmpty() && !profile.isValid) {
            Text("Use the 4-16 character lowercase profile ID from NextDNS.")
        }
        error?.let { Text(it) }
        Spacer(Modifier.height(16.dp))
        Button(
            onClick = {
                RelayConfiguration.save(context, RelayConfiguration.load(context).copy(
                    nextDNSProfileId = profile.profileID,
                    nextDNSDeviceName = profile.deviceName,
                ))
                val hostname = requireNotNull(profile.dotHostname)
                val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                clipboard.setPrimaryClip(ClipData.newPlainText("Private DNS hostname", hostname))
                error = try {
                    context.startActivity(Intent(PRIVATE_DNS_SETTINGS))
                    null
                } catch (e: ActivityNotFoundException) {
                    try {
                        context.startActivity(Intent(Settings.ACTION_WIRELESS_SETTINGS))
                        "Open Private DNS and paste the copied hostname: $hostname"
                    } catch (fallback: ActivityNotFoundException) {
                        "Settings could not be opened. Enter this hostname in Private DNS: $hostname"
                    }
                }
            },
            enabled = profile.isValid,
        ) { Text("Copy hostname and open Private DNS") }
        Text("Choose Private DNS provider hostname and paste ${profile.dotHostname ?: "your hostname"}. Android must show it as active before this phone uses it on wifi or cellular. A full-tunnel VPN can use its own resolver.")
        Button(onClick = onBack) { Text("Back") }
    }
}
