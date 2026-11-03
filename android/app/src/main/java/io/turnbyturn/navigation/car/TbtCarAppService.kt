package io.turnbyturn.navigation.car

import android.content.Intent
import androidx.car.app.CarAppService
import androidx.car.app.Screen
import androidx.car.app.Session
import androidx.car.app.SessionInfo
import androidx.car.app.validation.HostValidator

/** Android Auto entry point (declared as a NAVIGATION car app in the manifest). */
class TbtCarAppService : CarAppService() {
  // Sideloaded for testing, so any Android Auto host may bind. A Play Store build would list
  // the allowed hosts instead (see Ferrostar's demo for the reference allowlist).
  override fun createHostValidator(): HostValidator = HostValidator.ALLOW_ALL_HOSTS_VALIDATOR

  override fun onCreateSession(sessionInfo: SessionInfo): Session = TbtCarSession()
}

class TbtCarSession : Session() {
  override fun onCreateScreen(intent: Intent): Screen {
    CarModule.init(carContext)
    CarModule.viewModel.start()
    return TbtNavigationScreen(carContext)
  }
}
