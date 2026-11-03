# Turn by Turn prerecorded demo queries

These examples are deliberately phrased the way a driver might speak: one breath, mixed constraints, and informal descriptions of vehicle, stops, and weather. In the app, select the matching **Rehearsed demo** and click **Run demo**. That path uses a deterministic checked-in plan while still showing the original natural-language request in the conversation.

## Recommended three-minute recording

### 1. Tanker, ordered stops, and severe weather

> I am hauling a tanker from the Port of Houston to the Port of Port Arthur. Keep me on truck-safe roads, stop at Buc-ee's in Baytown and the TA in Beaumont in that order, and route me around any severe weather cells.

Point out that one utterance becomes a truck profile, two ordered required stops, a live weather-grid check, and storm exclusion polygons before Valhalla solves the route.

Expected plan:

- Origin: Port of Houston
- Destination: Port of Port Arthur
- Profile: truck
- Stops: Buc-ee's Baytown, then TA Beaumont
- Weather avoidance: on

### 2. EV road trip with charging waypoints

> Get me from San Francisco City Hall to Palisades Tahoe. I am in an electric car, so take me through the Electrify America chargers in Vacaville and Roseville, keep those stops in order, and avoid dangerous weather over the pass.

Point out that the current prototype models charging needs as required POI stops. It does not yet calculate battery state or charger availability, so do not claim that it does.

Expected plan:

- Origin: San Francisco City Hall
- Destination: Palisades Tahoe
- Profile: auto
- Stops: Electrify America Vacaville, then Electrify America Roseville
- Weather avoidance: on

### 3. Airport trip with natural pickup language

> Take me from Austin-Bergstrom airport to Dallas Love Field, but first pick someone up at the University of Texas in Austin and then another person at Baylor University in Waco. Use the fastest car route and steer clear of heavy rain.

This is a good closing example because “pick someone up” and “then another person” are understood as ordered waypoints without using routing jargon.

Expected plan:

- Origin: Austin-Bergstrom International Airport
- Destination: Dallas Love Field
- Profile: auto
- Stops: University of Texas at Austin, then Baylor University
- Weather avoidance: on

## Additional examples

### Freight corridor

> Plan a tractor-trailer run from the Port of New Orleans to the Port of Houston. Route through the Pilot in Lafayette and the Love's in Lake Charles, use truck routing, and do not send me through severe storms or hail.

### Winter mountain drive

> Drive me from Pike Place Market in Seattle to downtown Spokane. I need coffee in North Bend and a food stop in Ellensburg before crossing the mountains, and I would rather detour than drive into dangerous winter weather.

### Simple control case

> Navigate from Houston City Hall to Pasadena City Hall by car, with no extra stops.

Use this last one to show that the agent still handles ordinary navigation requests and does not force every query into an elaborate workflow.

## Recording checklist

1. Start local Valhalla, Ollama, and the Turn by Turn app before recording.
2. Confirm the geocoded POIs once; re-importing Nominatim or the places data can move them.
3. Use **Run demo**, not the send arrow, for deterministic interpretation.
4. Keep the OpenStreetMap/OpenMapTiles attribution visible in the map frame.
5. Describe storm avoidance as weather-grid-based routing, not a guarantee of road safety.
6. Describe truck routing as Valhalla's truck cost model, not regulatory hazmat compliance.
