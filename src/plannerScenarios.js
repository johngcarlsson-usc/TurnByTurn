// Rehearsed Bay Area scenarios for the talk. Each pairs the spoken request
// with a checked-in constraint IR, so a recorded demo never depends on the
// model returning identical JSON. The live path sends `query` to the model.

const NO_AVOID = { highways: false, tolls: false, ferries: false, storms: false, areas: [], roads: [] };

const errand = (what, extra = {}) => ({
  kind: 'errand', text: null, what, brands: [], side: 'any', maxDetourMinutes: null,
  position: 'anywhere', afterMinutes: null, dwellMinutes: null, closesAt: null, ...extra,
});

const task = (fields) => ({
  kind: 'errand', who: null, from: null, to: null, text: null, what: null, brands: [], arriveBy: null, dwellMinutes: null,
  ...fields,
});

const ir = (fields) => ({
  origin: null, destination: null, returnToOrigin: false, stops: [], stopOrder: 'as_listed',
  arriveBy: null, profile: 'auto', clarification: null,
  meetup: { friendFrom: null, friendLeftMinutesAgo: null, what: null, maxDetourMinutes: null },
  team: { partner: null, partnerAt: null, tasks: [] }, useRoads: [], custom: null, ...fields,
  avoid: { ...NO_AVOID, ...(fields.avoid || {}) },
});

export const PLANNER_SCENARIOS = [
  // FOSS4G 2026 talk: five requests from a phone at the Great Mall, Milpitas (the phone's GPS fix is
  // the start; scripts/capture_talk.py posts it the way the beacon page does). Each IR is what the
  // live model returned for the query; ?scenario=<id>&mode=live&autorun=1 sends the query instead.
  {
    id: 'gm-gas-right',
    group: 'FOSS4G: from the Great Mall',
    label: 'Gas on the right, to Levi\'s Stadium',
    query: 'Take me to Levi\'s Stadium, and get gas on the way at a station on the right so I don\'t have to turn across traffic.',
    note: 'Overture stations within 80 m of the road via DuckDB; the side is checked from the final route\'s heading into the stop.',
    ir: ir({ destination: 'Levi\'s Stadium', stops: [errand('gas', { side: 'right' })] }),
  },
  {
    id: 'gm-coffee-hour',
    group: 'FOSS4G: from the Great Mall',
    label: 'Coffee in an hour, to Sacramento',
    query: 'I\'m driving to the Sheraton in downtown Sacramento, and I\'m gonna want coffee about an hour from now. Keep the detour small.',
    note: 'The stretch you\'ll be driving about 60 minutes in (from the route\'s maneuver times), then the smallest detour there, from a Valhalla matrix.',
    ir: ir({ destination: 'Sheraton in downtown Sacramento', stops: [errand('coffee', { afterMinutes: 60 })] }),
  },
  {
    id: 'gm-no-880',
    group: 'FOSS4G: from the Great Mall',
    label: 'Oakland airport, stay off 880',
    query: 'Get me to Oakland airport, but stay off 880.',
    note: 'Map-match the route, exclude every I-880 edge, re-route, repeat until clean.',
    ir: ir({ destination: 'Oakland airport', avoid: { roads: [{ name: 'I-880', near: null }] } }),
  },
  {
    id: 'gm-errands',
    group: 'FOSS4G: from the Great Mall',
    label: 'Pharmacy before 6, groceries, gas, home by 6:15',
    query: 'I need a pharmacy before it closes at six, groceries, and gas, in any order, and I have to be home by 6:15.',
    note: 'Generalized TSP in OR-Tools: one disjunction per errand (which pharmacy, grocery, station), time windows for 6:00 and 6:15.',
    now: '17:15',
    ir: ir({
      destination: 'home', stopOrder: 'any', arriveBy: '18:15',
      stops: [errand('pharmacy', { closesAt: '18:00' }), errand('groceries'), errand('gas')],
    }),
  },
  {
    id: 'gm-team',
    group: 'FOSS4G: from the Great Mall',
    label: 'Split the afternoon with my wife',
    query: 'My wife is at the Fremont BART station. Between the two of us, someone has to pick up our daughter at Stratford School on North Milpitas Boulevard and get her home by 3:30, someone has to do the grocery shopping, which takes about half an hour, and someone has to stop at a CVS. Who should do what?',
    note: 'Two vehicles in OR-Tools: pickup and drop in the same car, 3:30 as a time window, one branch per errand. Then "Send to Wife" texts her part as a link.',
    now: '15:00',
    ir: ir({
      stopOrder: 'any',
      team: {
        partner: 'wife', partnerAt: 'Fremont BART station', tasks: [
          task({ kind: 'escort', who: 'daughter', from: 'Stratford School on North Milpitas Boulevard', to: 'home', arriveBy: '15:30' }),
          task({ what: 'groceries', dwellMinutes: 30 }),
          task({ what: 'pharmacy', brands: ['CVS'] }),
        ],
      },
    }),
  },
  {
    id: 'control',
    group: 'Basics',
    label: 'Plain A to B',
    query: 'Navigate from the Ferry Building to Stanford.',
    note: 'Control case: ordinary requests stay ordinary.',
    ir: ir({ origin: 'Ferry Building', destination: 'Stanford University' }),
  },
  {
    id: 'coffee',
    group: 'Route-aware search',
    label: 'Coffee on the way',
    query: 'Ferry Building to Stanford. I need coffee about halfway, and don\'t make me go more than five minutes out of the way.',
    note: 'Overture places within 2 km of the route, ranked by real detour minutes from a Valhalla matrix, not by distance.',
    ir: ir({
      origin: 'Ferry Building', destination: 'Stanford University',
      stops: [errand('coffee', { position: 'middle', maxDetourMinutes: 5 })],
    }),
  },
  {
    id: 'right-side-gas',
    group: 'Route-aware search',
    label: 'Gas on the right, then Starbucks or Peet\'s',
    query: 'Ferry Building to the San Francisco Zoo. Get me gas at a station on the right so I don\'t have to turn across traffic, then a Starbucks or a Peet\'s.',
    note: 'Without "on the right" it picks a Valero you\'d reach on the left. The side is checked from the route\'s own heading into the stop.',
    ir: ir({
      origin: 'Ferry Building', destination: 'San Francisco Zoo',
      stops: [errand('gas', { side: 'right' }), errand('coffee', { brands: ['Starbucks', "Peet's"] })],
    }),
  },
  {
    id: 'downtown-freeway',
    group: 'Corridor exclusion',
    label: 'Skip the downtown freeway',
    query: 'Take me from the Ferry Building to Daly City BART, but skip the freeway through downtown.',
    note: 'Blocking I-280 just moves the route onto I-80/101. The route-check-exclude loop runs until no downtown freeway remains.',
    ir: ir({
      origin: 'Ferry Building', destination: 'Daly City BART station',
      avoid: { areas: [{ text: 'downtown San Francisco', radiusMiles: null, roads: 'freeways' }] },
    }),
  },
  {
    id: 'bay-bridge',
    group: 'Corridor exclusion',
    label: 'Stay off the Bay Bridge',
    query: 'Get me to Oakland airport from the Ferry Building, and stay off the Bay Bridge.',
    note: 'The routing graph doesn\'t carry the bridge\'s name (OSM bridge:name), so it\'s matched by footprint.',
    ir: ir({
      origin: 'Ferry Building', destination: 'Oakland International Airport',
      avoid: { roads: [{ name: 'Bay Bridge', near: null }] },
    }),
  },
  {
    id: 'meetup',
    group: 'Optimization',
    label: 'Meet a friend on the way to Tahoe',
    query: 'My friend just left Davis and we\'re meeting up in Tahoe City. Where\'s a good coffee spot to meet on the way without a huge detour for either of us?',
    note: 'Both routes to the destination, one matrix from both origins, and the spot where you can arrive together soonest with the least waiting.',
    now: '15:00',
    ir: ir({
      origin: 'Ferry Building', destination: 'Tahoe City',
      meetup: { friendFrom: 'Davis', friendLeftMinutesAgo: 0, what: 'coffee', maxDetourMinutes: 15 },
    }),
  },
  {
    id: 'team-swim',
    group: 'Optimization',
    label: 'Split the errands with my wife',
    query: 'My wife is at Stonestown. Between the two of us, someone has to pick up our daughter at Alvarado Elementary and get her to swim lessons at Rossi Pool by 2, and someone has to go to the bank and a CVS. Who should do what?',
    note: 'Two drivers, one to-do list: a small vehicle-routing problem. Pickup and drop share a car, swim is a 2:00 deadline, and any bank or CVS near either of you will do. Say "use her shared location" (after "Ask for location") to plan from her live position instead.',
    now: '13:15',
    ir: ir({
      origin: 'Ferry Building', stopOrder: 'any',
      team: {
        partner: 'wife', partnerAt: 'Stonestown Galleria', tasks: [
          { kind: 'escort', who: 'daughter', from: 'Alvarado Elementary School', to: 'Rossi Pool', text: null, what: null, brands: [], arriveBy: '14:00', dwellMinutes: null },
          { kind: 'errand', who: null, from: null, to: null, text: null, what: 'bank', brands: [], arriveBy: null, dwellMinutes: null },
          { kind: 'errand', who: null, from: null, to: null, text: null, what: 'pharmacy', brands: ['CVS'], arriveBy: null, dwellMinutes: null },
        ],
      },
    }),
  },
  {
    id: 'storm',
    group: 'Hazards',
    label: 'Route around the storm',
    query: 'Ferry Building to Stanford, and route me around that storm cell over the airport.',
    note: 'Simulated storm cell (live Open-Meteo cells work the same way) becomes an exclusion polygon.',
    demoStorms: [{ lat: 37.615, lng: -122.39, radiusMiles: 3 }],
    ir: ir({ origin: 'Ferry Building', destination: 'Stanford University', avoid: { storms: true } }),
  },
  {
    id: 'combo',
    group: 'Optimization',
    label: 'Pickup + coffee + no tolls',
    query: 'Pick up Sam at Stanford, then grab a coffee, then take us to the Ferry Building. No toll bridges, and skip the downtown freeway.',
    note: 'Ordered stops mixing a named place and an errand, with two kinds of avoidance at once.',
    ir: ir({
      origin: 'work', destination: 'Ferry Building',
      stops: [{ kind: 'place', text: 'Stanford University', what: null, brands: [], side: 'any', maxDetourMinutes: null,
        position: 'anywhere', dwellMinutes: null, closesAt: null }, errand('coffee')],
      avoid: { tolls: true, areas: [{ text: 'downtown San Francisco', radiusMiles: null, roads: 'freeways' }] },
    }),
  },
  {
    id: 'breaks-deadline',
    group: 'Where it breaks',
    label: 'Impossible deadline',
    query: 'Pharmacy, groceries, and gas, any order, and I need to be home by five.',
    note: 'No assignment meets the deadline. OR-Tools relaxes it, and the verifier marks the miss instead of hiding it.',
    now: '16:55',
    ir: ir({
      destination: 'home', stopOrder: 'any', arriveBy: '17:00',
      stops: [errand('pharmacy'), errand('groceries'), errand('gas')],
    }),
  },
  {
    id: 'breaks-detour',
    group: 'Where it breaks',
    label: 'Over-tight detour',
    query: 'Ferry Building to Stanford, and find me a car wash that\'s less than one minute out of the way.',
    note: 'An honest "no": the closest candidate and its real detour are reported.',
    ir: ir({
      origin: 'Ferry Building', destination: 'Stanford University',
      stops: [errand('car wash', { maxDetourMinutes: 1 })],
    }),
  },
];

export const SCENARIO_OPTIONS = Object.entries(
  PLANNER_SCENARIOS.reduce((groups, s) => {
    (groups[s.group] = groups[s.group] || []).push({ value: s.id, label: s.label });
    return groups;
  }, {}),
).map(([group, items]) => ({ group, items }));

export const getPlannerScenario = (id) => PLANNER_SCENARIOS.find((s) => s.id === id) || PLANNER_SCENARIOS[0];
