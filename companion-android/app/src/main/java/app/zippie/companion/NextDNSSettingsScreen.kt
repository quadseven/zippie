package app.zippie.companion

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.ButtonDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import app.zippie.companion.design.Ink
import app.zippie.companion.design.Kind
import app.zippie.companion.design.Space
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/**
 * Per-user NextDNS settings screen.
 *
 * Lets each phone choose its own NextDNS profile ID and device name so queries
 * appear under different device names in the profile logs, whether the phone is
 * on the router's wifi or on cellular.
 *
 * The screen shows the actual system setting (not the desired value cached by the
 * app) because a failed save can leave the previous resolver installed and
 * active.
 */
@Composable
fun NextDNSSettingsScreen(
    onNextDNSConfigured: () -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var currentProfile by remember { mutableStateOf<NextDNSProfile?>(null) }
    var profileId by remember { mutableStateOf("") }
    var deviceName by remember { mutableStateOf("") }

    // Load current DNS config on first composition
    scope.launch(Dispatchers.IO) {
        val profile = getCurrentProfile(context)
        currentProfile = profile
        profile?.let {
            profileId = it.profileID
            deviceName = it.deviceName
        }
    }

    var isProcessing by remember { mutableStateOf(false) }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp, vertical = 24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text("NextDNS", style = Kind.title)
        Spacer(Modifier.height(12.dp))

        Text(
            "Configure your NextDNS profile so queries from this phone appear under your account, whether on the router's wifi or on cellular.",
            style = Kind.body,
            color = Ink.secondary,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(24.dp))

        OutlinedTextField(
            value = profileId,
            onValueChange = { profileId = it },
            label = { Text("Profile ID") },
            supportingText = {
                if (profileId.isNotEmpty() && !NextDNSProfile(profileId).isValid) {
                    Text("Use the 4-16 character lowercase profile ID from NextDNS.", color = Ink.degraded)
                }
            },
            singleLine = true,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
            keyboardActions = KeyboardActions(onNext = { Unit }),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(Space.tight))

        OutlinedTextField(
            value = deviceName,
            onValueChange = { deviceName = it },
            label = { Text("Device name (optional)") },
            supportingText = {
                Text(
                    "What this phone should be called in NextDNS logs.",
                    style = Kind.caption,
                    color = Ink.secondary,
                )
            },
            singleLine = true,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
            keyboardActions = KeyboardActions(onDone = { saveConfig(context, scope, profileId, deviceName) }),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(Space.roomy))

        val profile = NextDNSProfile(profileId, deviceName)
        if (isProcessing) {
            CircularProgressIndicator()
            Spacer(Modifier.height(Space.tight))
        } else {
            Button(
                onClick = { saveConfig(context, scope, profileId, deviceName) },
                enabled = profile.isValid,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text("Use NextDNS")
            }
            Spacer(Modifier.height(Space.tight))

            // Remove button if we have a current config
            if (currentProfile != null) {
                Button(
                    onClick = { removeConfig(context, scope) },
                    enabled = !isProcessing,
                    modifier = Modifier.fillMaxWidth(),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.errorContainer,
                        contentColor = MaterialTheme.colorScheme.onErrorContainer
                    ),
                ) {
                    Text("Stop using NextDNS")
                }
            }
        }

        Spacer(Modifier.height(Space.section))

        Text(
            if (currentProfile != null) {
                val name = profile.sanitizedDeviceName
                if (name.isEmpty()) {
                    "Android is using NextDNS profile ${profile.profileID}."
                } else {
                    "Android is using profile ${profile.profileID} with the device name ${name}."
                }
            } else if (profileId.isEmpty()) {
                "Add your profile so queries from this phone appear under your account."
            } else {
                "Not configured"
            },
            style = Kind.body,
            color = if (currentProfile != null) Ink.live else if (profileId.isNotEmpty()) Ink.primary else Ink.secondary,
            textAlign = TextAlign.Center,
        )

        Spacer(Modifier.height(Space.tight))

        Text(
            "This setting follows this phone across wifi and cellular. A full-tunnel VPN connection uses the VPN's resolver instead.",
            style = Kind.caption,
            color = Ink.secondary,
            textAlign = TextAlign.Center,
        )

        Spacer(Modifier.height(Space.tight))

        Button(
            onClick = onNextDNSConfigured,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("Back")
        }
    }
}

private fun getCurrentProfile(context: android.content.Context): NextDNSProfile? {
    try {
        val manager = AndroidDNSSettingsManager(context)
        // Use runBlocking to call suspend function in non-suspending context
        val serverURL = kotlinx.coroutines.runBlocking {
            manager.load()
            manager.serverURL
        }
        return serverURL?.let { NextDNSProfile.fromDohURL(it) }
    } catch (e: SecurityException) {
        return null
    } catch (e: UnsupportedOperationException) {
        return null
    }
}

private fun saveConfig(
    context: android.content.Context,
    scope: CoroutineScope,
    profileId: String,
    deviceName: String,
) {
    val profile = NextDNSProfile(profileId, deviceName.trim())
    if (!profile.isValid) return

    scope.launch(Dispatchers.IO) {
        try {
            val manager = AndroidDNSSettingsManager(context)
            manager.save(profile.dohURL!!)
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }
}

private fun removeConfig(
    context: android.content.Context,
    scope: CoroutineScope,
) {
    scope.launch(Dispatchers.IO) {
        try {
            val manager = AndroidDNSSettingsManager(context)
            manager.remove()
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }
}
