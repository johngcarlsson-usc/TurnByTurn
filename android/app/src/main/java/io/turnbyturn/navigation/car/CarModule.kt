package io.turnbyturn.navigation.car

import android.content.Context
import android.util.Log
import com.stadiamaps.ferrostar.core.AndroidTtsObserver
import com.stadiamaps.ferrostar.core.CorrectiveAction
import com.stadiamaps.ferrostar.core.CustomRouteProvider
import com.stadiamaps.ferrostar.core.FerrostarCore
import com.stadiamaps.ferrostar.core.RouteDeviationHandler
import com.stadiamaps.ferrostar.core.http.OkHttpClientProvider.Companion.toOkHttpClientProvider
import com.stadiamaps.ferrostar.core.location.AndroidLocationProvider
import com.stadiamaps.ferrostar.core.location.NavigationLocationProvider
import com.stadiamaps.ferrostar.core.location.SimulatedLocationProvider
import io.turnbyturn.navigation.BuildConfig
import java.time.Duration
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import uniffi.ferrostar.CourseFiltering
import uniffi.ferrostar.NavigationControllerConfig
import uniffi.ferrostar.RouteAdapter
import uniffi.ferrostar.RouteDeviationTracking
import uniffi.ferrostar.WaypointAdvanceMode
import uniffi.ferrostar.WellKnownRouteProvider
import uniffi.ferrostar.stepAdvanceDistanceEntryAndExit
import uniffi.ferrostar.stepAdvanceDistanceToEndOfStep
import uniffi.ferrostar.stepAdvanceOr

/**
 * Wiring for the Android Auto side, modeled on Ferrostar's demo AppModule.
 *
 * Routes never come from Valhalla directly: they come from the planner backend's
 * /api/nav/route, which rebuilds the planned trip with every exclusion the planner found, so
 * Ferrostar's automatic reroutes keep the driver's constraints.
 */
object CarModule {
  private const val TAG = "TbtCarModule"
  private val JSON = "application/json".toMediaType()

  val backendUrl: String = BuildConfig.BACKEND_URL.trimEnd('/')
  // The basemap: the local TileServer GL, published at /tiles on the same host.
  val MAP_STYLE_URL = "$backendUrl/tiles/styles/liberty/style.json"

  private lateinit var appContext: Context

  fun init(context: Context) {
    if (!::appContext.isInitialized) appContext = context.applicationContext
  }

  val http: OkHttpClient by lazy {
    OkHttpClient.Builder().callTimeout(Duration.ofSeconds(30)).build()
  }

  /** The trip that "Send to car" stored, which navigation and reroutes are built from. */
  @Volatile var activeTrip: ActiveTrip? = null

  suspend fun fetchActiveTrip(): ActiveTrip? =
      withContext(Dispatchers.IO) {
        val request = Request.Builder().url("$backendUrl/api/trip/active").build()
        http.newCall(request).execute().use { response ->
          if (!response.isSuccessful) return@withContext null
          val trip = JSONObject(response.body.string()).optJSONObject("trip") ?: return@withContext null
          ActiveTrip.fromJson(trip)
        }
      }

  /** Mirror the phone's position to the backend so the laptop's drive view follows along. */
  fun postLocation(lat: Double, lng: Double, accuracy: Double, heading: Double?, speed: Double?) {
    val body =
        JSONObject()
            .put("lat", lat)
            .put("lng", lng)
            .put("accuracy", accuracy)
            .put("heading", heading ?: JSONObject.NULL)
            .put("speed", speed ?: JSONObject.NULL)
            .toString()
            .toRequestBody(JSON)
    http
        .newCall(Request.Builder().url("$backendUrl/api/gps").post(body).build())
        .enqueue(
            object : okhttp3.Callback {
              override fun onFailure(call: okhttp3.Call, e: java.io.IOException) = Unit

              override fun onResponse(call: okhttp3.Call, response: okhttp3.Response) =
                  response.close()
            })
  }

  // Only the response parser is used: requests go to our backend, not to Valhalla.
  private val osrmParser: RouteAdapter by lazy {
    RouteAdapter.fromWellKnownRouteProvider(WellKnownRouteProvider.Valhalla("http://unused", "auto"))
  }

  /**
   * Ferrostar asks for routes here at the start and on every reroute. `waypoints` are the
   * remaining stops, so the backend restarts the planned trip from the current location through
   * the last `waypoints.size` points.
   */
  private val routeProvider = CustomRouteProvider { userLocation, waypoints ->
    val trip = activeTrip ?: throw IllegalStateException("No trip has been sent to the car.")
    val body =
        JSONObject()
            .put("navRequest", trip.navRequest)
            .put(
                "from",
                JSONObject()
                    .put("lat", userLocation.coordinates.lat)
                    .put("lng", userLocation.coordinates.lng))
            .put("remainingPoints", waypoints.size.coerceAtLeast(1))
            .toString()
            .toRequestBody(JSON)
    val bytes =
        withContext(Dispatchers.IO) {
          http
              .newCall(Request.Builder().url("$backendUrl/api/nav/route").post(body).build())
              .execute()
              .use { response ->
                if (!response.isSuccessful) {
                  throw IllegalStateException("Route request failed: HTTP ${response.code}")
                }
                response.body.bytes()
              }
        }
    osrmParser.parseResponse(bytes)
  }

  val locationProvider: NavigationLocationProvider by lazy {
    NavigationLocationProvider(
        liveProviding = AndroidLocationProvider(appContext),
        simulatedProvider = SimulatedLocationProvider(warpFactor = 2u),
    )
  }

  // Same step-advance rules as the web drive view: leave a step at its end OR within 15 m of
  // it, so zero-length "arrived" steps at intermediate stops don't stall navigation.
  private val navigationConfig =
      NavigationControllerConfig(
          waypointAdvance = WaypointAdvanceMode.WaypointWithinRange(100.0),
          stepAdvanceCondition =
              stepAdvanceOr(
                  listOf(
                      stepAdvanceDistanceEntryAndExit(20u.toUShort(), 5u.toUShort(), 25u.toUShort()),
                      stepAdvanceDistanceToEndOfStep(15u.toUShort(), 25u.toUShort()))),
          arrivalStepAdvanceCondition = stepAdvanceDistanceToEndOfStep(15u.toUShort(), 25u.toUShort()),
          routeDeviationTracking = RouteDeviationTracking.StaticThreshold(25u.toUShort(), 50.0),
          snappedLocationCourseFiltering = CourseFiltering.SNAP_TO_ROUTE,
      )

  val ferrostarCore: FerrostarCore by lazy {
    FerrostarCore(
            customRouteProvider = routeProvider,
            httpClient = http.toOkHttpClientProvider(),
            locationProvider = locationProvider,
            navigationControllerConfig = navigationConfig,
        )
        .apply {
          // Off route: ask our backend for a new route through the remaining stops.
          deviationHandler = RouteDeviationHandler { _, _, remainingWaypoints ->
            Log.i(TAG, "Off route; rerouting through ${remainingWaypoints.size} waypoint(s)")
            CorrectiveAction.GetNewRoutes(remainingWaypoints)
          }
          spokenInstructionObserver = ttsObserver
        }
  }

  val ttsObserver: AndroidTtsObserver by lazy { AndroidTtsObserver(appContext).also { it.start() } }

  val viewModel: CarNavigationViewModel by lazy { CarNavigationViewModel() }
}

/** A trip handed over from the planner with "Send to car". */
data class ActiveTrip(
    val id: Int,
    val navRequest: JSONObject,
    val destination: String,
    val stops: List<String>,
    val summary: String,
) {
  /** Planned points after the origin: the stops in order, then the destination. */
  val waypoints: List<Pair<Double, Double>>
    get() {
      val points = navRequest.getJSONArray("points")
      return (1 until points.length()).map {
        val p = points.getJSONObject(it)
        p.getDouble("lat") to p.getDouble("lng")
      }
    }

  companion object {
    fun fromJson(json: JSONObject): ActiveTrip {
      val stops = json.optJSONArray("stops")
      return ActiveTrip(
          id = json.getInt("id"),
          navRequest = json.getJSONObject("navRequest"),
          destination = json.optString("destination", "Destination"),
          stops = (0 until (stops?.length() ?: 0)).map { stops!!.getString(it) },
          summary = json.optString("summary", ""),
      )
    }
  }
}
