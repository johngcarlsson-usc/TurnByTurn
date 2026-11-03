package io.turnbyturn.navigation.car

import androidx.car.app.CarContext
import androidx.car.app.model.Action
import androidx.car.app.model.ActionStrip
import androidx.car.app.model.Template
import androidx.car.app.navigation.NavigationManager
import androidx.car.app.navigation.model.MessageInfo
import androidx.car.app.navigation.model.NavigationTemplate
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Modifier
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import com.rallista.car.app.compose.ComposableScreen
import com.stadiamaps.ferrostar.car.app.navigation.NavigationManagerBridge
import com.stadiamaps.ferrostar.car.app.template.NavigationTemplateBuilder
import com.stadiamaps.ferrostar.composeui.config.VisualNavigationViewConfig
import com.stadiamaps.ferrostar.core.NavigationUiState
import com.stadiamaps.ferrostar.core.boundingBox
import com.stadiamaps.ferrostar.maplibreui.runtime.NavigationMapState
import com.stadiamaps.ferrostar.maplibreui.runtime.navigationCameraOptions
import com.stadiamaps.ferrostar.maplibreui.runtime.rememberNavigationMapState
import com.stadiamaps.ferrostar.ui.maplibre.car.app.CarAppNavigationView
import com.stadiamaps.ferrostar.ui.maplibre.car.app.runtime.SurfaceAreaTracker
import com.stadiamaps.ferrostar.ui.maplibre.car.app.runtime.screenSurfaceState
import com.stadiamaps.ferrostar.ui.maplibre.car.app.runtime.surfaceStableFractionalPadding
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import uniffi.ferrostar.DrivingSide

/**
 * The car screen: Ferrostar's MapLibre navigation map on the car display surface, the
 * car-app NavigationTemplate (maneuvers, lanes, ETA) while navigating, and an idle template
 * showing the trip sent from the planner. Adapted from Ferrostar's DemoNavigationScreen.
 */
class TbtNavigationScreen(carContext: CarContext) : ComposableScreen(carContext) {

  private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
  private val viewModel = CarModule.viewModel

  private val navigationManagerBridge =
      NavigationManagerBridge(
          navigationManager = carContext.getCarService(NavigationManager::class.java),
          context = carContext,
          viewModel = viewModel,
          backupDrivingSide = DrivingSide.RIGHT,
          onStopNavigation = { viewModel.stopNavigation() },
          onAutoDriveEnabled = { viewModel.enableAutoDrive() },
          isCarForeground = { lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED) },
      )

  private var uiState: NavigationUiState? by mutableStateOf(null)
  private var navigationMapState: NavigationMapState? = null
  private var browsingPadding: PaddingValues = PaddingValues()
  private val surfaceAreaTracker = SurfaceAreaTracker { surfaceGestureCallback = it }

  init {
    navigationManagerBridge.start(scope)
    combine(viewModel.navigationUiState, viewModel.pendingTrip, viewModel.status, viewModel.simulated) {
          state, _, _, _ ->
          state
        }
        .onEach { state ->
          uiState = state
          invalidate()
        }
        .launchIn(scope)

    lifecycle.addObserver(
        object : DefaultLifecycleObserver {
          override fun onDestroy(owner: LifecycleOwner) {
            navigationManagerBridge.stop()
            scope.cancel()
          }
        })
  }

  @Composable
  override fun content() {
    val surfaceArea by screenSurfaceState(surfaceAreaTracker)
    val normalPadding = surfaceStableFractionalPadding(surfaceArea?.compositeArea)
    val trackingPadding = surfaceStableFractionalPadding(surfaceArea?.compositeArea, top = 0.5f)
    val cameraOptions =
        navigationCameraOptions().copy(browsingPadding = normalPadding, navigationPadding = trackingPadding)
    val mapState = rememberNavigationMapState(navigationCameraOptions = cameraOptions)
    browsingPadding = normalPadding
    navigationMapState = mapState

    LaunchedEffect(uiState?.isNavigating(), mapState) {
      if (uiState?.isNavigating() == true) mapState.recenter(isNavigating = true)
    }
    LaunchedEffect(mapState) { snapshotFlow { mapState.cameraMode }.collectLatest { invalidate() } }

    CarAppNavigationView(
        modifier = Modifier.fillMaxSize(),
        styleUrl = CarModule.MAP_STYLE_URL,
        navigationMapState = mapState,
        navigationCameraOptions = cameraOptions,
        viewModel = viewModel,
        config = VisualNavigationViewConfig.Default(),
        surfaceAreaTracker = surfaceAreaTracker,
    )
  }

  override fun onGetTemplate(): Template {
    val state = uiState
    if (state?.isNavigating() == true) {
      return NavigationTemplateBuilder(carContext)
          .setBackupDrivingSide(DrivingSide.RIGHT)
          .setOnStopNavigation { viewModel.stopNavigation() }
          .setOnMute(state.isMuted) { viewModel.toggleMute() }
          .setOnZoom(
              onZoomInTapped = { navigationMapState?.zoomIn() },
              onZoomOutTapped = { navigationMapState?.zoomOut() },
          )
          .setOnCycleCamera(navigationMapState?.isTrackingUser == true) { centerCamera() }
          .setTripState(state.tripState)
          .build()
    }
    return idleTemplate()
  }

  /** Not navigating: show what the planner sent, with Start and Simulate. */
  private fun idleTemplate(): Template {
    val trip = viewModel.pendingTrip.value
    val status = viewModel.status.value
    val message =
        when {
          status != null -> MessageInfo.Builder(status).build()
          trip != null ->
              MessageInfo.Builder("To ${trip.destination}")
                  .setText(
                      listOfNotNull(
                              trip.summary.ifBlank { null },
                              trip.stops.takeIf { it.isNotEmpty() }?.joinToString(" → ", prefix = "via "))
                          .joinToString(" · "))
                  .build()
          else ->
              MessageInfo.Builder("No trip yet")
                  .setText("Plan a route in Exagion and press Car")
                  .build()
        }
    val actions = ActionStrip.Builder()
    if (trip != null) {
      actions.addAction(
          Action.Builder().setTitle("Start").setOnClickListener { viewModel.startPendingTrip() }.build())
    }
    actions.addAction(
        Action.Builder()
            .setTitle(if (viewModel.simulated.value) "Sim: on" else "Simulate")
            .setOnClickListener {
              viewModel.toggleSimulation()
              invalidate()
            }
            .build())
    return NavigationTemplate.Builder()
        .setNavigationInfo(message)
        .setActionStrip(actions.build())
        .setMapActionStrip(ActionStrip.Builder().addAction(Action.PAN).build())
        .build()
  }

  private fun centerCamera() {
    val mapState = navigationMapState ?: return
    if (mapState.isTrackingUser) {
      val bounds = viewModel.navigationUiState.value.routeGeometry?.boundingBox() ?: return
      mapState.showRouteOverview(boundingBox = bounds, paddingValues = browsingPadding)
    } else {
      mapState.recenter(isNavigating = uiState?.isNavigating() == true)
    }
    invalidate()
  }
}
