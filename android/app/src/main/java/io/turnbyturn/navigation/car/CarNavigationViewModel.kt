package io.turnbyturn.navigation.car

import android.util.Log
import androidx.lifecycle.viewModelScope
import com.stadiamaps.ferrostar.core.DefaultNavigationViewModel
import com.stadiamaps.ferrostar.core.FerrostarCore
import com.stadiamaps.ferrostar.core.NavigationUiState
import com.stadiamaps.ferrostar.core.location.NavigationLocationProvider
import com.stadiamaps.ferrostar.core.location.toUserLocation
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import uniffi.ferrostar.GeographicCoordinate
import uniffi.ferrostar.Route
import uniffi.ferrostar.UserLocation
import uniffi.ferrostar.Waypoint
import uniffi.ferrostar.WaypointKind

/**
 * Ferrostar's DefaultNavigationViewModel plus what the planner hand-off needs: watching for a
 * trip from "Send to car", starting it from the current location, simulated "auto drive", and
 * mirroring the phone's position to the backend.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class CarNavigationViewModel(
    private val ferrostarCore: FerrostarCore = CarModule.ferrostarCore,
    private val locationProvider: NavigationLocationProvider = CarModule.locationProvider,
) : DefaultNavigationViewModel(ferrostarCore) {

  private val _pendingTrip = MutableStateFlow<ActiveTrip?>(null)
  /** The latest trip sent from the planner, whether or not it has been started. */
  val pendingTrip: StateFlow<ActiveTrip?> = _pendingTrip.asStateFlow()

  private val _status = MutableStateFlow<String?>(null)
  /** A short message for the car screen ("Fetching route…", errors). */
  val status: StateFlow<String?> = _status.asStateFlow()

  private val _simulated = MutableStateFlow(false)
  val simulated: StateFlow<Boolean> = _simulated.asStateFlow()

  private val idleLocation = MutableStateFlow<UserLocation?>(null)

  // Show the user's position on the map even before navigation starts (as Ferrostar's demo does).
  override val navigationUiState: StateFlow<NavigationUiState> =
      combine(super.navigationUiState, idleLocation) { state, location ->
            if (state.isNavigating()) state else state.copy(location = location)
          }
          .stateIn(viewModelScope, SharingStarted.Eagerly, NavigationUiState.empty())

  private var started = false
  // The route last started, for switching to simulation mid-trip.
  @Volatile private var currentRoute: Route? = null

  /** Begin polling and location tracking. Call once location permission is granted. */
  fun start() {
    if (started) return
    started = true
    viewModelScope.launch(Dispatchers.IO) {
      while (true) {
        try {
          _pendingTrip.value = CarModule.fetchActiveTrip()
        } catch (e: Exception) {
          Log.w(TAG, "Trip poll failed: ${e.message}")
        }
        delay(5_000)
      }
    }
    viewModelScope.launch {
      try {
        locationProvider.locationUpdates(2_000).collect { location ->
          idleLocation.value = location.toUserLocation()
        }
      } catch (e: SecurityException) {
        _status.value = "Location permission is needed"
      }
    }
    // Mirror the driven position (snapped while navigating) to the laptop, at most every 2 s.
    viewModelScope.launch {
      var lastPost = 0L
      navigationUiState.map { it.location }.collect { location ->
        val now = System.currentTimeMillis()
        if (location != null && now - lastPost >= 2_000) {
          lastPost = now
          CarModule.postLocation(
              location.coordinates.lat,
              location.coordinates.lng,
              location.horizontalAccuracy,
              location.courseOverGround?.degrees?.toDouble(),
              location.speed?.value,
          )
        }
      }
    }
  }

  /** Start (or restart) navigation for the trip most recently sent from the planner. */
  fun startPendingTrip() {
    viewModelScope.launch(Dispatchers.IO) {
      try {
        _status.value = "Fetching route…"
        val trip = CarModule.fetchActiveTrip() ?: _pendingTrip.value
        if (trip == null) {
          _status.value = "No trip yet: press Car in the planner"
          return@launch
        }
        CarModule.activeTrip = trip
        val here =
            idleLocation.value
                ?: locationProvider.lastLocation()?.toUserLocation()
                ?: throw IllegalStateException("Waiting for a GPS fix")
        val waypoints =
            trip.waypoints.map { (lat, lng) ->
              Waypoint(coordinate = GeographicCoordinate(lat, lng), kind = WaypointKind.BREAK)
            }
        val route = ferrostarCore.getRoutes(here, waypoints).first()
        currentRoute = route
        if (_simulated.value) locationProvider.enableSimulationOn(route)
        setDestination(trip.destination)
        if (navigationUiState.value.isNavigating()) {
          ferrostarCore.replaceRoute(route)
        } else {
          ferrostarCore.startNavigation(route)
        }
        _status.value = null
      } catch (e: Exception) {
        Log.e(TAG, "Could not start trip", e)
        _status.value = e.message ?: "Could not start the trip"
      }
    }
  }

  /** Android Auto's "auto drive" (and our Simulate action): drive the current route. */
  fun enableAutoDrive() {
    _simulated.value = true
    currentRoute?.let { locationProvider.enableSimulationOn(it) }
  }

  fun toggleSimulation() {
    if (_simulated.value) {
      _simulated.value = false
      locationProvider.disableSimulation()
    } else {
      enableAutoDrive()
    }
  }

  override fun toggleMute() {
    val observer = ferrostarCore.spokenInstructionObserver ?: return
    observer.setMuted(!observer.isMuted)
  }

  companion object {
    private const val TAG = "TbtCarViewModel"
  }
}
