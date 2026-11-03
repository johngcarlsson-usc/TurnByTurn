// Ferrostar core (Rust -> WASM) without a bundler plugin: the package is built
// for bundlers that understand `import * from "x.wasm"`, so instantiate the
// module by hand and hand it to the wasm-bindgen glue.
import * as bg from '@stadiamaps/ferrostar/ferrostar_bg.js';
import wasmUrl from '@stadiamaps/ferrostar/ferrostar_bg.wasm?url';

let ready = null;

export const loadFerrostar = () => {
  if (!ready) {
    ready = (async () => {
      const response = await fetch(wasmUrl);
      const { instance } = await WebAssembly.instantiate(await response.arrayBuffer(), { './ferrostar_bg.js': bg });
      bg.__wbg_set_wasm(instance.exports);
      instance.exports.__wbindgen_start();
      return bg;
    })();
  }
  return ready;
};

// Step advance: leave a step when you pass its end, OR come within 15 m of it.
// The second matters for zero-length "you have arrived" steps at intermediate
// stops, which entry/exit alone never advances past.
export const NAV_CONFIG = {
  waypointAdvance: { WaypointWithinRange: 100 },
  stepAdvanceCondition: {
    OrAdvanceConditions: {
      conditions: [
        { DistanceEntryExit: { distanceToEndOfStep: 20, distanceAfterEndStep: 5, minimumHorizontalAccuracy: 25, hasReachedEndOfCurrentStep: false } },
        { DistanceToEndOfStep: { distance: 15, minimumHorizontalAccuracy: 25 } },
      ],
    },
  },
  arrivalStepAdvanceCondition: { DistanceToEndOfStep: { distance: 15, minimumHorizontalAccuracy: 25 } },
  routeDeviationTracking: { StaticThreshold: { minimumHorizontalAccuracy: 25, maxAcceptableDeviation: 50 } },
  snappedLocationCourseFiltering: 'SnapToRoute',
};

export const parseValhallaOsrm = (ferrostar, text) => {
  const adapter = new ferrostar.RouteAdapter({ Valhalla: { endpointUrl: 'http://unused', profile: 'auto' } });
  const routes = adapter.parseResponse(new TextEncoder().encode(text));
  adapter.free();
  return routes[0];
};
