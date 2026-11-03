export const DEMO_SCENARIOS = [
  {
    id: 'gulf-coast-tanker',
    label: 'Tanker + two stops + storm',
    query: 'I am hauling a tanker from the Port of Houston to the Port of Port Arthur. Keep me on truck-safe roads, stop at Buc-ee\'s in Baytown and the TA in Beaumont in that order, and route me around any severe weather cells.',
    talkingPoints: 'Shows vehicle inference, ordered stops, open weather data, and a weather-aware truck route in one request.',
    plan: {
      originText: 'Port of Houston, Houston, Texas',
      destinationText: 'Port of Port Arthur, Port Arthur, Texas',
      includeTexts: ["Buc-ee's, Baytown, Texas", 'TA Travel Center, Beaumont, Texas'],
      avoidStorms: true,
      profile: 'truck',
      clarification: null,
    },
  },
  {
    id: 'ev-road-trip',
    label: 'EV trip + ordered charging stops',
    query: 'Get me from San Francisco City Hall to Palisades Tahoe. I am in an electric car, so take me through the Electrify America chargers in Vacaville and Roseville, keep those stops in order, and avoid dangerous weather over the pass.',
    talkingPoints: 'Demonstrates a long conversational request with ordered POIs and storm avoidance.',
    plan: {
      originText: 'San Francisco City Hall, San Francisco, California',
      destinationText: 'Palisades Tahoe, Olympic Valley, California',
      includeTexts: ['Electrify America, Vacaville, California', 'Electrify America, Roseville, California'],
      avoidStorms: true,
      profile: 'auto',
      clarification: null,
    },
  },
  {
    id: 'airport-family-pickup',
    label: 'Airport run + two pickups',
    query: 'Take me from Austin-Bergstrom airport to Dallas Love Field, but first pick someone up at the University of Texas in Austin and then another person at Baylor University in Waco. Use the fastest car route and steer clear of heavy rain.',
    talkingPoints: 'Shows natural sequencing, institution geocoding, and an explicit driving profile.',
    plan: {
      originText: 'Austin-Bergstrom International Airport, Texas',
      destinationText: 'Dallas Love Field, Texas',
      includeTexts: ['University of Texas at Austin, Texas', 'Baylor University, Waco, Texas'],
      avoidStorms: true,
      profile: 'auto',
      clarification: null,
    },
  },
  {
    id: 'freight-i10',
    label: 'Freight corridor + service stops',
    query: 'Plan a tractor-trailer run from the Port of New Orleans to the Port of Houston. Route through the Pilot in Lafayette and the Love\'s in Lake Charles, use truck routing, and do not send me through severe storms or hail.',
    talkingPoints: 'A commercial-driving example spanning multiple cities and safety constraints.',
    plan: {
      originText: 'Port of New Orleans, Louisiana',
      destinationText: 'Port of Houston, Texas',
      includeTexts: ['Pilot Travel Center, Lafayette, Louisiana', "Love's Travel Stop, Lake Charles, Louisiana"],
      avoidStorms: true,
      profile: 'truck',
      clarification: null,
    },
  },
  {
    id: 'winter-cascades',
    label: 'Mountain drive + weather detour',
    query: 'Drive me from Pike Place Market in Seattle to downtown Spokane. I need coffee in North Bend and a food stop in Ellensburg before crossing the mountains, and I would rather detour than drive into dangerous winter weather.',
    talkingPoints: 'Useful for showing that ordinary driver language maps onto waypoints and weather constraints.',
    plan: {
      originText: 'Pike Place Market, Seattle, Washington',
      destinationText: 'Downtown Spokane, Washington',
      includeTexts: ['North Bend, Washington', 'Ellensburg, Washington'],
      avoidStorms: true,
      profile: 'auto',
      clarification: null,
    },
  },
  {
    id: 'simple-contrast',
    label: 'Simple control example',
    query: 'Navigate from Houston City Hall to Pasadena City Hall by car, with no extra stops.',
    talkingPoints: 'A quick control case to contrast with the more constrained requests.',
    plan: {
      originText: 'Houston City Hall, Houston, Texas',
      destinationText: 'Pasadena City Hall, Pasadena, Texas',
      includeTexts: [],
      avoidStorms: false,
      profile: 'auto',
      clarification: null,
    },
  },
];

export const DEMO_OPTIONS = DEMO_SCENARIOS.map((scenario) => ({
  value: scenario.id,
  label: scenario.label,
}));

export const getDemoScenario = (id) => (
  DEMO_SCENARIOS.find((scenario) => scenario.id === id) || null
);
